"""P1-1 多信号融合：截面排序（exp_ret）+ 时间序列动量（趋势跟踪）。

动机：OOS 段截面信号间歇性失效（价差 35.7% 负），而贵金属单边牛市
（ag0 +1.159%/5日）是趋势跟踪能捕获的——两个信号正交，融合应互补。

信号构造（严格因果 ≤t，只用当日及之前的 close）：
  - exp_ret：LightGBM 截面排序（已有）
  - mom_N：close / close.shift(N) - 1（N=20/60 日时间序列动量）
  - 融合分 = w1 * rank(exp_ret) + w2 * rank(mom_N)（截面 rank 归一化）

配置对比（全样本 + OOS 段，BacktestEngine 完整口径，均叠 P0-1 监控 W=20 step）：
  C: exp_ret top30%（基线，当前最优 0.48）
  M1: 纯动量 top30%（动量单独有效性）
  M2: exp_ret top30% + 动量确认（mom_20 > 0 才做多）
  M3: 加权融合 top30%（w1=w2=0.5，mom_20）
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
from scripts.combo_validation import OOS_START
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals, scale_for

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)


def build_momentum(sig: pd.DataFrame, prices: pd.DataFrame, N: int) -> pd.DataFrame:
    """为每个信号行附加时间序列动量 mom_N（严格因果：只用 ≤ts 的 close）。"""
    closes = {sym: prices.xs(sym, level=0)["close"].sort_index() for sym in sig["symbol"].unique()}
    close_df = pd.DataFrame(closes).sort_index()
    mom = close_df / close_df.shift(N) - 1.0
    df = sig.copy()
    df["mom"] = df.apply(
        lambda r: float(mom.loc[r["ts"], r["symbol"]]) if r["ts"] in mom.index and r["symbol"] in mom.columns else np.nan,
        axis=1,
    )
    return df


def run_fusion(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
               mode: str, w1: float = 0.5, w2: float = 0.5, label: str = "") -> None:
    df = sig.copy()
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()

    if mode == "exp_ret":
        df["score"] = df["exp_ret"].rank(pct=True)
    elif mode == "momentum":
        df["score"] = df["mom"].rank(pct=True)
    elif mode == "confirm":
        # exp_ret top30% 且动量确认（mom>0）
        df["score"] = df["exp_ret"].rank(pct=True)
        df["_mom_pos"] = df["mom"] > 0
    elif mode == "fusion":
        r1 = df["exp_ret"].rank(pct=True)
        r2 = df["mom"].rank(pct=True)
        df["score"] = w1 * r1 + w2 * r2

    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    if mode == "confirm":
        long_flag = (df["score"] >= 0.7) & ~px_missing & df["_mom_pos"]
    else:
        long_flag = (df["score"] >= 0.7) & ~px_missing

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


def main() -> None:
    print("=" * 72)
    print("P1-1 多信号融合：截面排序 + 时间序列动量（均叠 W=20 监控）")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    print(f"[OK] 信号 {len(sig)} 条")

    # 动量信号（20/60 日）
    sig20 = build_momentum(sig, prices, 20)
    sig60 = build_momentum(sig, prices, 60)
    cov20 = sig20["mom"].notna().mean()
    print(f"[OK] mom20 覆盖率 {cov20*100:.0f}% | mom60 覆盖率 {sig60['mom'].notna().mean()*100:.0f}%")

    # 监控（W=20 step）
    spreads = build_rolling_spread(sig, prices, window=20)
    scale = scale_for(spreads, "step", 0.0)

    oos20 = sig20[sig20["ts"] >= OOS_START].copy()
    oos60 = sig60[sig60["ts"] >= OOS_START].copy()

    print("\n=== 全样本（2018~2026-08） ===")
    run_fusion(sig20, prices, scale, "exp_ret", label="C exp_ret top30%")
    run_fusion(sig20, prices, scale, "momentum", label="M1 纯动量 top30%")
    run_fusion(sig20, prices, scale, "confirm", label="M2 exp_ret+动量确认")
    run_fusion(sig20, prices, scale, "fusion", label="M3 加权融合(mom20)")

    print("\n=== OOS 段（2024-07-18~2026-06，真新数据） ===")
    run_fusion(oos20, prices, scale, "exp_ret", label="C exp_ret top30%")
    run_fusion(oos20, prices, scale, "momentum", label="M1 纯动量 top30%")
    run_fusion(oos20, prices, scale, "confirm", label="M2 exp_ret+动量确认")
    run_fusion(oos20, prices, scale, "fusion", label="M3 加权融合(mom20)")
    # mom60 融合敏感性
    print("\n=== mom60 敏感性（OOS） ===")
    run_fusion(oos60, prices, scale, "momentum", label="M1 纯动量60 top30%")
    run_fusion(oos60, prices, scale, "fusion", label="M3 加权融合(mom60)")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/multi-signal-fusion-2026-08-17.md")


if __name__ == "__main__":
    main()
