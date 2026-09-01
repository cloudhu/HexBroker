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
from .types import EVT_PLAN_CHANGE, NewsItem, Plan, PlanChange, Quote, SignalFrame, dt_now

log = get_logger("PAPER")


class PlanManager:
    """交易计划维护 / 情报备注 / 变更落盘。"""

    def __init__(
        self,
        multipliers: Optional[dict[str, float]] = None,
        risk_reward_ratio: float = 1.5,
        plans_dir: str | Path = "trade_plans",
        # ---- P0-4（2026-09-01）仓位粒度放大防护 ----
        size_by_risk: bool = False,
        risk_per_trade: float = 0.01,
        risk_stop_atr_mult: float = 2.5,
        max_position_pct: float = 0.30,
    ) -> None:
        self._multipliers = multipliers or {}
        self._risk_reward_ratio = float(risk_reward_ratio)
        self._plans_dir = Path(plans_dir)
        self._plans: dict[str, Plan] = {}
        self._changes: list[PlanChange] = []
        # P0-4：见 ``_size_qty`` 文档串。默认 False = 沿用历史行为（不静默改变实盘）。
        self._size_by_risk = bool(size_by_risk)
        self._risk_per_trade = float(risk_per_trade)
        # 无止损价可用时的兜底止损距离倍率（= ATRTier.HIGH 最宽档 2.5，与生产 compute_stop 同参）
        self._risk_stop_atr_mult = float(risk_stop_atr_mult)
        self._max_position_pct = float(max_position_pct)

    # ------------------------------------------------------------------
    # 信号 → 计划
    # ------------------------------------------------------------------
    def update_from_signal(
        self,
        signal: SignalFrame,
        decision: Any,
        quote: Optional[Quote] = None,
        equity: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> Plan:
        """由信号与风控决策更新计划（无行情/权益时不计算目标手数）。

        Args:
            atr: 当前 ATR（P0-4）。仅当 ``decision.stop_price`` 缺失时用作风险定价兜底。
        """
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

        # P0-4：止损要先算 —— 风险预算法要用它给「1 手风险」定价（见 _size_qty）。
        stop = getattr(decision, "stop_price", None)
        if stop is not None and stop <= 0:
            stop = None

        target_qty = 0.0
        if quote is not None and quote.price > 0 and equity is not None and equity > 0:
            target_qty = self._size_qty(
                symbol, target_pos_pct, quote.price, equity, stop=stop, atr=atr
            )

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

    # ------------------------------------------------------------------
    # P0-4（2026-09-01）：仓位粒度放大
    # ------------------------------------------------------------------
    def risk_distance(self, price: float, stop: Optional[float] = None,
                      atr: Optional[float] = None) -> Optional[float]:
        """单手持仓的止损距离（元/单位）。优先用**真实止损价**，缺则按 ATR 兜底。

        Returns:
            距离；无法定价（既无止损也无 ATR）时返回 None。
        """
        if stop is not None and stop > 0 and price > 0:
            d = abs(price - float(stop))
            return d if d > 0 else None
        if atr is not None and atr > 0:
            return self._risk_stop_atr_mult * float(atr)
        return None

    def size_metrics(
        self,
        symbol: str,
        pos_pct: float,
        price: float,
        equity: float,
        stop: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> dict:
        """P0-4 可观测：把「名义敞口 / 单笔风险 / 两种口径的手数」一次算清。

        供日志、trace 与端到端验证使用（不产生副作用）。
        """
        multiplier = float(self._multipliers.get(symbol, 10.0))
        out = {
            "symbol": symbol,
            "multiplier": multiplier,
            "raw_lots": 0.0,
            "lots_notional": 0,
            "notional_pct": 0.0,
            "over_notional_cap": False,
            "risk_dist": None,
            "risk_per_lot": None,
            "risk_pct_1lot": None,
            "lots_risk": None,
            "final_lots": 0,
            "capped_by": "none",
        }
        if abs(pos_pct) < 1e-9 or price <= 0 or equity <= 0:
            return out
        raw = abs(pos_pct) * equity / (price * multiplier)
        out["raw_lots"] = raw
        if raw < 0.10:
            out["capped_by"] = "min_lot_threshold"
            return out
        lots_notional = max(1, int(raw))
        out["lots_notional"] = lots_notional
        notional_pct = lots_notional * price * multiplier / equity
        out["notional_pct"] = notional_pct
        out["over_notional_cap"] = notional_pct > self._max_position_pct + 1e-12

        dist = self.risk_distance(price, stop, atr)
        out["risk_dist"] = dist
        if dist is None or dist <= 0:
            out["final_lots"] = lots_notional
            out["capped_by"] = "notional_only(no_risk_data)"
            return out
        risk_per_lot = dist * multiplier
        out["risk_per_lot"] = risk_per_lot
        out["risk_pct_1lot"] = risk_per_lot / equity
        lots_risk = int((equity * self._risk_per_trade) / risk_per_lot) if risk_per_lot > 0 else 0
        out["lots_risk"] = lots_risk

        if not self._size_by_risk:
            out["final_lots"] = lots_notional
            out["capped_by"] = "notional_only(size_by_risk=off)"
        elif lots_risk < 1:
            out["final_lots"] = 0
            out["capped_by"] = "risk_budget"
        else:
            out["final_lots"] = min(lots_notional, lots_risk)
            out["capped_by"] = (
                "risk_budget" if lots_risk < lots_notional else "notional"
            )
        return out

    def _size_qty(
        self,
        symbol: str,
        pos_pct: float,
        price: float,
        equity: float,
        stop: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> float:
        """目标仓位比例 → 目标手数。

        历史规则（P1-2 修复）：raw >= 0.10 手即开至少 1 手（10 万账户 ag 在合理信号强度
        下可开 1 手，保证金由预算第二道防线兜底 margin <= budget）；>=1 手向下取整。

        ⚠️ P0-4（2026-09-01 实证）：期货**最小交易单位是 1 手**，所以「风控批准 0.4454 手
        → 实开 1 手」是粒度约束，不是笔误；但它会让**实际名义敞口越过风控自身的
        ``max_position_pct``（rb0 15%→33.7%，ag0 15%→**257.6%**）。而 broker 第二道防线是
        **按保证金**把关（ag0 保证金仅占 30.9%，预算 40% 内放行），兜不住名义敞口。

        两难：① 放行 = 越过硬顶；② 向下取整 = 小账户（10 万）在 ag0/rb0 上永不交易。

        ✅ 解法（``size_by_risk=True`` 时启用）——**按风险预算法定价**，绕开粒度死结：
            单笔亏损 = |price - stop| × multiplier × lots  ≤  equity × risk_per_trade
        即以「止损距离」而非「名义敞口」定手数。实测（equity 94,868，止损 2.5×ATR）：
            ag0  1 手风险 25.49% 权益 → **0 手（拦下）**
            rb0  1 手风险  0.82% 权益 → 1 手（与现状一致）
            c0   1 手风险  0.59% 权益 → 1 手（与现状一致）
        → 只拦真正风险过大的品种，其余**零行为变化**。

        止损距离优先用 ``decision.stop_price``（真实值）；缺失时按 ``risk_stop_atr_mult × atr``
        兜底（默认 2.5 = ATRTier.HIGH 最宽档，与生产 ``compute_stop`` 同参，保守侧）。
        """
        m = self.size_metrics(symbol, pos_pct, price, equity, stop=stop, atr=atr)
        if m["raw_lots"] < 0.10:
            return 0.0

        # 越过硬顶时告警（无论是否启用风险预算法，都让问题可见）
        if m["over_notional_cap"]:
            log.warning(
                "P0-4 名义敞口越过硬顶 symbol={} 手数={} 名义占比={:.2%} > max_position_pct={:.2%}"
                "（风控意图 {:.2%}；期货最小 1 手，粒度放大不可避免）",
                symbol, m["lots_notional"], m["notional_pct"], self._max_position_pct,
                abs(pos_pct),
            )
        if m["capped_by"] == "risk_budget":
            log.warning(
                "P0-4 风险预算拦截 symbol={} 1手风险={:.2%} 权益 > risk_per_trade={:.2%}"
                "（止损距离={:.2f}）→ 不开仓",
                symbol, m["risk_pct_1lot"] or 0.0, self._risk_per_trade, m["risk_dist"] or 0.0,
            )
        if m["final_lots"] <= 0:
            return 0.0
        return math.copysign(float(m["final_lots"]), pos_pct)

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
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)  # P2-6：tmp + os.replace 原子写，避免写入中断产生半文件
        log.info("交易计划已落盘 path={} plans={}", path, len(self._plans))
        return path