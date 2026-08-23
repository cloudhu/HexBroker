"""交易计划管理（§3.1 PlanManager / R8）。

由信号 + 风控决策生成 Plan（方向/目标手数/止盈止损），情报事件仅施加
「风险提示 + 计划备注」（Q3 批复，不自动改方向/仓位）；计划变更落盘
``trade_plans/YYYY-MM-DD_plan.json``（复用 schema_version 约定）。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..utils.logging import get_logger, log_structured
from .types import EVT_PLAN_CHANGE, NewsItem, Plan, PlanChange, Quote, SignalFrame
from .types import dt_now

log = get_logger("PAPER")


class PlanManager:
    """交易计划维护 / 情报备注 / 变更落盘。"""

    def __init__(
        self,
        multipliers: Optional[dict[str, float]] = None,
        risk_reward_ratio: float = 1.5,
        plans_dir: str | Path = "trade_plans",
    ) -> None:
        self._multipliers = multipliers or {}
        self._risk_reward_ratio = float(risk_reward_ratio)
        self._plans_dir = Path(plans_dir)
        self._plans: dict[str, Plan] = {}
        self._changes: list[PlanChange] = []

    # ------------------------------------------------------------------
    # 信号 → 计划
    # ------------------------------------------------------------------
    def update_from_signal(
        self,
        signal: SignalFrame,
        decision: Any,
        quote: Optional[Quote] = None,
        equity: Optional[float] = None,
    ) -> Plan:
        """由信号与风控决策更新计划（无行情/权益时不计算目标手数）。"""
        symbol = signal.symbol
        target_pos_pct = float(getattr(decision, "target_position", 0.0))
        liquidate = bool(getattr(decision, "liquidate", False))
        direction = 0
        if liquidate:
            target_pos_pct = 0.0
        elif target_pos_pct > 1e-9:
            direction = 1
        elif target_pos_pct < -1e-9:
            direction = -1

        target_qty = 0.0
        if quote is not None and quote.price > 0 and equity is not None and equity > 0:
            target_qty = self._size_qty(symbol, target_pos_pct, quote.price, equity)

        stop = getattr(decision, "stop_price", None)
        if stop is not None and stop <= 0:
            stop = None
        tp = self._take_profit(symbol, quote, target_pos_pct, stop)

        old = self._plans.get(symbol)
        plan = Plan(
            symbol=symbol,
            direction=direction,
            target_qty=target_qty,
            target_pos_pct=target_pos_pct,
            stop_price=stop,
            take_profit=tp,
            note=(old.note if old else ""),
            risk_flag=(old.risk_flag if old else ""),
            source=signal.source,
            updated_at=dt_now(),
        )
        self._plans[symbol] = plan
        if old is not None and (
            abs((old.target_qty or 0.0) - target_qty) > 1e-9
            or (old.stop_price or 0.0) != (stop or 0.0)
        ):
            detail = f"target_qty {old.target_qty:g}->{target_qty:g} stop {old.stop_price}->{stop}"
            self._record_change(PlanChange(symbol=symbol, change_type="signal_update", detail=detail))
        return plan

    def _size_qty(self, symbol: str, pos_pct: float, price: float, equity: float) -> float:
        """目标仓位比例 → 目标手数（向下取整；>=0.5 手进 1 手）。"""
        if abs(pos_pct) < 1e-9 or price <= 0 or equity <= 0:
            return 0.0
        multiplier = float(self._multipliers.get(symbol, 10.0))
        raw = abs(pos_pct) * equity / (price * multiplier)
        lots = int(raw)
        if lots == 0 and raw >= 0.5:
            lots = 1
        return math.copysign(lots, pos_pct)

    def _take_profit(self, symbol: str, quote: Optional[Quote], pos_pct: float, stop: Optional[float]) -> Optional[float]:
        """止盈 = 入场价 ± 风险收益比 × |入场-止损|（无止损 → None）。"""
        if quote is None or stop is None or abs(pos_pct) < 1e-9:
            return None
        dist = abs(quote.price - stop)
        if dist <= 0:
            return None
        return quote.price + math.copysign(dist * self._risk_reward_ratio, pos_pct)

    # ------------------------------------------------------------------
    # 情报 → 计划（Q3：仅备注 + 风险提示）
    # ------------------------------------------------------------------
    def apply_news(self, events: list[NewsItem]) -> list[PlanChange]:
        """情报事件应用到计划；返回产生的变更（落盘 + 日志由上层负责）。"""
        changes: list[PlanChange] = []
        for ev in events or []:
            for sym in ev.symbols or []:
                plan = self._plans.get(sym)
                if plan is None:
                    continue
                if "risk" in (ev.tags or []):
                    plan.risk_flag = ev.title[:120]
                    change = PlanChange(symbol=sym, change_type="risk_hint", detail=ev.title[:200])
                else:
                    plan.note = f"{plan.note} | {ev.title[:100]}".strip(" |")
                    change = PlanChange(symbol=sym, change_type="note", detail=ev.title[:200])
                self._record_change(change)
                changes.append(change)
        return changes

    def _record_change(self, change: PlanChange) -> None:
        self._changes.append(change)
        log_structured(EVT_PLAN_CHANGE, change.to_dict())

    # ------------------------------------------------------------------
    # 查询/落盘
    # ------------------------------------------------------------------
    def get_plan(self, symbol: str) -> Optional[Plan]:
        return self._plans.get(symbol)

    def get_all_plans(self) -> dict[str, Plan]:
        return dict(self._plans)

    def recent_changes(self, since: Optional[datetime] = None) -> list[PlanChange]:
        if since is None:
            return list(self._changes)
        return [c for c in self._changes if c.ts >= since]

    def save_plan_file(self, day: date) -> Path:
        """当日计划落盘 ``trade_plans/YYYY-MM-DD_plan.json``。"""
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "date": day.isoformat(),
            "generated_at": dt_now().isoformat(timespec="seconds"),
            "plans": [p.to_dict() for p in self._plans.values()],
            "changes": [c.to_dict() for c in self._changes],
        }
        path = self._plans_dir / f"{day.isoformat()}_plan.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log.info("交易计划已落盘 path={} plans={}", path, len(self._plans))
        return path