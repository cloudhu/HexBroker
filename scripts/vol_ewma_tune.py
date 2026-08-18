"""波动率 EWMA 半衰加权微调（2026-08-18）：在采纳配置 v2.0 上对比等权20日 vs EWMA 波动率目标。

变体：
  V0  等权 20 日（当前采纳基准）
  V1  EWMA halflife=5
  V2  EWMA halflife=10
  V3  EWMA halflife=20
  V4  EWMA halflife=30

目标年化波动 17.5%，scale = clip(target/realized, 0, 1.5)。严格因果、OOS 留出、BacktestEngine 完整口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.combo_validation import OOS_START
from scripts.drawdown_control import (
    COST, DROP_P0, INIT_CAP, base_targets, load_prices18, load_signals, run_bt,
)

TARGET_VOL = 0.175


def apply_vol_target_ewma(targets: pd.DataFrame, closes: dict[str, pd.Series],
                          halflife: float | None, min_periods: int = 20) -> pd.DataFrame:
    """波动率目标：组合 EWMA（halflife 半衰）滚动年化波动 → scale = clip(target/realized, 0, 1.5)。

    halflife=None 时为等权 rolling(20)（V0 对照）。
    """
    all_closes = pd.DataFrame(closes).sort_index()
    ret = all_closes.pct_change()
    if halflife is None:
        roll_vol = ret.mean(axis=1).rolling(min_periods, min_periods=min_periods).std() * np.sqrt(252)
    else:
        roll_vol = ret.mean(axis=1).ewm(halflife=halflife, min_periods=min_periods).std() * np.sqrt(252)
    scale = (TARGET_VOL / roll_vol).clip(0.0, 1.5).fillna(1.0)
    tg = targets["target"].copy()
    for (sym, d) in tg.index:
        if d in scale.index and pd.notna(scale.loc[d]):
            tg.loc[(sym, d)] = int(tg.loc[(sym, d)] * scale.loc[d])
    return pd.DataFrame({"target": tg}).sort_index()


def main() -> None:
    print("=" * 96)
    print(f"波动率 EWMA 半衰微调：目标年化波动 {TARGET_VOL*100:.1f}%，等权 vs EWMA(hl=5/10/20/30)")
    print("=" * 96)
    sig = load_signals()
    prices, close_by = load_prices18()
    closes18 = {sym: close_by[sym] for sym in SYMBOLS18}
    base = base_targets(sig, prices, pd.DataFrame(closes18))
    closes17 = {sym: close_by[sym] for sym in DROP_P0}
    print(f"[OK] 基础 target {len(base)} 条（剔 p0 17 品种）")

    run_bt(base, prices, "A无波目")  # 参照：无波动率目标
    tg = apply_vol_target_ewma(base.copy(), closes17, None)
    run_bt(tg, prices, "V0等权20")
    for hl in (5, 10, 20, 30):
        tg = apply_vol_target_ewma(base.copy(), closes17, hl)
        run_bt(tg, prices, f"V{hl}EWMA{hl}")
    # 精调：hl=10 的邻域（8/12）
    for hl in (8, 12):
        tg = apply_vol_target_ewma(base.copy(), closes17, hl)
        run_bt(tg, prices, f"V{hl}EWMA{hl}")


if __name__ == "__main__":
    main()
