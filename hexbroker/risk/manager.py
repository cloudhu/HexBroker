"""风控管理器（§3.4 优先级链 v4.0）。

决策优先级（不可被下位覆盖）：
    硬止损 (limits) > S1–S5 (sell_engine) > 风险预算 (budget) > 回撤恢复 R1–R4 (recovery) > RL 意图

每根 bar 调用 :meth:`evaluate`，输入实时 ``RiskState`` 与 RL 意图仓位、信号概率 ``p_up``，
输出不可篡改的 ``RiskDecision``。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..constants import RecoveryStage, SellSignalCode
from .budget import budget_target
from .limits import HARD_STOP_DRAWDOWN, hard_stop_triggered, position_within_limit
from .recovery import recovery_scalar, recovery_stage
from .sell_engine import detect_sell_signals, strongest
from .stoploss import ATRRatchet, compute_stop, trailing_stop
from .types import ATRTier, RiskDecision, RiskState


class RiskManager:
    """风控优先级链实现。"""

    def __init__(self, cfg: Any, hard_stop: Optional[float] = None) -> None:
        self.cfg = cfg
        self.risk = getattr(cfg, "risk", None)
        self.hard_stop = float(hard_stop if hard_stop is not None else HARD_STOP_DRAWDOWN)
        # 按品种隔离 ATR ratchet 与移动止损记忆（P1 修复：避免 ag0/rb0 交替
        # evaluate 时互相污染止损档位与 prev_stop，导致止损价跨品种串扰）
        self._ratchets: dict[str, ATRRatchet] = {}
        self._prev_stops: dict[str, float] = {}

    # --------------------------- 主入口 ---------------------------
    def evaluate(
        self,
        state: RiskState,
        intent_position: float,
        p_up: float = 0.5,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskDecision:
        cfg = self.risk
        decision = RiskDecision()

        # ① 硬止损（最高优先级，不可被任何下位覆盖）
        if hard_stop_triggered(state.drawdown, self.hard_stop):
            decision.liquidate = True
            decision.target_position = 0.0
            decision.reason = "hard_stop"
            decision.atr_tier = state.atr_tier
            return decision

        # ③ 风险预算：波动率目标 + Kelly 上限给出基础允许仓位
        vol = max(state.realized_vol, 1e-6)
        budget = budget_target(
            p_up, vol,
            vol_target=getattr(cfg, "vol_target", 0.20),
            kelly_cap=getattr(cfg, "kelly_cap", 0.25),
            max_position_pct=getattr(cfg, "max_position_pct", 0.30),
        )

        # ④ 回撤恢复分级缩放（覆盖 RL 意图强度）
        stage = recovery_stage(
            state.drawdown,
            dd_r1=getattr(cfg, "recovery_drawdown_r1", 0.05),
            dd_r2=getattr(cfg, "recovery_drawdown_r2", 0.10),
            dd_r3=getattr(cfg, "recovery_drawdown_r3", 0.15),
        )
        scalar = recovery_scalar(
            stage,
            s_r1=getattr(cfg, "position_scalar_r1", 0.5),
            s_r2=getattr(cfg, "position_scalar_r2", 0.0),
            s_r3=getattr(cfg, "position_scalar_r3", 0.2),
            s_r4=getattr(cfg, "position_scalar_r4", 1.0),
        )

        # ⑤ RL 意图先经恢复缩放，再被「预算与硬上限中的较小者」封顶（P4 修复）
        # 预算（vol 目标 + Kelly 上限，≤0.25）须真正约束 RL 意图；max_position_pct
        # 仅作硬上限——取较小者，使预算成为生效上界而非恒被 0.30 覆盖。
        target = intent_position * scalar
        cap = min(abs(budget), getattr(cfg, "max_position_pct", 0.30))
        target = position_within_limit(target, cap)
        decision.stage = stage

        # ② S1–S5 卖出信号（高于风险预算与 RL 意图）
        signals = detect_sell_signals(
            state,
            recent_returns if recent_returns is not None else np.array([]),
            recent_volumes if recent_volumes is not None else np.array([]),
            ma_price,
            cfg,
        )
        if signals:
            if state.position != 0 or target != 0.0:
                target = 0.0
                decision.liquidate = state.position != 0
            decision.sell_signals = list(signals)
            decision.reason = strongest(signals).value
        else:
            decision.reason = "rl_intent"

        decision.target_position = float(target)
        decision.kelly_fraction = float(budget)

        # ATR 三档 ratchet（只增不减）+ 止损价（按品种隔离记忆）
        sym = state.symbol or "default"
        ratchet = self._ratchets.setdefault(sym, ATRRatchet(ATRTier.HIGH))
        prev_stop = self._prev_stops.get(sym)
        ratchet.update(
            state.vol_quantile,
            vol_low_q=getattr(cfg, "vol_low_q", 0.2),
            vol_high_q=getattr(cfg, "vol_high_q", 0.8),
        )
        tier = ratchet.tier
        decision.atr_tier = tier
        # ATR 止损价：用 trailing_stop 实现「止损只向有利方向移动」（红线），
        # 参考价优先 current_price（随价移动锁定利润），缺失时回退 entry_price。
        ref_price = state.current_price if state.current_price > 0 else state.entry_price
        base_stop = compute_stop(ref_price, state.position or target, state.atr, tier)
        if base_stop is None:
            decision.stop_price = None
            self._prev_stops.pop(sym, None)
        else:
            decision.stop_price = trailing_stop(
                ref_price, state.position or target, state.atr, tier, prev_stop
            )
            self._prev_stops[sym] = decision.stop_price
        return decision

    # --------------------------- 工具 ---------------------------
    def reset_ratchet(self, symbol: Optional[str] = None, tier: ATRTier = ATRTier.HIGH) -> None:
        """重置 ATR ratchet / 止损记忆。

        ``symbol`` 指定则仅重置该品种；None 则全部重置（保持旧调用兼容）。
        """
        if symbol is None:
            self._ratchets.clear()
            self._prev_stops.clear()
            return
        self._ratchets[symbol] = ATRRatchet(tier)
        self._prev_stops.pop(symbol, None)
