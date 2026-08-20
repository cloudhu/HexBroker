"""QA 独立复核：P6-3b v4 信号缓存 + 引擎 A 重估（fresh-eyes，不调用 p5 函数）。

独立实现（对齐口径但代码独立）：
  1. engine_a_targets_cs_ind：按日截面 exp_ret.rank(pct=True) + min_symbols 过滤 + 价格对齐
  2. engine_b_targets_ind：basis_ratio 品种内滚动 252 分位 thr=0.7 做多
  3. 完整回测：BacktestEngine + CostModel（滑点1tick + 费0.005% + 保证金12% + CONTRACTS18）
  4. 组合 A15/B85（vol_target=False）+ 归因（OOS 截断 <=2026-06-11）

口径常量与 p5/p6_3b 一致：TOP_K=0.30, NOTIONAL_FRAC=0.20, INITIAL_CAPITAL=1e6,
OOS_START=2024-07-18, MIN_SYMBOLS_S2=3, 引擎B win=252/thr=0.70。
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
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices, load_basis_panel

TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
OOS_START = "2024-07-18"
MIN_SYMBOLS_S2 = 3
V2_TAIL_CUT = "2026-06-11"
V2_PATH = ROOT / "artifacts" / "signals_cache18_grouped_v2.parquet"
V4_PATH = ROOT / "artifacts" / "signals_cache18_grouped_v4.parquet"


# ---------------------------------------------------------------------------
# 独立实现：引擎 A（按日截面 rank）
# ---------------------------------------------------------------------------
def engine_a_targets_cs_ind(prices: pd.DataFrame, top_k: float, min_symbols: int | None,
                            cache_path: Path, ts_cap: str | None = None) -> pd.DataFrame:
    """按日横截面 exp_ret rank top_k 做多；min_symbols 过滤；可选 ts_cap 截断缓存。"""
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    if ts_cap is not None:
        sig = sig[sig["ts"] <= pd.Timestamp(ts_cap)]
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC

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


# ---------------------------------------------------------------------------
# 独立实现：引擎 B（基差收敛 win252/thr0.7）
# ---------------------------------------------------------------------------
def engine_b_targets_ind(prices: pd.DataFrame, win: int = 252, thr: float = 0.70) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 回测辅助（独立实现，等价 run_engine_row）
# ---------------------------------------------------------------------------
def run_bt(cfg, cost, prices, targets) -> tuple[pd.Series, pd.Series]:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    return ret, eq


def oos_metrics(eq: pd.Series, cap: str | None = None):
    idx = pd.to_datetime(eq.index)
    sel = idx >= pd.Timestamp(OOS_START)
    if cap is not None:
        sel &= idx <= pd.Timestamp(cap)
    sub = eq[sel]
    if len(sub) > 30:
        m = compute_metrics(sub, freq="daily")
        return m.sharpe, m.max_drawdown, m.total_return, len(sub)
    return np.nan, np.nan, np.nan, len(sub)


def combo_stats_ind(ret_a: pd.Series, ret_b: pd.Series, w_a: float, vol_target: bool = False):
    common = ret_a.index.intersection(ret_b.index)
    ra = ret_a.loc[common].sort_index()
    rb = ret_b.loc[common].sort_index()
    comb = w_a * ra + (1.0 - w_a) * rb
    if vol_target:
        vol = comb.ewm(halflife=10, adjust=False).std().shift(1)
        scale = (0.175 / (vol * np.sqrt(252))).clip(lower=0.0, upper=1.5).fillna(1.0)
        comb = comb * scale
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return, "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
    }


def main() -> None:
    print("=" * 96)
    print("QA 独立复核：P6-3b v4 缓存 + 引擎 A 重估（fresh-eyes，独立实现不调用 p5 函数）")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[env] prices: {len(prices)} 行 | 口径: 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"IC={INITIAL_CAPITAL:,.0f} | top_k={TOP_K} | notional={NOTIONAL_FRAC*100:.0f}%")

    # ---- 引擎 B（缓存无关） ----
    tgt_b = engine_b_targets_ind(prices, BASIS_WIN, BASIS_THR)
    ret_b, eq_b = run_bt(cfg, cost, prices, tgt_b)
    sh, dd, tr, n = oos_metrics(eq_b)
    print(f"\n[引擎B 独立] 全样本 Sharpe={compute_metrics(eq_b, freq='daily').sharpe:.3f} | "
          f"OOS Sharpe={sh:.3f} OOS MaxDD={dd*100:.1f}% OOS ret={tr*100:+.1f}% (n={n})")

    # ---- 引擎 A S2（min=3）独立复现 v2/v4 ----
    print("\n[引擎A S2 min=3 独立复现]")
    rows = []
    for label, path in (("v2", V2_PATH), ("v4", V4_PATH)):
        tgt = engine_a_targets_cs_ind(prices, TOP_K, MIN_SYMBOLS_S2, path)
        ret, eq = run_bt(cfg, cost, prices, tgt)
        m = compute_metrics(eq, freq="daily")
        sh, dd, tr, n = oos_metrics(eq)
        # 截断归因：OOS <= 2026-06-11
        sh_cap, dd_cap, tr_cap, n_cap = oos_metrics(eq, cap=V2_TAIL_CUT)
        n_long = int((tgt["target"] > 0).sum())
        print(f"  [{label}] 全样本 Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={sh:.3f} OOS MaxDD={dd*100:.1f}% "
              f"OOS ret={tr*100:+.1f}% (n={n}) | 做多行={n_long}")
        print(f"          归因(OOS<=2026-06-11): Sharpe={sh_cap:.3f} MaxDD={dd_cap*100:.1f}% "
              f"ret={tr_cap*100:+.1f}% (n={n_cap})")
        rows.append({"cache": label, "oos_sharpe": sh, "oos_cap_sharpe": sh_cap, "n_long": n_long})
    d = rows[1]["oos_sharpe"] - rows[0]["oos_sharpe"]
    d_cap = rows[1]["oos_cap_sharpe"] - rows[0]["oos_cap_sharpe"]
    print(f"  ΔOOS Sharpe v4-v2 = {d:+.3f} | ΔOOS(截断<=06-11) = {d_cap:+.3f}")

    # ---- 组合 A15/B85（vol_target=False） ----
    print("\n[组合 独立复现] A15/B85 与 A25/B75（引擎B 固定 win252/thr0.7, vol_target=False）")
    for label, path in (("v2", V2_PATH), ("v4", V4_PATH)):
        tgt_a = engine_a_targets_cs_ind(prices, TOP_K, MIN_SYMBOLS_S2, path)
        ret_a, _ = run_bt(cfg, cost, prices, tgt_a)
        for w_a, wname in ((0.15, "A15B85"), (0.25, "A25B75")):
            r = combo_stats_ind(ret_a, ret_b, w_a, vol_target=False)
            print(f"  [{label}] {wname}: 全样本 Sharpe={r['sharpe_full']:.3f} | "
                  f"OOS Sharpe={r['oos_sharpe']:.3f} OOS MaxDD={r['oos_maxdd']*100:.1f}% "
                  f"OOS ret={r['oos_ret']*100:+.1f}% (n={r['oos_n']})")

    # ---- 纯 B 参考 ----
    r_pure = combo_stats_ind(ret_b, ret_b, 1.0, vol_target=False)
    print(f"\n[纯B 参考] 全样本 Sharpe={r_pure['sharpe_full']:.3f} | "
          f"OOS Sharpe={r_pure['oos_sharpe']:.3f} OOS ret={r_pure['oos_ret']*100:+.1f}% (n={r_pure['oos_n']})")

    print("\n[DONE] QA 独立复核完成")


if __name__ == "__main__":
    main()
