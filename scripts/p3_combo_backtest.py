"""P3 双引擎组合：趋势引擎(v2.1) + 基差引擎(B) 低相关组合验证。

步骤：
  1. 主引擎：exp_ret top30% 做多（v2.1 配置，信号缓存 signals_cache18_grouped_v2）
  2. 引擎 B：basis_ratio 滚动分位 thr=0.7 做多（win=252）
  3. 各自 BacktestEngine 完整口径回测 → 日收益序列
  4. 相关性 + 组合（等权 / 波动率加权）→ 全样本 + OOS 指标

用法：
  python scripts/p3_combo_backtest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.engine import BacktestEngine
from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices, load_basis_panel

TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
OOS_START = "2024-07-18"


def engine_a_targets(prices: pd.DataFrame) -> pd.DataFrame:
    """主引擎：exp_ret top30% 做多（信号缓存）。"""
    sig = pd.read_parquet("artifacts/signals_cache18_grouped_v2.parquet")
    sig["ts"] = pd.to_datetime(sig["ts"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sig["rank_pct"] = sig["exp_ret"].rank(pct=True)
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(mult_map)
    sig["target"] = np.where(
        (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna(),
        (notional / (sig["_px"] * sig["_mult"])).astype(int),
        0,
    )
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def engine_b_targets(prices: pd.DataFrame, win: int = 252, thr: float = 0.70) -> pd.DataFrame:
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


def run_engine(cfg, cost, prices, targets, label: str) -> pd.Series:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos, freq="daily") if len(oos) > 30 else None
    oos_sh = m_oos.sharpe if m_oos else float("nan")
    oos_dd = m_oos.max_drawdown if m_oos else float("nan")
    print(f"  [{label}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% MaxDD={m.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={oos_sh:.3f} OOS MaxDD={oos_dd*100:.1f}% "
          f"| 最终={pf.final_equity:,.0f}")
    return ret


def combo_stats(ret_a: pd.Series, ret_b: pd.Series, w_a: float, label: str) -> None:
    comb = w_a * ret_a + (1 - w_a) * ret_b
    eq = (1 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos, freq="daily") if len(oos) > 30 else None
    oos_sh = m_oos.sharpe if m_oos else float("nan")
    oos_dd = m_oos.max_drawdown if m_oos else float("nan")
    print(f"  [{label}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% MaxDD={m.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={oos_sh:.3f} OOS MaxDD={oos_dd*100:.1f}%")


def main() -> None:
    print("=" * 88)
    print("P3 双引擎组合验证：趋势引擎(v2.1 top30%) + 基差引擎(B win252/thr0.7)")
    print("=" * 88)
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    print("\n--- 单引擎回测 ---")
    tgt_a = engine_a_targets(prices)
    ret_a = run_engine(cfg, cost, prices, tgt_a, "引擎A 趋势")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine(cfg, cost, prices, tgt_b, "引擎B 基差")

    # 对齐到共同交易日
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a[common], ret_b[common]
    corr = ra.corr(rb)
    print(f"\n--- 相关性 ---")
    print(f"  共同交易日: {len(common)} | 日收益相关性: {corr:.3f}")

    # 组合
    print("\n--- 组合 ---")
    combo_stats(ra, rb, 0.5, "组合 等权50/50")
    combo_stats(ra, rb, 0.7, "组合 A70/B30")
    combo_stats(ra, rb, 0.3, "组合 A30/B70")
    # 波动率倒数加权
    va, vb = ra.std(), rb.std()
    w_a_vol = (1 / va) / (1 / va + 1 / vb)
    combo_stats(ra, rb, w_a_vol, f"组合 波动率加权(A={w_a_vol:.0%})")

    # 保存
    out = pd.DataFrame({"ret_a": ra, "ret_b": rb})
    out.to_parquet("artifacts/p3_combo_returns.parquet")
    print("\n[OK] 结果 → artifacts/p3_combo_returns.parquet")


if __name__ == "__main__":
    main()
