"""名义比例放大测试：验证 Sharpe 杠杆中性，把高 Sharpe 低年化转化为目标收益。

最优配置：0.6·rank(exp_ret) + 0.4·rank(mom120), top25%, W20 监控阈 -0.2%
名义比例：30% / 50% / 70% / 100% 权益/标的（BacktestEngine 完整口径）

验证：
  1. 年化/回撤是否随名义近似线性放大；
  2. Sharpe 是否杠杆中性（基本不变）——若是，则可用名义比例直接设定目标收益；
  3. 手数取整离散化（小名义时 floor 精度损失）与保证金约束的影响。
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
from scripts.auto_iterate_search import get_mom
from scripts.combo_validation import OOS_START
from scripts.monitor_adaptive_exposure import CONTRACTS, build_rolling_spread, build_signals

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)
THR = -0.002  # 监控阈值 -0.2%


def scale_step(spreads: pd.Series, thr: float) -> pd.Series:
    sc = (spreads >= thr).astype(float)
    return sc.fillna(1.0).clip(0.0, 1.0)


def run_notional(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series,
                 notional_frac: float, label: str) -> None:
    df = sig.copy()
    df["score"] = 0.6 * df["exp_ret"].rank(pct=True) + 0.4 * df["mom"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (notional_frac * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["score"] >= 0.75) & ~px_missing
    df["_scale"] = df["ts"].map(scale).fillna(1.0)
    df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")

    oos_df = df[df["ts"] >= OOS_START].copy()
    oos_df["_px"] = oos_df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_df["_mult"] = oos_df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    op = oos_df["_px"].isna()
    oq = (notional_frac * 1_000_000.0 / (oos_df["_px"] * oos_df["_mult"])).astype(int)
    oflag = (oos_df["score"] >= 0.75) & ~op
    oos_df["_scale"] = oos_df["ts"].map(scale).fillna(1.0)
    oos_df["target"] = np.where(oflag, (oq * oos_df["_scale"]).astype(int), 0)
    otg = oos_df.set_index(["symbol", "ts"])[["target"]].sort_index()
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, otg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")

    # 保证金峰值占用估算（满仓时 = 名义×margin_rate，仅参考）
    peak_notional = 0.0
    for _, r in df.iterrows():
        if r["target"] > 0 and r["_px"] > 0:
            peak_notional = max(peak_notional, r["_px"] * r["_mult"] * abs(r["target"]))
    print(f"[{label}] 全样本: 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} | OOS: 年化={mo.annual_return*100:+.2f}% 回撤={mo.max_drawdown*100:.2f}% "
          f"Sharpe={mo.sharpe:.2f} | 峰值名义={peak_notional/1e6*100:.0f}%权益")


def main() -> None:
    print("=" * 72)
    print("名义比例放大测试（最优配置，杠杆中性验证）")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    sig = get_mom(sig, prices, 120)
    spreads = build_rolling_spread(sig, prices, window=20)
    scale = scale_step(spreads, THR)
    print(f"[OK] 信号 {len(sig)} 条 | 监控阈 {THR*100:+.1f}%")

    results = []
    print("\n=== 名义比例扫描 ===")
    for frac in (0.30, 0.50, 0.70, 1.00):
        run_notional(sig, prices, scale, frac, f"名义{frac*100:.0f}%")

    print("\n[分析] 若 Sharpe 杠杆中性，年化/回撤应随名义近似线性放大")
    print("  30% → 100% 理论倍数 3.33×；实测对照见上表")


if __name__ == "__main__":
    main()
