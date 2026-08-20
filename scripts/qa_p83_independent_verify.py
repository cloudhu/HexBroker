"""QA P8-3 独立复核：v4/v6/v7 缓存 → 引擎 A S2 / 组合 A15B85 / exp_ret 截面 IC。

口径铁律（与 p8_3 声明一致）：
- OOS 起点 2024-07-18 后
- 引擎 A 排序 = 按日截面 rank(pct=True) + min_symbols=3（S2）
- 完整回测：BacktestEngine + CostModel（滑点1tick + 费0.005% + 保证金12%）
  + CONTRACTS18 + INITIAL_CAPITAL=1e6
- 组合 A15/B85（vol_target=N 与 Y 两种）
- exp_ret 截面 IC：每日截面 Spearman(exp_ret, realized)，OOS 段均值

本脚本**不调用** p8_3_label_cross_section / p5_engineA_cross_section 的任何
重估函数，全部自行实现，仅复用数据加载（load_prices）与 hexbroker 回测基础设施。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices

OOS_START = pd.Timestamp("2024-07-18")
IS_END = pd.Timestamp("2022-04-21")
TOP_K = 0.30
NOTIONAL_FRAC = 0.20
MIN_SYMBOLS = 3
PROD_W_A = 0.15
PROD_W_B = 0.85
VOL_TARGET = 0.175
VOL_HALFLIFE = 10
VOL_SCALE_CAP = 1.5
HORIZON = 5

VERSIONS = ["v4", "v6", "v7"]


# ---------------------------------------------------------------------------
# 1. 已实现收益（独立实现）
# ---------------------------------------------------------------------------
def realized_returns(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"]
    )


# ---------------------------------------------------------------------------
# 2. 引擎 A 目标构造（独立实现：按日截面 rank + min=3）
# ---------------------------------------------------------------------------
def engine_a_targets_ind(sig: pd.DataFrame, prices: pd.DataFrame,
                         top_k: float = TOP_K, min_symbols: int = MIN_SYMBOLS) -> pd.DataFrame:
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    # 每日截面 rank：越大越强（pct=True -> [0,1]）
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(mult_map)
    long_cond = (sig["rank_pct"] >= 1.0 - top_k) & sig["_px"].notna()
    if min_symbols is not None:
        long_cond = long_cond & (sig["_day_cnt"] >= min_symbols)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = notional / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(long_cond, raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def run_backtest(cfg, cost, prices, targets) -> tuple[pd.Series, object, object]:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, m, m_oos


def combo_stats_ind(ret_a: pd.Series, ret_b: pd.Series, w_a: float,
                    vol_target: bool) -> dict:
    comb = w_a * ret_a + (1.0 - w_a) * ret_b
    if vol_target:
        vol = comb.ewm(halflife=VOL_HALFLIFE, adjust=False).std().shift(1)
        scale = (VOL_TARGET / (vol * np.sqrt(252))).clip(lower=0.0, upper=VOL_SCALE_CAP)
        scale = scale.fillna(1.0)
        comb = comb * scale
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
    }


# ---------------------------------------------------------------------------
# 3. exp_ret 截面 IC（独立实现）
# ---------------------------------------------------------------------------
def cs_ic_oos(sig: pd.DataFrame, realized: pd.Series, seg_start: pd.Timestamp) -> dict:
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized.reindex(sig.index)
    sig = sig.dropna(subset=["realized"])

    def _daily_ic(g: pd.DataFrame) -> float:
        if len(g) < 3:
            return np.nan
        return float(g["exp_ret"].corr(g["realized"], method="spearman"))

    ics = sig.groupby(level="ts").apply(_daily_ic).dropna()
    ics.name = "ic"

    def _summ(sub: pd.Series) -> dict:
        if len(sub) == 0:
            return {"n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
                    "icir": np.nan, "pos_ratio": np.nan}
        std = sub.std(ddof=1) if len(sub) > 1 else 0.0
        return {"n_days": int(len(sub)), "ic_mean": float(sub.mean()),
                "ic_std": float(std),
                "icir": float(sub.mean() / std) if len(sub) > 1 and std > 0 else np.nan,
                "pos_ratio": float((sub > 0).mean())}

    out = {"full": _summ(ics)}
    out["is"] = _summ(ics[ics.index < IS_END])
    out["oos"] = _summ(ics[ics.index >= seg_start])
    return out


# ---------------------------------------------------------------------------
# 4. v6 跨组尺度错配诊断（独立复算）
# ---------------------------------------------------------------------------
def group_scale_diag(sig: pd.DataFrame) -> pd.DataFrame:
    """每组 exp_ret 统计 + OOS 期间引擎 A 选中占比（验证跨组尺度错配）。"""
    from scripts.group_modeling_v2 import GROUPS_V2
    sym2group = {}
    for gname, gcfg in GROUPS_V2.items():
        for s in gcfg["syms"]:
            sym2group[s] = gname
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["group"] = sig["symbol"].map(sym2group)
    rows = []
    for gname, g in sig.groupby("group"):
        rows.append({
            "group": gname,
            "n_syms": g["symbol"].nunique(),
            "exp_ret_mean": float(g["exp_ret"].mean()),
            "exp_ret_std": float(g["exp_ret"].std()),
            "exp_ret_median": float(g["exp_ret"].median()),
        })
    return pd.DataFrame(rows)


def selection_by_group(sig: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """OOS 期间每日截面 top30% 选中品种按组分布（独立复算）。"""
    from scripts.group_modeling_v2 import GROUPS_V2
    sym2group = {}
    for gname, gcfg in GROUPS_V2.items():
        for s in gcfg["syms"]:
            sym2group[s] = gname
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig[sig["ts"] >= OOS_START]
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["selected"] = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMBOLS)
    sel = sig[sig["selected"]]
    total = len(sel)
    out = sel.groupby(sel["symbol"].map(sym2group)).size().sort_values(ascending=False)
    out = (out / total * 100).round(1)
    out.name = "sel_pct"
    return out.to_frame()


def main() -> None:
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)

    print("=" * 100)
    print("QA P8-3 独立复核（自写实现，不调用 p8_3/p5 重估函数）")
    print(f"OOS 起点 {OOS_START.date()} | 引擎 A S2 min_symbols={MIN_SYMBOLS} top_k={TOP_K} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18")
    print("=" * 100)

    # ---- 引擎 B（固定，用于组合） ----
    from scripts.p3_combo_backtest import engine_b_targets
    tgt_b = engine_b_targets(prices, 252, 0.70)
    ret_b, m_b, m_b_oos = run_backtest(cfg, cost, prices, tgt_b)
    print(f"[引擎B] Sharpe={m_b.sharpe:.3f} | OOS Sharpe={m_b_oos.sharpe:.3f}")

    rows_a, rows_combo, ic_tbl = [], [], []
    for ver in VERSIONS:
        path = ROOT / "artifacts" / f"signals_cache18_grouped_{ver}.parquet"
        sig = pd.read_parquet(path)
        # ---- 引擎 A S2 ----
        tgt = engine_a_targets_ind(sig, prices)
        ret, m, m_oos = run_backtest(cfg, cost, prices, tgt)
        tgt_pos = tgt["target"]
        total_days = pd.Index(pd.to_datetime(tgt.index.get_level_values(1).unique()))
        long_days = pd.Index(pd.to_datetime(tgt_pos[tgt_pos > 0].index.get_level_values(1).unique()))
        long_ratio = len(long_days) / len(total_days) if len(total_days) else np.nan
        rows_a.append({
            "ver": ver, "engine": "A-S2",
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "long_day_ratio": long_ratio,
        })
        print(f"[A-S2 {ver}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% MaxDD={m.max_drawdown*100:.1f}% "
              f"| OOS Sharpe={m_oos.sharpe:.3f} OOS MaxDD={m_oos.max_drawdown*100:.1f}% | 做多占比={long_ratio*100:.1f}%")
        # ---- 组合 A15/B85 ----
        for vt in (False, True):
            ra = ret.loc[ret.index.intersection(ret_b.index)]
            rb = ret_b.loc[ret.index.intersection(ret_b.index)]
            r = combo_stats_ind(ra, rb, PROD_W_A, vt)
            rows_combo.append({"ver": ver, "engine": f"A{PROD_W_A}/B{PROD_W_B}",
                               "vol_target": vt, **r})
            print(f"  [combo {ver} A{PROD_W_A}/B{PROD_W_B} vol={'Y' if vt else 'N'}] "
                  f"Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f}")
        # ---- exp_ret 截面 IC ----
        summ = cs_ic_oos(sig, realized, OOS_START)
        for seg in ("full", "is", "oos"):
            st = summ[seg]
            ic_tbl.append({"ver": ver, "segment": seg, **st})
        print(f"  [{ver}] exp_ret 截面IC: full={summ['full']['ic_mean']:+.4f} "
              f"is={summ['is']['ic_mean']:+.4f} oos={summ['oos']['ic_mean']:+.4f}")

    print("-" * 100)
    print("[诊断] v6/v7 跨组尺度（exp_ret 组均值/标准差）")
    for ver in VERSIONS:
        sig = pd.read_parquet(ROOT / "artifacts" / f"signals_cache18_grouped_{ver}.parquet")
        g = group_scale_diag(sig)
        print(f"--- {ver} ---")
        print(g.round(4).to_string(index=False))
    print("-" * 100)
    print("[诊断] OOS 期间引擎 A 选中品种按组占比（top30%）")
    for ver in VERSIONS:
        sig = pd.read_parquet(ROOT / "artifacts" / f"signals_cache18_grouped_{ver}.parquet")
        sel = selection_by_group(sig, prices)
        print(f"--- {ver} ---")
        print(sel.to_string())

    # ---- 汇总 ----
    df_a = pd.DataFrame(rows_a)
    df_combo = pd.DataFrame(rows_combo)
    df_ic = pd.DataFrame(ic_tbl)
    print("=" * 100)
    print("引擎 A S2 汇总（独立复算 vs 工程师声明）")
    print(df_a[["ver", "sharpe_full", "maxdd_full", "oos_sharpe", "oos_maxdd", "long_day_ratio"]].round(3).to_string(index=False))
    print("组合 A15/B85 OOS（独立复算）")
    print(df_combo[["ver", "vol_target", "oos_sharpe", "oos_maxdd"]].round(3).to_string(index=False))
    print("exp_ret 截面 IC（独立复算）")
    print(df_ic.pivot_table(index="ver", columns="segment", values="ic_mean").round(4).to_string())


if __name__ == "__main__":
    main()
