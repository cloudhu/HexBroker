"""风控层数据类型（§3.4 数据契约）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from ..constants import RecoveryStage, SellSignalCode, SignalDirection


class ATRTier(IntEnum):
    """ATR 三档止损距离倍率（索引越大倍率越小 = 止损越窄）。

    约束：一旦因波动收紧（索引增大），**只增不减**（ratchet）。
    """

    HIGH = 0  # 2.5σ 宽止损（最保守）
    MID = 1  # 2.0σ
    LOW = 2  # 1.5σ 窄止损

    @property
    def multiplier(self) -> float:
        return {0: 2.5, 1: 2.0, 2: 1.5}[self.value]


@dataclass
class RiskState:
    """风控决策所需的实时状态（由组合/回测引擎每根 bar 注入）。"""

    equity: float = 0.0
    peak_equity: float = 0.0
    position: float = 0.0  # 当前仓位比例 [-1, 1]
    entry_price: float = 0.0
    current_price: float = 0.0
    atr: float = 0.0
    realized_vol: float = 0.0  # 已实现波动率（年化/区间化标量）
    bars_in_position: int = 0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
    atr_tier: ATRTier = ATRTier.HIGH
    pnl_pct: float = 0.0  # 当前持仓浮动盈亏比例
    drawdown: float = 0.0  # 当前回撤比例 [0,1]
    # 波动分位（用于 ATR 档位切换），由引擎外部提供或内部估计
    vol_quantile: float = 0.5

    @property
    def drawdown_pct(self) -> float:
        return self.drawdown


@dataclass
class RiskDecision:
    """风控最终决策。"""

    target_position: float = 0.0  # 目标仓位比例 [-1, 1]
    liquidate: bool = False
    stop_price: Optional[float] = None
    reason: str = "rl_intent"
    stage: RecoveryStage = RecoveryStage.R0_NORMAL
    atr_tier: ATRTier = ATRTier.HIGH
    sell_signals: list = field(default_factory=list)
    kelly_fraction: float = 0.0

    def effective_direction(self) -> SignalDirection:
        if self.liquidate:
            return SignalDirection.FLAT
        if self.target_position > 0:
            return SignalDirection.LONG
        if self.target_position < 0:
            return SignalDirection.SHORT
        return SignalDirection.FLAT
