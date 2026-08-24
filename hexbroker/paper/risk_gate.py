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

from ..backtest.cost import CostModel
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
        cost: Optional[CostModel] = None,
        cost_gate_enabled: bool = True,
        cost_gate_min_ratio: float = 2.0,
        slippage_in_cost: bool = True,
    ) -> None:
        self._manager = load_risk_manager(risk_config, hard_stop=hard_stop, overrides=overrides)
        self._default_intent = float(default_intent)
        self._default_vol = float(default_vol)
        self._vol_quantile = float(vol_quantile)
        # P0-1 开仓成本门禁：cost 为 None → 门禁跳过（默认关闭，向后兼容现有调用）
        self._cost = cost
        self._cost_gate_enabled = bool(cost_gate_enabled)
        self._cost_gate_min_ratio = float(cost_gate_min_ratio)
        # R3：往返成本口径是否纳入滑点（min_tick×slippage_ticks×multiplier×2 边）
        self._slippage_in_cost = bool(slippage_in_cost)

    def set_cost(
        self,
        cost: Optional[CostModel],
        cost_gate_enabled: Optional[bool] = None,
        cost_gate_min_ratio: Optional[float] = None,
        slippage_in_cost: Optional[bool] = None,
    ) -> None:
        """运行时注入成本模型与门禁参数（P0-1；scheduler 从 broker.cost 复用）。"""
        self._cost = cost
        if cost_gate_enabled is not None:
            self._cost_gate_enabled = bool(cost_gate_enabled)
        if cost_gate_min_ratio is not None:
            self._cost_gate_min_ratio = float(cost_gate_min_ratio)
        if slippage_in_cost is not None:
            self._slippage_in_cost = bool(slippage_in_cost)

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
        # P0-1 开仓成本门禁：仅对「新开仓 + 有效信号 + 已注入成本模型」施加。
        # 净期望收益（exp_ret/100 × 名义价值）须覆盖往返手续费 × min_ratio，
        # 否则不开仓（intent=0），并在 decision.reason 标注 cost_gate_reject 供审计。
        cost_rejected = False
        if (
            abs(pos_ctx.position) < 1e-12
            and abs(intent) > 1e-12
            and self._cost_gate_enabled
            and self._cost is not None
        ):
            ok, exp_pnl, rt_cost, notional = self._cost_gate_pass(signal, quote)
            if not ok:
                cost_rejected = True
                intent = 0.0
                log.warning(
                    "成本门禁拦截开仓 symbol={} price={:.2f} p_up={:.4f} exp_ret={:.4f} "
                    "expected_pnl={:.2f} round_trip_cost={:.2f} notional={:.2f} min_ratio={:.1f}",
                    signal.symbol,
                    quote.price if quote is not None else 0.0,
                    float(signal.p_up),
                    float(signal.exp_ret),
                    exp_pnl, rt_cost, notional,
                    self._cost_gate_min_ratio,
                )
        state = self.build_state(quote, acct, pos_ctx)
        # 新开仓前重置 ATR ratchet / 止损记忆（仅本品种），避免沿用上一笔持仓的旧止损
        if abs(pos_ctx.position) < 1e-12 and abs(intent) > 1e-12:
            self._manager.reset_ratchet(symbol=signal.symbol)
        decision = self._manager.evaluate(
            state,
            intent_position=intent,
            p_up=float(signal.p_up),
            recent_returns=recent_returns,
            recent_volumes=recent_volumes,
            ma_price=ma_price,
        )
        if cost_rejected and not decision.liquidate:
            decision.target_position = 0.0
            decision.reason = "cost_gate_reject"
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
            symbol=pos_ctx.symbol or getattr(quote, "symbol", ""),
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

    def _cost_gate_pass(
        self, signal: SignalFrame, quote: Quote
    ) -> tuple[bool, float, float, float]:
        """开仓成本门禁判定（P0-1）。

        返回 ``(是否通过, expected_pnl, round_trip_cost, notional)``：
        - ``notional = price × multiplier(symbol)``（乘数从 CostModel 取，单手口径）；
        - ``expected_pnl = direction × exp_ret/100 × notional``（exp_ret 为日收益百分比）；
        - ``round_trip_cost = 手续费部分 + 滑点部分``（单手往返口径，R3）：
          - 手续费部分 = ``notional × (fee_open + fee_close_today)``；
          - 滑点部分 = ``2 × min_tick(symbol) × slippage_ticks × multiplier(symbol)``
            （单边滑点 × 双边；``slippage_in_cost=False`` 时按 0 计，保持纯费口径）；
        - 同时校验「p_up 方向与 exp_ret 符号一致性」——模型自相矛盾（多头却预期下跌等）
          一律拦截，避免按错误方向计算净期望收益；
        - 通过条件：``expected_pnl > round_trip_cost × cost_gate_min_ratio``。
        """
        price = quote.price if quote is not None and quote.price > 0 else 0.0
        if price <= 0:
            return False, 0.0, 0.0, 0.0
        multiplier = float(self._cost._multiplier(signal.symbol))
        notional = price * multiplier
        if notional <= 0:
            return False, 0.0, 0.0, 0.0
        direction = 1 if signal.p_up >= 0.5 else -1
        exp_ret = float(signal.exp_ret)
        if exp_ret != exp_ret:  # NaN 防御：无有效期望收益 → 不开仓
            return False, 0.0, 0.0, notional
        if direction > 0 and exp_ret <= 0:
            return False, 0.0, 0.0, notional
        if direction < 0 and exp_ret >= 0:
            return False, 0.0, 0.0, notional
        expected_pnl = direction * exp_ret / 100.0 * notional
        # R3：往返成本 = 手续费部分 + 滑点部分（单手口径）。
        # 滑点部分 = 2 × min_tick × slippage_ticks × multiplier（单边滑点 × 双边）。
        fee_part = notional * (
            float(self._cost.fee_open) + float(self._cost.fee_close_today)
        )
        slippage_part = 0.0
        if self._slippage_in_cost:
            slippage_part = (
                2.0
                * float(self._cost._min_tick(signal.symbol))
                * float(self._cost.slippage_ticks)
                * multiplier
            )
        round_trip_cost = fee_part + slippage_part
        if round_trip_cost <= 0:
            return True, expected_pnl, 0.0, notional
        return expected_pnl > round_trip_cost * self._cost_gate_min_ratio, expected_pnl, round_trip_cost, notional
