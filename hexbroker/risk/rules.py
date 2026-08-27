"""风控规则接口化（P1-8，§3）。

``RiskRule`` ABC + 5 个 concrete rule，各自**直接调用**既有 ``limits`` /
``sell_engine`` / ``budget`` / ``recovery`` / ``stoploss`` 函数，逻辑不重算、
口径零漂移。``build_default_rules`` 默认顺序复刻 ``RiskManager.evaluate`` 现状
代码路径（数值一致，由 A8.0 回归测试钉死）。

设计要点：
- ``RiskContext`` 为跨规则可变决策上下文；规则遍历填充它，最终由 manager 生成
  ``RiskDecision``。
- ``RiskAdjustment`` 是规则的标准化覆盖返回（target / liquidate / reason / veto）；
  需要写入上下文的其他字段（stage / kelly_fraction / sell_signals）由规则直接写入
  共享的 ``RiskContext``（其为可变上下文，属预期用法，不改判定逻辑）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ..constants import RecoveryStage, SellSignalCode
from .budget import budget_target
from .limits import HARD_STOP_DRAWDOWN, hard_stop_triggered, position_within_limit
from .recovery import recovery_scalar, recovery_stage
from .sell_engine import detect_sell_signals, strongest
from .types import ATRTier, RiskState


@dataclass
class RiskAdjustment:
    """单条规则对决策上下文的标准化覆盖。"""

    target_position: Optional[float] = None   # 设置则覆盖 running target
    liquidate: Optional[bool] = None
    reason: Optional[str] = None
    stop_price: Optional[float] = None
    veto: bool = False                         # 本规则否决后续规则（如硬止损）


@dataclass
class RiskContext:
    """跨规则可变决策上下文（由 manager 在 ``evaluate`` 中创建并驱动）。"""

    target_position: float = 0.0
    liquidate: bool = False
    reason: str = "rl_intent"
    stage: RecoveryStage = RecoveryStage.R0_NORMAL
    atr_tier: ATRTier = ATRTier.HIGH
    sell_signals: list = field(default_factory=list)
    kelly_fraction: float = 0.0
    stop_price: Optional[float] = None
    veto: bool = False


class RiskRule(ABC):
    """风控规则抽象（包裹既有函数，零重算）。"""

    name: str = "base"

    @abstractmethod
    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        """返回对决策上下文的标准化覆盖（不修改``state``）。"""


class HardStopRule(RiskRule):
    """硬止损（最高优先级）：回撤超阈值立即清仓，veto 后续规则。"""

    name = "hard_stop"

    def __init__(self, threshold: float = HARD_STOP_DRAWDOWN) -> None:
        self.threshold = float(threshold)

    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        if hard_stop_triggered(state.drawdown, self.threshold):
            return RiskAdjustment(
                target_position=0.0, liquidate=True, reason="hard_stop", veto=True
            )
        return RiskAdjustment()


class RLIntentRule(RiskRule):
    """RL 意图基线：把 running target 设为 RL 意图仓位。"""

    name = "rl_intent"

    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        return RiskAdjustment(target_position=float(intent))


class RecoveryRule(RiskRule):
    """回撤恢复分级缩放（覆盖 RL 意图强度）。"""

    name = "recovery"

    def __init__(
        self,
        dd_r1: float = 0.05,
        dd_r2: float = 0.10,
        dd_r3: float = 0.15,
        s_r1: float = 0.5,
        s_r2: float = 0.0,
        s_r3: float = 0.2,
        s_r4: float = 1.0,
    ) -> None:
        self.dd_r1 = dd_r1
        self.dd_r2 = dd_r2
        self.dd_r3 = dd_r3
        self.s_r1 = s_r1
        self.s_r2 = s_r2
        self.s_r3 = s_r3
        self.s_r4 = s_r4

    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        stage = recovery_stage(state.drawdown, self.dd_r1, self.dd_r2, self.dd_r3)
        scalar = recovery_scalar(
            stage, self.s_r1, self.s_r2, self.s_r3, self.s_r4
        )
        ctx.stage = stage  # 写入共享上下文（预期用法）
        return RiskAdjustment(target_position=float(ctx.target_position * scalar))


class BudgetRule(RiskRule):
    """风险预算：波动率目标 + Kelly 上限给出允许仓位，对 running target 封顶。"""

    name = "budget"

    def __init__(
        self,
        vol_target: float = 0.20,
        kelly_cap: float = 0.25,
        max_position_pct: float = 0.30,
    ) -> None:
        self.vol_target = vol_target
        self.kelly_cap = kelly_cap
        self.max_position_pct = max_position_pct

    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        vol = max(state.realized_vol, 1e-6)
        budget = budget_target(
            p_up, vol, self.vol_target, self.kelly_cap, self.max_position_pct
        )
        ctx.kelly_fraction = float(budget)  # 写入共享上下文
        cap = min(abs(budget), self.max_position_pct)
        new_target = position_within_limit(ctx.target_position, cap)
        return RiskAdjustment(target_position=float(new_target))


class SellEngineRule(RiskRule):
    """S1–S5 卖出信号：有信号则 targets=0（覆盖 RL 意图与预算）。"""

    name = "sell_engine"

    def __init__(self, cfg: Any = None) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        ctx: RiskContext,
        state: RiskState,
        intent: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskAdjustment:
        signals = detect_sell_signals(
            state,
            recent_returns if recent_returns is not None else np.array([]),
            recent_volumes if recent_volumes is not None else np.array([]),
            ma_price,
            self.cfg,
        )
        if signals:
            ctx.sell_signals = list(signals)  # 写入共享上下文
            ctx.reason = strongest(signals).value  # 写入共享上下文
            if state.position != 0 or ctx.target_position != 0.0:
                return RiskAdjustment(
                    target_position=0.0, liquidate=bool(state.position != 0)
                )
        return RiskAdjustment()


def build_default_rules(
    cfg: Any, hard_stop: Optional[float] = None
) -> list[RiskRule]:
    """默认规则顺序 = 复刻 ``RiskManager.evaluate`` 现状代码路径（数值一致）。

    顺序：硬止损 → RL 意图 → 回撤恢复 → 风险预算 → S1–S5 卖出信号。
    ``cfg.risk`` 缺失时回退冻结默认阈值。
    """
    risk = getattr(cfg, "risk", None) if cfg is not None else None

    def g(name: str, default: float) -> float:
        return getattr(risk, name, default) if risk is not None else default

    hs = hard_stop if hard_stop is not None else HARD_STOP_DRAWDOWN
    return [
        HardStopRule(threshold=hs),
        RLIntentRule(),
        RecoveryRule(
            dd_r1=g("recovery_drawdown_r1", 0.05),
            dd_r2=g("recovery_drawdown_r2", 0.10),
            dd_r3=g("recovery_drawdown_r3", 0.15),
            s_r1=g("position_scalar_r1", 0.5),
            s_r2=g("position_scalar_r2", 0.0),
            s_r3=g("position_scalar_r3", 0.2),
            s_r4=g("position_scalar_r4", 1.0),
        ),
        BudgetRule(
            vol_target=g("vol_target", 0.20),
            kelly_cap=g("kelly_cap", 0.25),
            max_position_pct=g("max_position_pct", 0.30),
        ),
        SellEngineRule(cfg=risk),
    ]
