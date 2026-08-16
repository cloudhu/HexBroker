"""S1–S5 卖出信号引擎（§3.4）。

依据持仓状态与行情上下文检测五类风控卖出信号：
- S1 趋势破坏：价格跌破 N 日均线。
- S2 量价背离：放量但价格走平/下跌。
- S3 目标达成：浮盈达到目标比例（止盈）。
- S4 时间止损：持仓超过阈值根数仍未盈利。
- S5 波动异常：单根收益 z 分数超过阈值。

所有阈值由 ``RiskConfig`` 提供，缺省为冻结友好值。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..constants import SellSignalCode
from .types import RiskState


def detect_sell_signals(
    state: RiskState,
    recent_returns: np.ndarray,  # 最近若干根的单根收益（用于 S5）
    recent_volumes: np.ndarray,  # 最近若干根成交量
    ma_price: Optional[float] = None,  # 当前均线价（用于 S1）
    cfg: Optional[object] = None,
) -> list[SellSignalCode]:
    """返回当前触发的卖出信号列表（可能为空）。"""
    signals: list[SellSignalCode] = []
    if state.position == 0:
        return signals

    # 读取阈值（优先 cfg，否则冻结默认）
    s1_window = getattr(cfg, "sell_s1_window", 20) if cfg else 20
    s3_target = getattr(cfg, "sell_s3_target", 0.10) if cfg else 0.10
    s4_bars = getattr(cfg, "sell_s4_bars", 20) if cfg else 20
    s5_z = getattr(cfg, "sell_s5_z", 3.0) if cfg else 3.0

    # S1 趋势破坏：价格 < 均线（多头）或 > 均线（空头）
    if ma_price is not None and state.current_price > 0:
        if state.position > 0 and state.current_price < ma_price:
            signals.append(SellSignalCode.S1_TREND_BREAK)
        elif state.position < 0 and state.current_price > ma_price:
            signals.append(SellSignalCode.S1_TREND_BREAK)

    # S2 量价背离：最新成交量显著放大但价格未创新高（多头情景）
    if len(recent_volumes) >= 2:
        vol_ratio = recent_volumes[-1] / max(recent_volumes[:-1].mean(), 1e-9)
        price_up = recent_returns[-1] > 0
        if vol_ratio > 2.0 and not price_up:
            signals.append(SellSignalCode.S2_VOL_DIVERGENCE)

    # S3 目标达成（止盈）：浮盈达到目标
    if state.pnl_pct >= s3_target:
        signals.append(SellSignalCode.S3_TARGET_REACHED)

    # S4 时间止损：持仓过久且未盈利
    if state.bars_in_position >= s4_bars and state.pnl_pct <= 0.0:
        signals.append(SellSignalCode.S4_TIME_STOP)

    # S5 波动异常：最新单根收益 z 分数超阈值
    if len(recent_returns) >= 5:
        r = recent_returns[-1]
        mu = recent_returns[-s4_bars:].mean()
        sd = recent_returns[-s4_bars:].std()
        if sd > 1e-9 and abs((r - mu) / sd) > s5_z:
            signals.append(SellSignalCode.S5_VOLATILITY_SPIKE)

    return signals


# 优先级：数字越小越优先（用于排序/取最强信号）
SELL_PRIORITY = {
    SellSignalCode.S1_TREND_BREAK: 1,
    SellSignalCode.S2_VOL_DIVERGENCE: 2,
    SellSignalCode.S3_TARGET_REACHED: 3,
    SellSignalCode.S4_TIME_STOP: 4,
    SellSignalCode.S5_VOLATILITY_SPIKE: 5,
}


def strongest(signals: list[SellSignalCode]) -> Optional[SellSignalCode]:
    """返回优先级最高的卖出信号（数值最小）。"""
    if not signals:
        return None
    return min(signals, key=lambda s: SELL_PRIORITY.get(s, 99))
