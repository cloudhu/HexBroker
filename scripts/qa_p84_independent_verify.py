"""QA P8-4 独立复核：引擎 A S2 复利口径 + 组合 A15/B85 + m0 入选率 + 选组均衡。

fresh-eyes 独立复核（严过关）：
- 不调用 p8_4/p5/p8_3 的重估函数（engine_a_targets_cs/run_engine_row/combo_stats_row/
  selection_balance 均不用）；只用 BacktestEngine/CostModel/compute_metrics 核心引擎
  与数据加载函数（load_prices/load_basis_panel/CONTRACTS18），目标构建全部自写。
- 口径与工程师完全一致：OOS 2024-07-18 后；按日截面 rank top30% + min=3；
  滑点1tick+费0.005%+保证金12%+CONTRACTS18；OOS 复利=OOS 权益段 total_return。
- 目标：复现 v4 -1.99% / v7 -1.63% / v8 +8.68%（复利口径）、组合 1.384/1.399/1.602、
  m0 入选天数占比 27.3%、最大组 <=23%。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_basis_panel, load_prices

ART = ROOT / "artifacts"
OOS_START = pd.Timestamp("2024-07-18")
TOP_K = 0.30
MIN_SYMS = 3
NOTIONAL_FRAC = 0.20
W_A = 0.15
W_B = 0.85
BASIS_WIN = 252
BASIS_THR = 0.70

CACHES = {
    "v4": ART / "signals_cache18_grouped_v4.parquet",
    "v7": ART / "signals_cache18_grouped_v7.parquet",
    "v8": ART / "signals_cache18_grouped_v8.parquet",
    "v9": ART / "signals_cache18_grouped_v9.parquet",
}

MULT = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
NOTIONAL = INITIAL_CAPITAL * NOTIONAL_FRAC


# ---------------------------------------------------------------------------
# 1. 独立引擎 A 目标构建（自写，不调用 p5/p8_4 函数）
# ---------------------------------------------------------------------------
def build_engine_a_targets(cache_path: Path, prices: pd.DataFrame) -> pd.DataFrame:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    # 每日截面 rank（ascending=True, pct → [0,1]，越大越强）
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    # 价格对齐：该品种 ts 当日的 close
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(MULT)
    long_cond = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = NOTIONAL / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(long_cond, raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 2. 独立引擎 B 目标构建（自写）
# ---------------------------------------------------------------------------
def build_engine_b_targets(prices: pd.DataFrame) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(BASIS_WIN, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= BASIS_THR,
        (NOTIONAL / (sig["_px"] * sym_level.map(MULT))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 3. 独立回测 + 指标
# ---------------------------------------------------------------------------
def run_backtest(cfg, cost, prices, targets) -> tuple[pd.Series, pd.Series, object, object]:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, eq, m, m_oos


def segment_metrics(eq: pd.Series, start: str, end: str | None = None) -> dict:
    idx = pd.to_datetime(eq.index)
    sel = idx >= pd.Timestamp(start)
    if end is not None:
        sel &= idx < pd.Timestamp(end)
    sub = eq[sel]
    if len(sub) <= 30:
        return {"n": int(len(sub)), "sharpe": np.nan, "total_return": np.nan}
    m = compute_metrics(sub, freq="daily")
    return {"n": int(len(sub)), "sharpe": float(m.sharpe), "total_return": float(m.total_return)}


def combo_stats(ret_a: pd.Series, ret_b: pd.Series, w_a: float) -> dict:
    idx = ret_a.index.intersection(ret_b.index)
    comb = w_a * ret_a.loc[idx] + (1.0 - w_a) * ret_b.loc[idx]
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    oos_eq = eq[pd.to_datetime(eq.index) >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "sharpe_full": float(m.sharpe),
        "maxdd_full": float(m.max_drawdown),
        "oos_sharpe": float(m_oos.sharpe) if m_oos else np.nan,
        "oos_ret": float(m_oos.total_return) if m_oos else np.nan,
        "oos_maxdd": float(m_oos.max_drawdown) if m_oos else np.nan,
    }


# ---------------------------------------------------------------------------
# 4. m0 入选率 / 选组均衡（独立实现）
# ---------------------------------------------------------------------------
def selection_stats(cache_path: Path, prices: pd.DataFrame) -> pd.DataFrame:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig[sig["ts"] >= OOS_START]
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["selected"] = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)

    from scripts.group_modeling_v2 import GROUPS_V2 as G2

    sym2group = {s: g for g, gc in G2.items() for s in gc["syms"]}
    sig["group"] = sig["symbol"].map(sym2group)
    sel = sig[sig["selected"]]
    n_sel = len(sel)
    n_sig = len(sig)

    rows = []
    for gname, g in sig.groupby("group"):
        rows.append({
            "group": gname,
            "n_syms": int(g["symbol"].nunique()),
            "sel_pct": float(sel[sel["group"] == gname].shape[0] / n_sel * 100) if n_sel else np.nan,
            "sig_pct": float(g.shape[0] / n_sig * 100) if n_sig else np.nan,
        })
    out = pd.DataFrame(rows).sort_values("sel_pct", ascending=False)
    # m0 专项
    m0 = sig[sig["symbol"] == "m0"]
    m0_cand = m0[m0["_day_cnt"] >= MIN_SYMS]
    m0_sel = m0_cand[m0_cand["selected"]]
    m0_share = float(sel[sel["symbol"] == "m0"].shape[0] / n_sel * 100) if n_sel else np.nan
    m0_day = float(m0_sel.shape[0] / m0_cand.shape[0] * 100) if len(m0_cand) else np.nan
    out.loc["_m0_share"] = {"group": "_m0_share", "n_syms": 1,
                            "sel_pct": round(m0_share, 2) if pd.notna(m0_share) else np.nan,
                            "sig_pct": round(float(m0.shape[0] / n_sig * 100), 2) if n_sig else np.nan}
    out.loc["_m0_day_sel_ratio"] = {"group": "_m0_day_sel_ratio", "n_syms": 1,
                                    "sel_pct": round(m0_day, 2) if pd.notna(m0_day) else np.nan,
                                    "sig_pct": np.nan}
    return out


def main() -> None:
    print("=" * 100)
    print("QA P8-4 独立复核（fresh-eyes）：引擎 A S2 复利口径 + 组合 + m0/选组均衡")
    print("口径：OOS 2024-07-18 后；按日截面 rank top30% + min=3；复利口径评估；"
          "目标构建全部自写（不调用 p8_4/p5 重估函数）")
    print("=" * 100)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[env] prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种")

    tgt_b = build_engine_b_targets(prices)
    ret_b, eq_b, m_b, m_b_oos = run_backtest(cfg, cost, prices, tgt_b)
    print(f"\n[引擎 B 基线] 全样本 Sharpe={m_b.sharpe:.3f} | OOS Sharpe={m_b_oos.sharpe:.3f}")

    rows = []
    for tag, cp in CACHES.items():
        if not cp.exists():
            continue
        tgt = build_engine_a_targets(cp, prices)
        ret, eq, m, m_oos = run_backtest(cfg, cost, prices, tgt)
        seg1 = segment_metrics(eq, "2024-07-18", "2025-06-30")
        seg2 = segment_metrics(eq, "2025-07-01", None)
        c = combo_stats(ret, ret_b, W_A)
        rows.append({
            "cache": tag,
            "A_full_sharpe": float(m.sharpe),
            "A_full_ann": float(m.annual_return),
            "A_full_maxdd": float(m.max_drawdown),
            "A_oos_sharpe": float(m_oos.sharpe) if m_oos else np.nan,
            "A_oos_compound": float(m_oos.total_return) if m_oos else np.nan,
            "A_oos_maxdd": float(m_oos.max_drawdown) if m_oos else np.nan,
            "seg1_sharpe": seg1["sharpe"], "seg1_ret": seg1["total_return"],
            "seg2_sharpe": seg2["sharpe"], "seg2_ret": seg2["total_return"],
            "combo_oos_sharpe": c["oos_sharpe"],
            "combo_oos_ret": c["oos_ret"],
            "combo_full_sharpe": c["sharpe_full"],
            "combo_full_maxdd": c["maxdd_full"],
        })
        print(f"\n[{tag}] 引擎 A S2：")
        print(f"  全样本 Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% MaxDD={m.max_drawdown*100:.1f}%")
        print(f"  OOS Sharpe={m_oos.sharpe:.3f} OOS 复利={m_oos.total_return*100:+.2f}% "
              f"OOS MaxDD={m_oos.max_drawdown*100:.1f}%")
        print(f"  OOS 分段: seg1(24H2) Sharpe={seg1['sharpe']:.3f} 复利={seg1['total_return']*100:+.2f}% | "
              f"seg2(25+) Sharpe={seg2['sharpe']:.3f} 复利={seg2['total_return']*100:+.2f}%")
        print(f"  组合 A15/B85: OOS Sharpe={c['oos_sharpe']:.3f} OOS 复利={c['oos_ret']*100:+.2f}% "
              f"| 全样本 Sharpe={c['sharpe_full']:.3f} MaxDD={c['maxdd_full']*100:.1f}%")

    tbl = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print("对比表（QA 独立复算）")
    print(tbl.round(4).to_string(index=False))
    tbl.to_csv(ART / "qa_p84_independent_verify.csv", index=False)
    print(f"[OK] → {ART / 'qa_p84_independent_verify.csv'}")

    # ---- m0 / 选组均衡 ----
    print("\n" + "=" * 100)
    print("m0 入选率 / 选组均衡（OOS，独立实现）")
    for tag, cp in CACHES.items():
        if not cp.exists():
            continue
        sb = selection_stats(cp, prices)
        m0_row = sb.loc["_m0_share"]
        m0_day = sb.loc["_m0_day_sel_ratio"]
        real = sb[~sb["group"].str.startswith("_")]
        max_grp = real.sort_values("sel_pct", ascending=False).iloc[0]
        bal_dev = float(np.abs(real["sel_pct"] - real["sig_pct"]).mean())
        n2 = real[real["n_syms"] == 2]
        two_sum = float(n2["sel_pct"].sum()) if len(n2) else np.nan
        print(f"[{tag}] m0 选中占比={m0_row['sel_pct']:.2f}% (公平 {m0_row['sig_pct']:.2f}%) | "
              f"m0 入选天数占比={m0_day['sel_pct']:.2f}% | "
              f"最大组={max_grp['group']}({max_grp['n_syms']}品种) {max_grp['sel_pct']:.2f}% | "
              f"2品种组合计={two_sum:.1f}% | 组间均衡偏差={bal_dev:.2f}pp")
        sb.insert(0, "cache", tag)
        print(sb.round(2).to_string(index=False))

    print("\n[DONE] QA 独立复核完成")


if __name__ == "__main__":
    main()
