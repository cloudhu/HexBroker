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
        risk_per_trade_by_symbol: Optional[dict[str, float]] = None,
        risk_stop_atr_mult: float = 2.5,
        max_position_pct: float = 0.30,
        # ---- P1 / P1-C（2026-09-02）可交易性门槛与保证金硬顶 ----
        margin_rate: float = 0.12,
        max_margin_pct: float = 0.20,
        struct_untradeable_ratio: float = 3.0,
    ) -> None:
        self._multipliers = multipliers or {}
        self._risk_reward_ratio = float(risk_reward_ratio)
        self._plans_dir = Path(plans_dir)
        self._plans: dict[str, Plan] = {}
        self._changes: list[PlanChange] = []
        # P0-4：见 ``_size_qty`` 文档串。默认 False = 沿用历史行为（不静默改变实盘）。
        self._size_by_risk = bool(size_by_risk)
        self._risk_per_trade = float(risk_per_trade)
        # D2-C（2026-09-01）：按品种覆盖单笔风险预算。不同品种「1 手」的绝对风险
        # 差异数量级（rb0 0.86% vs ag0 24.89% 权益），一把尺子量到底必然是
        # 「要么全放行、要么拦死某个品种」。未列出的品种沿用 ``risk_per_trade``。
        self._risk_per_trade_by_symbol = {
            str(k): float(v) for k, v in (risk_per_trade_by_symbol or {}).items()
        }
        # 无止损价可用时的兜底止损距离倍率（= ATRTier.HIGH 最宽档 2.5，与生产 compute_stop 同参）
        self._risk_stop_atr_mult = float(risk_stop_atr_mult)
        # ⛔ 名义硬顶（``max_position_pct``）**设计上仅告警、不拦截**（见 ``_size_qty``
        # 与 ``scripts/paper_trading_main.py::_max_position_pct`` 的 docstring）。
        # P1-C（2026-09-02）：该口径对期货**根本错配** —— 名义价值口径下 18 个品种
        # 仅 3 个达标，而保证金口径下 15 个达标（期货天然带杠杆，名义价值必然 >> 本金）。
        # 故另设**保证金口径**硬顶并让它**真正生效**，名义口径降级为纯诊断。
        self._max_position_pct = float(max_position_pct)
        self._margin_rate = float(margin_rate)
        # ⛔ 已知局限（QA 复核 🟡 项，2026-09-02 登记，刻意不修）：
        # ``max_margin_pct`` 目前是**全局单一值**，**不支持**像 ``risk_per_trade``
        # 那样走 ``risk_per_trade_by_symbol`` 的按品种覆盖。若在 risk_overrides
        # 里为某个品种写 ``max_margin_pct``，会被**静默忽略**。
        # 不修的理由：① 当前生产宇宙（ag0/rb0/c0）无需分品种保证金上限；
        # ② 接入 overrides 要改配置加载链路，扩大改动面，与「最小改动」原则冲突。
        # 触发条件（届时再改）：确需对某一品种单独放宽/收紧保证金上限时。
        self._max_margin_pct = float(max_margin_pct)
        # P1（2026-09-02）：``min_equity / equity >= 此值`` → 判定为**结构性不可交易**
        # （合约规模与账户规模量级不匹配，调参数无解），日志降级为每日 1 条 INFO，
        # 避免每轮刷屏淹没真实告警。设 0 可关闭该降级（全部按普通拦截告警）。
        self._struct_untradeable_ratio = float(struct_untradeable_ratio)
        # 结构性不可交易日志的按日去重表：symbol -> 上次打日志的日期
        self._struct_logged_on: dict[str, date] = {}

    def risk_budget_for(self, symbol: str) -> float:
        """该品种的单笔风险预算（D2-C：优先取按品种覆盖值，未覆盖则用全局）。"""
        return float(self._risk_per_trade_by_symbol.get(symbol, self._risk_per_trade))

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
        budget = self.risk_budget_for(symbol)
        out = {
            "symbol": symbol,
            "multiplier": multiplier,
            "risk_budget": budget,          # D2-C：本品种实际生效的预算（便于 trace 归因）
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
            # ---- P1 / P1-C（2026-09-02）----
            "min_equity": None,              # 开 1 手所需的最低权益（= 单手风险 / 预算）
            "margin_pct": 0.0,               # 1 手保证金占权益比例
            "lots_margin": None,             # 保证金硬顶下允许的最大手数
            "over_margin_cap": False,        # 是否越过保证金硬顶（**真正拦截**）
            "structurally_untradeable": False,  # 结构性不可交易（量级不匹配，调参无解）
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

        # P1-C：保证金口径（与 broker 第一道防线同口径；**真正生效的硬顶**）
        #
        # ⛔ fail-safe（QA 复核 🔴 项，2026-09-02 修复）：保证金口径**不可用时必须
        # 放弃这道硬顶，而不是让它返回 0 手**。原写法在 margin_rate<=0（配置缺失/
        # 误填 0）或 price/multiplier<=0 时，margin_per_lot=0 → lots_margin=0 →
        # **全品种 final_lots=0，交易静默全停**，且日志只报「保证金 0.00% > 上限
        # 20.00%」这种自相矛盾的告警，极难定位。
        # 新增护栏不得引入新的「全停」失效模式：口径不可用时降级为「不限制」
        # = 退回 P1-C 之前的基线（仍有风险预算 + broker 保证金两道把关）。
        margin_on = (
            self._margin_rate > 0
            and self._max_margin_pct > 0
            and price > 0
            and multiplier > 0
        )
        margin_per_lot = price * multiplier * self._margin_rate if margin_on else 0.0
        out["margin_pct"] = margin_per_lot / equity
        if margin_on:
            lots_margin = int(equity * self._max_margin_pct / margin_per_lot)
            out["lots_margin"] = lots_margin
            out["over_margin_cap"] = lots_margin < 1
        else:
            # 口径不可用 → 该硬顶不生效（lots_margin 取 lots_notional，不收紧 min()）
            lots_margin = lots_notional
            out["lots_margin"] = None      # None = 本轮不适用，区别于「算出来是 0 手」
            out["over_margin_cap"] = False

        dist = self.risk_distance(price, stop, atr)
        out["risk_dist"] = dist
        if dist is None or dist <= 0:
            # ⛔ 无风险数据的降级路径（既无 stop 也无 ATR）。此处**刻意不施加**
            # 保证金硬顶 —— 保持与 P1-C 之前完全一致的行为，避免动到罕见分支。
            out["final_lots"] = lots_notional
            out["capped_by"] = "notional_only(no_risk_data)"
            return out
        risk_per_lot = dist * multiplier
        out["risk_per_lot"] = risk_per_lot
        out["risk_pct_1lot"] = risk_per_lot / equity
        lots_risk = int((equity * budget) / risk_per_lot) if risk_per_lot > 0 else 0
        out["lots_risk"] = lots_risk

        # P1：门槛权益 = 「开 1 手所需的最低权益」。量级远超当前权益 ⇒ 结构性不可交易
        # （合约规模与账户规模不匹配，调风险预算/止损倍数都无解，只能加本金）。
        if budget > 0 and risk_per_lot > 0:
            min_equity = risk_per_lot / budget
            out["min_equity"] = min_equity
            out["structurally_untradeable"] = (
                self._struct_untradeable_ratio > 0
                and min_equity > equity * self._struct_untradeable_ratio
            )

        if not self._size_by_risk:
            out["final_lots"] = lots_notional
            out["capped_by"] = "notional_only(size_by_risk=off)"
        elif lots_risk < 1:
            # ⛔ 判序铁律：风险预算**必须**先于保证金判定 —— 这样 ag0 等结构性品种的
            # capped_by 仍是 "risk_budget"，P1 的日志降级才能正确命中（否则会被
            # 判成 "margin_cap"，每轮刷保证金告警，P1 白做）。
            out["final_lots"] = 0
            out["capped_by"] = "risk_budget"
        elif lots_margin < 1:
            out["final_lots"] = 0
            out["capped_by"] = "margin_cap"
        else:
            out["final_lots"] = min(lots_notional, lots_risk, lots_margin)
            out["capped_by"] = (
                "risk_budget" if lots_risk < min(lots_notional, lots_margin)
                else ("margin_cap" if lots_margin < lots_notional else "notional")
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
        ``max_position_pct``（rb0 15%→33.5%，ag0 30%→**256.9%**）。而 broker 第二道防线是
        **按保证金**把关（ag0 保证金仅占 ~31%，预算 40% 内放行），兜不住名义敞口。

        ⛔ 危害窗口：ag0 **只在意图 ≥ ~25.8% 时**才被放大（15% 时 raw=0.058 < 0.10
        阈值直接不开仓）。而 ``risk_default_intent=0.30`` 恰在区间内 —— 只看 15% 会漏掉。

        两难：① 放行 = 越过硬顶；② 向下取整 = 小账户（10 万）在 ag0/rb0 上永不交易。

        ✅ 解法（``size_by_risk=True`` 时启用）——**按风险预算法定价**，绕开粒度死结：
            单笔亏损 = |price - stop| × multiplier × lots  ≤  equity × 本品种预算
        即以「止损距离」而非「名义敞口」定手数。实测（equity 94,868，止损 2.5×ATR）：
            ag0  1 手风险 24.89% 权益 → **0 手（拦下）**
            rb0  1 手风险  0.86% 权益 → 1 手（与现状一致）
            c0   1 手风险  0.59% 权益 → 1 手（与现状一致）
        → 只拦真正风险过大的品种，其余**零行为变化**。

        **D2-C（2026-09-01 主理人裁决）**：不同品种「1 手」的绝对风险差一个数量级，
        一把尺子量到底必然是「要么全放行、要么拦死某品种」。故支持
        ``risk_per_trade_by_symbol`` 按品种覆盖，见 ``risk_budget_for``。
        ⚠️ 但须知悉：ag0 属**结构性不可交易** —— 1 手风险 24.89% 是合约乘数（15×）
        与最小手数决定的，任何 <24.89% 的预算都会拦它；把预算抬到 25% 则等于
        放弃风控。要让 ag0 在 1% 风险下可开 1 手，**需权益 ≈ 236 万**。

        止损距离优先用 ``decision.stop_price``（真实值）；缺失时按 ``risk_stop_atr_mult × atr``
        兜底（默认 2.5 = ATRTier.HIGH 最宽档，与生产 ``compute_stop`` 同参，保守侧）。

        **P1（2026-09-02 主理人裁决）权益自适应可交易门槛**：
        上段说的「结构性不可交易」其实是**全品种通病**，不是 ag0 独有 —— 按名义价口径
        复算（equity 94,857 / 2.5×ATR / 1% 预算），18 个品种里**仅 rb0（0.84%）与 hc0（0.77%）
        可交易**；门槛权益升序：hc0 7.3 万 < rb0 7.9 万 < m0 11.2 万 < sr0 14.3 万 <
        y0 25.8 万 < cf0 29.0 万 < i0 31.4 万 < al0 32.4 万 < p0 37.7 万 < ta0 42.5 万 <
        zn0 50.6 万 < ni0 60.7 万 < jm0 85.3 万 < cu0 141.7 万 < j0 154.3 万 <
        **ag0 230.6 万** < au0 462.1 万 < sc0 605.1 万。

        ⛔ **口径警告（2026-09-02 QA 复核后修正，勿再混用）**：上面这张 18 品种表是
        **回测研究口径**（CONTRACTS18，``data/raw/processed/`` 下全部品种），
        **不是生产在跑的宇宙**。生产宇宙以 ``configs/paper.yaml::symbols`` 为准，
        只有 **ag0 / rb0 / c0** 三个，实测（同口径）：
            rb0  1 手风险 0.84%、保证金  4.02%、门槛  7.9 万（0.84×）→ ✅ 可交易
            c0   1 手风险 0.59%、保证金  2.91%、门槛  5.6 万（0.59×）→ ✅ 可交易
            ag0  1 手风险 24.31%、保证金 30.83%、门槛 230.6 万（24.31×）→ ⛔ 结构性
        即**生产 3 品种中 2 个可交易**，只有 ag0 被结构性拦下。注意两侧的错位：
        **hc0 在研究宇宙但不在生产宇宙**；**c0 在生产宇宙但不在 CONTRACTS18**，
        且 c0 **不在主湖 parquet**（``data/raw/processed/`` 无 c0 目录，其历史行情
        只能取实时源）—— 这点在复算生产可交易性时会直接踩空 FileNotFoundError。
        故引入 ``min_equity``（= 单手风险 / 本品种预算）：当 ``min_equity / equity >=
        struct_untradeable_ratio``（默认 3.0）时判定**结构性不可交易**，拦截日志按日去重
        降级为 INFO（见 ``_log_struct_untradeable``）；未达该倍数的品种（如当前 m0 仅
        1.18 倍）仍按普通 WARNING 告警 —— 它们离解锁很近，值得提醒。
        这样**本金一涨，品种自动解锁**，无需每次改白名单。

        **P1-C（2026-09-02）名义硬顶 → 保证金硬顶**：
        ``max_position_pct`` 是**名义价值**口径，对带杠杆的期货根本错配 —— 名义口径下
        18 个品种仅 3 个达标，而保证金口径下 15 个达标。故保留名义告警（**设计上仅
        诊断、不拦截**），另设 ``max_margin_pct``（保证金口径，默认 0.20，对应行业实践
        的「单品种保证金占用 ≤10%–20%」）并让它**真正生效**。
        ⛔ **判序铁律**：``risk_budget`` 判定**必须**先于 ``margin_cap``，否则 ag0 会被
        判成保证金拦截而每轮刷告警，P1 的静默逻辑就失效了。

        ⚠️ **措辞更正（回归实测后）**：「零行为变化」**仅在风险预算未被人为放宽时
        成立**。全量回归发现 3 个历史用例（``tests/test_size_qty_risk_cap.py``）构造了
        「把 ag0 预算放宽到 30%」或「用极窄止损 0.1%」来"救活" ag0 的场景 —— 改动前
        能开 1 手，改动后被保证金硬顶拦下（ag0 的 1 手保证金占权益 30.9% > 20%）。
        判定：这是**正确的风控补强**（窄止损只降低单笔风险一个维度，掩盖不了
        「账户近 1/3 资金被这一手占用」；且恰好堵住「放宽预算/收窄止损绕过风控」
        这条路径），已保留拦截并更新用例断言。
        **生产配置下（ag0 预算 1%，风险 24.31% 先被拦）保证金硬顶永远不会成为
        第一拦截者 → 生产行为与改动前逐位一致。**
        """
        m = self.size_metrics(symbol, pos_pct, price, equity, stop=stop, atr=atr)
        if m["raw_lots"] < 0.10:
            return 0.0

        # 名义硬顶：**仅诊断，不拦截**（这是原设计，见 __init__ 注释）。
        # P1-C（2026-09-02）：该口径对期货**根本错配**（名义价值口径 3/18 达标 vs
        # 保证金口径 15/18），真正生效的硬顶已改为保证金（见 ``margin_cap``）。
        # 此告警保留仅为可观测，不代表任何拦截动作。
        if m["over_notional_cap"]:
            log.warning(
                "P0-4 名义敞口越过硬顶 symbol={} 手数={} 名义占比={:.2%} > max_position_pct={:.2%}"
                "（风控意图 {:.2%}；期货最小 1 手，粒度放大不可避免）"
                "ⓘ 名义口径对期货错配，仅供诊断、不拦截；实际硬顶见保证金占比 {:.2%}",
                symbol, m["lots_notional"], m["notional_pct"], self._max_position_pct,
                abs(pos_pct), m["margin_pct"],
            )
        if m["capped_by"] == "risk_budget":
            if m["structurally_untradeable"]:
                # P1：量级不匹配 → 每日仅 1 条 INFO，不再每轮刷屏淹没真实告警
                self._log_struct_untradeable(symbol, m, equity)
            else:
                log.warning(
                    "P0-4 风险预算拦截 symbol={} 1手风险={:.2%} 权益 > 预算={:.2%}"
                    "（止损距离={:.2f}；需权益≥{:.0f} 才能开 1 手）→ 不开仓",
                    symbol, m["risk_pct_1lot"] or 0.0, m["risk_budget"], m["risk_dist"] or 0.0,
                    (m["risk_per_lot"] or 0.0) / m["risk_budget"] if m["risk_budget"] > 0 else 0.0,
                )
        elif m["capped_by"] == "margin_cap":
            log.warning(
                "P1-C 保证金硬顶拦截 symbol={} 1手保证金={:.2%} 权益 > max_margin_pct={:.2%}"
                "（保证金率={:.2%}；最多可开 {} 手）→ 不开仓",
                symbol, m["margin_pct"], self._max_margin_pct, self._margin_rate,
                max(m["lots_margin"] or 0, 0),
            )
        if m["final_lots"] <= 0:
            return 0.0
        return math.copysign(float(m["final_lots"]), pos_pct)

    def _log_struct_untradeable(self, symbol: str, m: dict, equity: float) -> None:
        """P1（2026-09-02）：结构性不可交易 —— **每日仅 1 条 INFO**，不再每轮 WARNING。

        所谓「结构性」= 合约规模与账户规模**量级不匹配**：门槛权益达到当前权益的
        ``struct_untradeable_ratio``（默认 3.0）倍以上。此时调风险预算或止损倍数都无解
        （ag0 即使把止损收到 1.0×ATR 仍需 9.73% 预算，远超行业 1%–2%），**只能加本金**。

        这类品种的拦截是**预期的稳态行为**，每轮告警只会淹没真实告警（如行情故障、
        信号缺失、P0-4 对边缘品种的拦截）。故按日去重后降级为 INFO，并在文末给出
        门槛权益与倍数，便于运维一眼判断「该加多少钱才能解锁」。
        """
        today = dt_now().date()
        if self._struct_logged_on.get(symbol) == today:
            return
        self._struct_logged_on[symbol] = today
        min_equity = float(m.get("min_equity") or 0.0)
        ratio = min_equity / equity if equity > 0 else float("inf")
        log.info(
            "P1 结构性不可交易（observe_only）symbol={} 1手风险={:.2%} 权益 > 预算={:.2%}"
            "；门槛权益={:,.0f} = 当前权益 {:,.0f} 的 {:.1f} 倍"
            "（合约规模与账户规模不匹配，调参无解，需加本金）→ 本日不再重复告警",
            symbol, m["risk_pct_1lot"] or 0.0, m["risk_budget"],
            min_equity, equity, ratio,
        )

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