"""叠加验证：固定 top30% + MA20 趋势过滤 + P0-1 信号监控（W=20 step）。

三个已验证组件的组合：
  1. 固定 top-30% 单边多头（静态参数，P0-2 确认优于滚动重选）
  2. MA20 趋势过滤（close >= MA20 才持仓，全样本 Sharpe 0.43）
  3. 信号监控 W=20 step（滚动价差 < 0 → 空仓，全样本 Sharpe 0.48）

对比矩阵（全样本 + OOS 段，BacktestEngine 完整口径）：
  A: top30%（基线）
  B: top30% + MA20
  C: top30% + 监控
  D: top30% + MA20 + 监控（叠加，本实验）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals, scale_for

OOS_START = pd.Timestamp("2024-07-18")
COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)


def _trend_ok(close_by: dict[str, pd.Series], sym: str, ts, ma: int | None) -> bool:
    if ma is None:
        return True
    st = close_by[sym].loc[:ts]
    if len(st) < ma:
        return False
    return bool(st.iloc[-1] >= st.rolling(ma, min_periods=ma).mean().iloc[-1])


def run_combined(sig: pd.DataFrame, prices: pd.DataFrame, close_by: dict[str, pd.Series],
                 top_k: float, ma: int | None, scale: pd.Series | None,
                 label: str) -> tuple[float, float, float, int]:
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    df["_trend"] = df.apply(lambda r: _trend_ok(close_by, r["symbol"], r["ts"], ma), axis=1)
    long_flag = (df["rank_pct"] >= 1.0 - top_k) & ~px_missing & df["_trend"]
    if scale is not None:
        df["_scale"] = df["ts"].map(scale).fillna(1.0)
        df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    else:
        df["target"] = np.where(long_flag, qty, 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")
    n_active = int((df["target"] > 0).sum())
    print(f"[{label}] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} 持仓信号={n_active}")
    return m.annual_return, m.max_drawdown, m.sharpe, n_active


def main() -> None:
    print("=" * 72)
    print("叠加验证：top30% + MA20 + 信号监控（W=20 step）")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    print(f"[OK] 信号 {len(sig)} 条 | prices {len(prices)} 行")

    close_by: dict[str, pd.Series] = {}
    for sym in sig["symbol"].unique():
        close_by[sym] = prices.xs(sym, level=0)["close"].sort_index()

    # 信号监控（W=20 step）
    spreads = build_rolling_spread(sig, prices, window=20)
    scale = scale_for(spreads, "step", 0.0)
    print(f"[OK] 滚动价差覆盖 {spreads.notna().sum()} 交易日 | 负值占比 {(spreads < 0).mean()*100:.1f}%")

    oos = sig[sig["ts"] >= OOS_START].copy()

    print("\n=== 全样本（2018~2026-08） ===")
    run_combined(sig, prices, close_by, 0.30, None, None, "A top30%（基线）")
    run_combined(sig, prices, close_by, 0.30, 20, None, "B top30%+MA20")
    run_combined(sig, prices, close_by, 0.30, None, scale, "C top30%+监控")
    run_combined(sig, prices, close_by, 0.30, 20, scale, "D top30%+MA20+监控")

    print("\n=== OOS 段（2024-07-18~2026-06，真新数据） ===")
    run_combined(oos, prices, close_by, 0.30, None, None, "A top30%（基线）")
    run_combined(oos, prices, close_by, 0.30, 20, None, "B top30%+MA20")
    run_combined(oos, prices, close_by, 0.30, None, scale, "C top30%+监控")
    run_combined(oos, prices, close_by, 0.30, 20, scale, "D top30%+MA20+监控")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/combo-validation-2026-08-17.md")


if __name__ == "__main__":
    main()
