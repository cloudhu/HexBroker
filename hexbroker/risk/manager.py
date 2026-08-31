"""风控管理器（§3.4 优先级链 v4.0）。

决策优先级（不可被下位覆盖）：
    硬止损 (limits) > S1–S5 (sell_engine) > 风险预算 (budget) > 回撤恢复 R1–R4 (recovery) > RL 意图

每根 bar 调用 :meth:`evaluate`，输入实时 ``RiskState`` 与 RL 意图仓位、信号概率 ``p_up``，
输出不可篡改的 ``RiskDecision``。

P1-8 重构（增量、零口径漂移）：``evaluate`` 改为遍历 ``self.rules`` 填充同一个
``RiskContext``，最终由 manager 计算 ATR ratchet / stop_price。默认 ``build_default_rules(cfg)``
顺序复刻迁移前代码路径，数值/布尔与既有 549 测试完全一致（A8.0 钉死）。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from ..constants import RecoveryStage, SellSignalCode
from .budget import budget_target
from .limits import HARD_STOP_DRAWDOWN, hard_stop_triggered, position_within_limit
from .recovery import recovery_scalar, recovery_stage
from .rules import (
    RiskAdjustment,
    RiskContext,
    RiskRule,
    build_default_rules,
)
from .sell_engine import detect_sell_signals, strongest
from .stoploss import ATRRatchet, compute_stop, trailing_stop
from .types import ATRTier, RiskDecision, RiskState
from ..utils.logging import get_logger

log = get_logger("RISK")


def _apply(ctx: RiskContext, adj: RiskAdjustment) -> None:
    """把一条 ``RiskAdjustment`` 合并进共享的 ``RiskContext``。"""
    if adj.target_position is not None:
        ctx.target_position = float(adj.target_position)
    if adj.liquidate is not None:
        ctx.liquidate = adj.liquidate
    if adj.reason is not None:
        ctx.reason = adj.reason
    if adj.stop_price is not None:
        ctx.stop_price = adj.stop_price
    if adj.veto:
        ctx.veto = True


class RiskManager:
    """风控优先级链实现（规则驱动，默认顺序复刻现状）。"""

    def __init__(
        self, cfg: Any, hard_stop: Optional[float] = None,
        rules: Optional[list[RiskRule]] = None,
    ) -> None:
        self.cfg = cfg
        self.risk = getattr(cfg, "risk", None)
        self.hard_stop = float(hard_stop if hard_stop is not None else HARD_STOP_DRAWDOWN)
        # 默认顺序复刻迁移前 evaluate 代码路径（数值一致）
        self.rules: list[RiskRule] = (
            rules if rules is not None else build_default_rules(cfg, self.hard_stop)
        )
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

        # 遍历规则填充决策上下文（默认顺序复刻现状代码路径）
        ctx = RiskContext(target_position=float(intent_position), atr_tier=state.atr_tier)
        for rule in self.rules:
            adj = rule.evaluate(
                ctx, state, intent_position, p_up,
                recent_returns=recent_returns, recent_volumes=recent_volumes,
                ma_price=ma_price,
            )
            _apply(ctx, adj)
            if ctx.veto:
                break

        # 硬止损（veto）路径：等价于迁移前 early return（仅置清仓/原因/atr_tier）
        if ctx.veto:
            decision.liquidate = ctx.liquidate
            decision.target_position = float(ctx.target_position)
            decision.reason = ctx.reason
            decision.atr_tier = state.atr_tier
            return decision

        # 正常路径：由 ctx 生成 decision（ATR ratchet / stop_price 仍由 manager 计算）
        decision.target_position = float(ctx.target_position)
        decision.liquidate = ctx.liquidate
        decision.reason = ctx.reason
        decision.stage = ctx.stage
        decision.sell_signals = list(ctx.sell_signals)
        decision.kelly_fraction = float(ctx.kelly_fraction)
        decision.atr_tier = ctx.atr_tier

        # ATR 三档 ratchet（只增不减）+ 止损价（按品种隔离记忆）
        sym = state.symbol or "default"
        if sym == "default":
            # ④ 加固：正常生产流 state.symbol 恒非空（来自 self._symbols），此兜底仅当上游 bug。
            # 不发 fail-fast（会误伤单测中对 manager 的 symbol-less 隔离测试），改为可观测告警，
            # 使潜在跨品种止损串扰（缺陷④同构路径）从静默变为可发现。
            if not getattr(self, "_warned_default_symbol", False):
                self._warned_default_symbol = True
                log.warning(
                    "RiskManager.evaluate 收到 symbol 缺失的 RiskState，回落 'default' 共享桶；"
                    "若多品种同时命中将致跨品种止损串扰（稳定性缺陷④同构路径），请排查上游 symbol 注入。"
                )
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
        base_stop = compute_stop(ref_price, state.position or ctx.target_position, state.atr, tier)
        if base_stop is None:
            decision.stop_price = None
            self._prev_stops.pop(sym, None)
        else:
            decision.stop_price = trailing_stop(
                ref_price, state.position or ctx.target_position, state.atr, tier, prev_stop
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
