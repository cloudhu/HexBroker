"""风控层（§2.2 L6 / §3.4）。

优先级链（红线）：硬止损 > S1–S5 卖出信号 > 风险预算 > 回撤恢复 R1–R4 > RL 意图。
ATR 三档止损只增不减（ratchet），保证持仓期间止损距离不会收窄。

P1-8：风控规则接口化（``RiskRule`` / ``RiskContext`` / ``build_default_rules``）
+ 组合目标合并层（``ComboTargetMerger``）。
"""

from .combo import ComboTargetMerger, EngineTarget
from .manager import RiskManager
from .rules import (
    RiskAdjustment,
    RiskContext,
    RiskRule,
    build_default_rules,
)
from .types import ATRTier, RiskDecision, RiskState

__all__ = [
    "RiskManager",
    "RiskRule",
    "RiskContext",
    "RiskAdjustment",
    "build_default_rules",
    "ComboTargetMerger",
    "EngineTarget",
    "RiskDecision",
    "RiskState",
    "ATRTier",
]
