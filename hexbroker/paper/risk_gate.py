"""风控门（§3.1 RiskGate / D4 / R6）。

构造 ``RiskState`` 并调用 ``RiskManager.evaluate``（优先级链：
硬止损 > S1–S5 > 风险预算 > R1–R4 > 意图），输出 ``RiskDecision``。

意图仓位由 ``SignalFrame`` 换算：有效信号 → 方向 × 默认意图仓位（风控再封顶）；
中性信号（risk_only）→ 0（不驱动新开仓，仅风控管理现有持仓）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..risk.manager import RiskManager
from ..risk.types import RiskDecision, RiskState
from ..utils.logging import get_logger
from .types import AccountSnapshot, PositionCtx, Quote, SignalFrame

log = get_logger("PAPER")


def load_risk_manager(
    risk_config: str | Path | Any,
    hard_stop: Optional[float] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> RiskManager:
    """构造 RiskManager：支持 yaml 路径（OmegaConf）或已含 .risk 的配置对象。

    ``overrides`` 在 v4_atr.yaml 之上覆盖（如按 10 万账户调整 vol_target/kelly_cap）。
    """
    if isinstance(risk_config, (str, Path)):
        from omegaconf import OmegaConf

        cfg = OmegaConf.load(str(risk_config))
    else:
        cfg = risk_config
    if overrides:
        from omegaconf import OmegaConf

        cfg = OmegaConf.merge(cfg, OmegaConf.create({"risk": dict(overrides)}))
    return RiskManager(cfg, hard_stop=hard_stop)


class RiskGate:
    """风控门：SignalFrame + Quote + 账户 → RiskDecision。"""

    def __init__(
        self,
        risk_config: str | Path | Any,
        hard_stop: Optional[float] = None,
        overrides: Optional[dict[str, Any]] = None,
        default_intent: float = 0.30,
        default_vol: float = 0.02,
        vol_quantile: float = 0.5,
    ) -> None:
        self._manager = load_risk_manager(risk_config, hard_stop=hard_stop, overrides=overrides)
        self._default_intent = float(default_intent)
        self._default_vol = float(default_vol)
        self._vol_quantile = float(vol_quantile)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def evaluate(
        self,
        signal: SignalFrame,
        quote: Quote,
        acct: AccountSnapshot,
        pos_ctx: PositionCtx,
        recent_returns: Optional[np.ndarray] = None,
        recent_volumes: Optional[np.ndarray] = None,
        ma_price: Optional[float] = None,
    ) -> RiskDecision:
        """构造 RiskState 并调用 RiskManager.evaluate。"""
        intent = self._intent(signal)
        state = self.build_state(quote, acct, pos_ctx)
        # 新开仓前重置 ATR ratchet / 止损记忆，避免沿用上一笔持仓的旧止损
        if abs(pos_ctx.position) < 1e-12 and abs(intent) > 1e-12:
            self._manager.reset_ratchet()
        decision = self._manager.evaluate(
            state,
            intent_position=intent,
            p_up=float(signal.p_up),
            recent_returns=recent_returns,
            recent_volumes=recent_volumes,
            ma_price=ma_price,
        )
        return decision

    # ------------------------------------------------------------------
    # RiskState 构造
    # ------------------------------------------------------------------
    def build_state(self, quote: Quote, acct: AccountSnapshot, pos_ctx: PositionCtx) -> RiskState:
        price = quote.price if quote and quote.price > 0 else pos_ctx.entry_price
        atr = pos_ctx.atr if pos_ctx.atr > 0 else self._default_vol * price
        realized_vol = atr / price if price > 0 else self._default_vol
        pnl_pct = 0.0
        if abs(pos_ctx.position) > 1e-12 and pos_ctx.entry_price > 0:
            pnl_pct = (price - pos_ctx.entry_price) / pos_ctx.entry_price * np.sign(pos_ctx.position)
        return RiskState(
            equity=acct.equity,
            peak_equity=acct.peak_equity,
            position=pos_ctx.position,
            entry_price=pos_ctx.entry_price,
            current_price=price,
            atr=atr,
            realized_vol=realized_vol,
            bars_in_position=pos_ctx.bars_in_position,
            highest_since_entry=pos_ctx.highest_since_entry,
            lowest_since_entry=pos_ctx.lowest_since_entry,
            pnl_pct=pnl_pct,
            drawdown=acct.drawdown,
            vol_quantile=self._vol_quantile,
        )

    def _intent(self, signal: SignalFrame) -> float:
        """信号 → 意图仓位比例。"""
        if signal is None or not signal.is_effective:
            return 0.0
        direction = 1 if signal.p_up >= 0.5 else -1
        return direction * self._default_intent
