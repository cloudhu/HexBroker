"""QA P19 独立交叉验证（fresh-eyes，不调用 p19_rebuild_config / _p19_ext 任何函数）。

独立实现（自写）：
  1. 引擎 A 截面选择（v8 缓存 + 每日 rank + S2 min_symbols=3 + group_cap=0.5 ferrous_all）
  2. 引擎 B targets（basis_ratio 滚动分位 win252/thr0.7，显式名义）
  3. BacktestEngine（修复后 broker，CONTRACTS18 + INITIAL_CAPITAL=1e6 + CostModel）
  4. 组合：研究口径（200k/引擎 + 权重）与生产口径（A/B 有效名义 + 无权重求和）
  5. 对照：P18 无 cap 的 A0.35/B0.65

口径铁律：复利；OOS 2024-07-18 后；完整回测；与 p19 相同的 seg 分段。
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

INITIAL_CAPITAL = 1_000_000.0
OOS_START = "2024-07-18"
TOP_K = 0.30
MIN_SYMBOLS = 3
GROUP_CAP = 0.5
V8_PATH = ROOT / "artifacts" / "signals_cache18_grouped_v8.parquet"
FUND_DIR = ROOT / "data" / "raw" / "fundamental"
OOS_SUB = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]

GROUP_MAP: dict[str, str] = {
    "i0": "ferrous_all", "j0": "ferrous_all", "jm0": "ferrous_all",
    "rb0": "ferrous_all", "hc0": "ferrous_all",
    "cu0": "industrial", "al0": "industrial", "zn0": "industrial", "ni0": "industrial",
    "au0": "precious", "ag0": "precious",
    "y0": "agri_oil", "p0": "agri_oil",
    "m0": "agri_protein",
    "sr0": "agri_soft", "cf0": "agri_soft",
    "ta0": "chem_energy", "sc0": "chem_energy",
}


# ---------------------------------------------------------------------------
# 数据加载（独立实现，不调 p2/p3 的加载函数）
# ---------------------------------------------------------------------------
def load_prices() -> pd.DataFrame:
    import glob
    parts = []
    for sym in SYMBOLS18:
        for fp in sorted(glob.glob(str(ROOT / f"data/raw/processed/{sym}/1d/*.parquet"))):
            df = pd.read_parquet(fp)
            df["datetime"] = pd.to_datetime(df["datetime"])
            parts.append(df[["symbol", "datetime", "close"]])
    prices = pd.concat(parts, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
    return prices.loc[~prices.index.duplicated(keep="last")]


def load_basis_panel() -> pd.DataFrame:
    rows = []
    for sym in SYMBOLS18:
        sym_u = sym[:-1].upper()
        f = FUND_DIR / f"basis_{sym_u}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df["datetime"] = pd.to_datetime(df["date"])
        df["symbol"] = sym
        rows.append(df[["symbol", "datetime", "basis_ratio"]])
    return pd.concat(rows, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()


# ---------------------------------------------------------------------------
# 引擎 A（独立实现：每日截面 rank + S2 + cap0.5 替补逻辑，与 p5 语义一致）
# ---------------------------------------------------------------------------
def _capped_selection(ranked, target_count, cap, group_map):
    """按 rank 降序施加单组敞口上限：剔除超限组尾部，再从其余候选替补。"""
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts: dict[str, int] = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g, n, total):
        return total > 0 and n / total > cap

    changed = True
    while changed and len(selected) > 1:
        changed = False
        for i in range(len(selected) - 1, -1, -1):
            g = group_map.get(selected[i], "other")
            if over_cap(g, counts[g], len(selected)) and counts[g] >= 2:
                counts[g] -= 1
                selected.pop(i)
                changed = True
                break

    sel_set = set(selected)
    for s in ranked:
        if len(selected) >= target_count:
            break
        if s in sel_set:
            continue
        g = group_map.get(s, "other")
        if (counts.get(g, 0) + 1) / (len(selected) + 1) <= cap:
            selected.append(s)
            counts[g] = counts.get(g, 0) + 1
            sel_set.add(s)
    return selected


def engine_a_selection_ind(prices: pd.DataFrame, group_cap=GROUP_CAP) -> pd.DataFrame:
    """独立版引擎 A 选中明细（每日截面 top30% + S2 + group_cap）。"""
    sig = pd.read_parquet(V8_PATH)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18})
    sig["group"] = sig["symbol"].map(lambda s: GROUP_MAP.get(s, "other"))

    elig_mask = sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMBOLS)
    sig["selected"] = False
    elig = sig[elig_mask]
    for _ts, g in elig.groupby("ts"):
        g = g.sort_values("exp_ret", ascending=False)
        target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
        if target_count <= 0:
            continue
        if group_cap is None:
            sel_syms = g["symbol"].tolist()[:target_count]
        else:
            sel_syms = _capped_selection(g["symbol"].tolist(), target_count, group_cap, GROUP_MAP)
        sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    return sig[["symbol", "ts", "exp_ret", "rank_pct", "_day_cnt", "_px", "_mult", "group", "selected"]]


def engine_a_targets_ind(sel: pd.DataFrame, notional: float) -> pd.DataFrame:
    """独立版引擎 A targets（显式 per-symbol 名义 → floor 手数）。"""
    s = sel.copy()
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = notional / (s["_px"] * s["_mult"])
    s["target"] = np.where(s["selected"], raw.fillna(0.0).astype(int), 0)
    return s.set_index(["symbol", "ts"])[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 引擎 B（独立实现：win252/thr0.7，显式名义）
# ---------------------------------------------------------------------------
def engine_b_targets_ind(prices: pd.DataFrame, win: int = 252, thr: float = 0.70,
                         notional: float = INITIAL_CAPITAL * 0.20) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 回测 + 指标（独立实现）
# ---------------------------------------------------------------------------
def run_engine(cfg, cost, prices, targets):
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    return eq.pct_change().dropna(), eq


def oos_metrics(eq: pd.Series) -> dict:
    idx = pd.to_datetime(eq.index)
    oos = eq[idx >= pd.Timestamp(OOS_START)]
    m = compute_metrics(oos, freq="daily") if len(oos) > 30 else None
    seg1 = _seg_sharpe(eq, OOS_SUB[0][0], OOS_SUB[0][1])
    seg2 = _seg_sharpe(eq, OOS_SUB[1][0], OOS_SUB[1][1])
    return {
        "oos_sharpe": m.sharpe if m else np.nan,
        "oos_ret": m.total_return if m else np.nan,
        "oos_maxdd": m.max_drawdown if m else np.nan,
        "oos_n": len(oos) if m else 0,
        "seg1": seg1,
        "seg2": seg2,
    }


def _seg_sharpe(eq, start, end=None):
    idx = pd.to_datetime(eq.index)
    sel = idx >= pd.Timestamp(start)
    if end is not None:
        sel &= idx < pd.Timestamp(end)
    sub = eq[sel]
    if len(sub) > 30:
        return float(compute_metrics(sub, freq="daily").sharpe)
    return float("nan")


def combo_from_returns(ret_a, ret_b, w_a, w_b):
    """研究口径：权重组合（两引擎同名义 200k）。"""
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[common].sort_index(), ret_b.loc[common].sort_index()
    comb = w_a * ra + w_b * rb
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    return eq


def combo_production(ret_a, ret_b):
    """生产口径：两引擎各自有效名义回测，日收益直接求和（各自以 1e6 为基数）。"""
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[common].sort_index(), ret_b.loc[common].sort_index()
    comb = ra + rb
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    return eq


def main() -> None:
    print("=" * 100)
    print("QA P19 独立交叉验证（不调 p19 函数；修复后 broker；复利口径）")
    print("=" * 100)
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 | v8 存在={V8_PATH.exists()} | OOS {OOS_START}")

    sel = engine_a_selection_ind(prices, group_cap=GROUP_CAP)
    print(f"[1] 引擎 A 选中行 {int(sel['selected'].sum())} / 天数 "
          f"{sel.loc[sel['selected'], 'ts'].nunique()}（cap0.5）")

    # ---- 引擎 A 名义敏感性（研究口径 200k，对照 p19_2）----
    print("\n--- 引擎 A 名义敏感性（cap0.5，独立实现）---")
    for nf in (0.20, 0.30, 0.40, 0.50):
        tgt = engine_a_targets_ind(sel, INITIAL_CAPITAL * nf)
        ret, eq = run_engine(cfg, cost, prices, tgt)
        m = oos_metrics(eq)
        print(f"  nf={nf:.2f}: OOS Sharpe={m['oos_sharpe']:.3f} OOS={m['oos_ret']*100:+.2f}% "
              f"MaxDD={m['oos_maxdd']*100:.1f}% seg={m['seg1']:.3f}/{m['seg2']:.3f}")

    # ---- 引擎 B thr 对照（独立实现，研究名义 200k）----
    print("\n--- 引擎 B thr（win126/252 × 0.60/0.70/0.80，独立实现，名义 200k）---")
    for win in (126, 252):
        for thr in (0.60, 0.70, 0.80):
            tgt = engine_b_targets_ind(prices, win, thr)
            ret, eq = run_engine(cfg, cost, prices, tgt)
            m = oos_metrics(eq)
            print(f"  win={win} thr={thr:.2f}: OOS Sharpe={m['oos_sharpe']:.3f} "
                  f"OOS={m['oos_ret']*100:+.2f}% MaxDD={m['oos_maxdd']*100:.1f}%")

    # ---- 组合复验（研究口径：200k/引擎 + 权重）----
    print("\n--- 组合复验（研究口径 200k/引擎 + 权重）---")
    tgt_a20 = engine_a_targets_ind(sel, INITIAL_CAPITAL * 0.20)
    ret_a20, eq_a20 = run_engine(cfg, cost, prices, tgt_a20)
    tgt_b70 = engine_b_targets_ind(prices, 252, 0.70, INITIAL_CAPITAL * 0.20)
    ret_b70, eq_b70 = run_engine(cfg, cost, prices, tgt_b70)
    tgt_b60 = engine_b_targets_ind(prices, 252, 0.60, INITIAL_CAPITAL * 0.20)
    ret_b60, _ = run_engine(cfg, cost, prices, tgt_b60)

    print("\n  引擎 A 单引擎 OOS（研究 200k，独立）：")
    ma = oos_metrics(eq_a20)
    print(f"    A nf0.20: Sharpe={ma['oos_sharpe']:.3f} ret={ma['oos_ret']*100:+.2f}% "
          f"MaxDD={ma['oos_maxdd']*100:.1f}% seg={ma['seg1']:.3f}/{ma['seg2']:.3f}")
    mb = oos_metrics(eq_b70)
    print(f"    B thr0.70: Sharpe={mb['oos_sharpe']:.3f} ret={mb['oos_ret']*100:+.2f}% "
          f"MaxDD={mb['oos_maxdd']*100:.1f}%")

    for w_a, w_b, thr, label in [
        (0.10, 0.90, 0.70, "现生产 A10/B90 thr0.70"),
        (0.35, 0.65, 0.70, "A35/B65 thr0.70"),
        (0.50, 0.50, 0.70, "候选 A50/B50 thr0.70"),
        (0.50, 0.50, 0.60, "A50/B50 thr0.60"),
    ]:
        rb = ret_b70 if thr == 0.70 else ret_b60
        eq = combo_from_returns(ret_a20, rb, w_a, w_b)
        m = oos_metrics(eq)
        print(f"  {label}: OOS Sharpe={m['oos_sharpe']:.3f} OOS={m['oos_ret']*100:+.2f}% "
              f"MaxDD={m['oos_maxdd']*100:.1f}%")

    # ---- P18 无 cap 对照（group_cap=None）----
    print("\n--- P18 无 cap 对照（engine A group_cap=None）---")
    sel_nocap = engine_a_selection_ind(prices, group_cap=None)
    tgt_a_nocap = engine_a_targets_ind(sel_nocap, INITIAL_CAPITAL * 0.20)
    ret_a_nocap, eq_a_nocap = run_engine(cfg, cost, prices, tgt_a_nocap)
    eq_35_nocap = combo_from_returns(ret_a_nocap, ret_b70, 0.35, 0.65)
    m_nc = oos_metrics(eq_35_nocap)
    print(f"  A0.35/B0.65 无cap: OOS Sharpe={m_nc['oos_sharpe']:.3f} "
          f"OOS={m_nc['oos_ret']*100:+.2f}% MaxDD={m_nc['oos_maxdd']*100:.1f}% "
          f"(P18 声称 0.731)")

    # ---- 生产口径（A0.50/B0.50 → A 100k + B 100k；A0.35/B0.65 → A 70k + B 130k）----
    print("\n--- 生产有效名义口径（直接构建 100k/70k targets + 日收益求和）---")
    tgt_a_100k = engine_a_targets_ind(sel, INITIAL_CAPITAL * 0.20 * 0.50)
    ret_a_100k, _ = run_engine(cfg, cost, prices, tgt_a_100k)
    tgt_b_100k = engine_b_targets_ind(prices, 252, 0.70, INITIAL_CAPITAL * 0.20 * 0.50)
    ret_b_100k, _ = run_engine(cfg, cost, prices, tgt_b_100k)
    eq_5050_prod = combo_production(ret_a_100k, ret_b_100k)
    m = oos_metrics(eq_5050_prod)
    print(f"  A0.50/B0.50 生产口径 (A 100k + B 100k): OOS Sharpe={m['oos_sharpe']:.3f} "
          f"OOS={m['oos_ret']*100:+.2f}% MaxDD={m['oos_maxdd']*100:.1f}%")

    tgt_a_70k = engine_a_targets_ind(sel, INITIAL_CAPITAL * 0.20 * 0.35)
    ret_a_70k, _ = run_engine(cfg, cost, prices, tgt_a_70k)
    tgt_b_130k = engine_b_targets_ind(prices, 252, 0.70, INITIAL_CAPITAL * 0.20 * 0.65)
    ret_b_130k, _ = run_engine(cfg, cost, prices, tgt_b_130k)
    eq_3565_prod = combo_production(ret_a_70k, ret_b_130k)
    m = oos_metrics(eq_3565_prod)
    print(f"  A0.35/B0.65 生产口径 (A 70k + B 130k): OOS Sharpe={m['oos_sharpe']:.3f} "
          f"OOS={m['oos_ret']*100:+.2f}% MaxDD={m['oos_maxdd']*100:.1f}%")

    # ---- 生产 B 可交易性（A0.50/B0.50 时 B 有效名义）----
    print("\n--- 生产 B 有效名义可交易性（nf_b=0.20，OOS 最低一手 m0）---")
    px_oos = prices[pd.to_datetime(prices.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    lot_min = min(
        float(px_oos.xs(s, level=0)["close"].min() * CONTRACTS18[s]["multiplier"])
        for s in SYMBOLS18
    )
    print(f"  OOS 最低一手成本: {lot_min:,.0f} CNY")
    for w_b in (0.50, 0.40, 0.30, 0.20, 0.10):
        eff = INITIAL_CAPITAL * 0.20 * w_b
        print(f"  w_b={w_b:.2f}: eff={eff:,.0f} CNY = {int(eff//lot_min)} 手 m0")

    print("\n[DONE] QA P19 独立交叉验证完成")


if __name__ == "__main__":
    main()
