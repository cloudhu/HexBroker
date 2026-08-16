"""风控层（§2.2 L6 / §3.4）。

优先级链（红线）：硬止损 > S1–S5 卖出信号 > 风险预算 > 回撤恢复 R1–R4 > RL 意图。
ATR 三档止损只增不减（ratchet），保证持仓期间止损距离不会收窄。
"""

from .types import ATRTier, RiskDecision, RiskState
from .manager import RiskManager

__all__ = ["RiskManager", "RiskDecision", "RiskState", "ATRTier"]
