"""模拟盘券商（§3.1 PaperBroker / D1）。

薄封装 ``SimBroker``（同口径成本/保证金/平今费），叠加资金约束（A4）：
- 可用资金 = equity - margin_used >= 0；
- 全组合总保证金占用（`_margin_after` 遍历 positions 全量求和，含新开仓目标品种）<= 预算上限（budget_ratio * equity）；
- 超预算/资金不足 → 拒绝下单（不超预算下单）。

账户快照可原子落盘 ``data/paper/account.json``，重启续跑（交易日计数从快照恢复）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..backtest.broker import SimBroker
from ..backtest.cost import CostModel
from ..utils.logging import get_logger
from .types import AccountSnapshot, Plan, PositionCtx, Quote, TradeEvent

log = get_logger("PAPER")

# ---------------------------------------------------------------------------
# 异常交易日（P1-4 补记，2026-09-04）
# ---------------------------------------------------------------------------
# 2026-08-24 因**三进程并发**导致数据不可信：该日复盘从未生成、_c0_intraday 等
# 内存态早失，成交/盈亏口径被并发污染。团队裁决：该日**补记为交易日**（计入
# trading_day_count），但其成交/盈亏**不参与** 20 日策略评估样本（只保留在审计
# 轨迹）。凡需从评估样本剔除异常日成交的逻辑，用 ``is_anomaly_day`` 判定。
# 集合可用字符串 "YYYY-MM-DD" 配置注入（默认含 08-24）。
ANOMALY_TRADING_DAYS: frozenset[str] = frozenset({"2026-08-24"})


def is_anomaly_day(day: Any, extra: Optional[frozenset[str]] = None) -> bool:
    """某日是否标记为「异常交易日」（其成交/盈亏不参与策略评估样本）。

    入参 ``day`` 接受 ``date`` / ``datetime`` / ``"YYYY-MM-DD"`` 字符串。
    默认集合 ``ANOMALY_TRADING_DAYS``，可用 ``extra`` 叠加（便于测试/配置覆写）。
    """
    if day is None:
        return False
    if isinstance(day, datetime):
        day_s = day.date().isoformat()
    elif isinstance(day, date):
        day_s = day.isoformat()
    else:
        day_s = str(day)
        if len(day_s) > 10:
            day_s = day_s[:10]
    return day_s in ANOMALY_TRADING_DAYS or (extra is not None and day_s in extra)



def _to_date(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


def build_cost_model(paper_cfg: Any) -> CostModel:
    """从 ``configs/paper.yaml`` 构造 CostModel（品种级合约参数显式声明）。"""
    cost_cfg = paper_cfg.get("cost", {})
    contracts: dict[str, dict] = {}
    for sym, cfg in paper_cfg.get("symbols", {}).items():
        contracts[sym] = {
            "multiplier": float(cfg.get("multiplier", 10.0)),
            "min_tick": float(cfg.get("min_tick", 1.0)),
        }
    return CostModel(
        fee_open=float(cost_cfg.get("fee_rate_open", 0.00005)),
        fee_close=float(cost_cfg.get("fee_rate_close", 0.00005)),
        fee_close_today=float(cost_cfg.get("fee_rate_close_today", 0.00010)),
        slippage_ticks=float(cost_cfg.get("slippage_ticks", 1.0)),
        margin_rate=float(cost_cfg.get("margin_rate", 0.12)),
        multiplier=float(cost_cfg.get("multiplier", 10.0)),
        min_tick=float(cost_cfg.get("min_tick", 10.0)),
        contracts=contracts,
    )


class PaperBroker:
    """模拟盘券商：SimBroker 薄封装 + 资金/预算约束 + 账户快照。"""

    def __init__(
        self,
        cost: CostModel,
        initial_capital: float = 100_000.0,
        budget_ratio: float = 0.30,
        data_dir: str | Path = "data/paper",
        cross_day_rolling_extremes: bool = False,
    ) -> None:
        self._cost = cost
        self._budget_ratio = float(budget_ratio)
        self.data_dir = Path(data_dir)
        self._broker = SimBroker(cost, initial_capital=float(initial_capital))
        self._peak_equity = float(initial_capital)
        self._trading_day_count = 0
        self._last_trading_day: Optional[date] = None
        self._trade_seq = 0
        # 每品种实际止损/止盈（P0-1 修复：平仓事件须读持仓实际档位，而非 plan
        # 派生值；SimBroker.positions 仅存数量无档位，故 PaperBroker 自维护）
        self._stops: dict[str, float | None] = {}
        self._take_profits: dict[str, float | None] = {}
        # ⚠️ 跨日滚动极值（任务B / 名实不符语义债）：
        #   hi/lo 原实现取「当日」quote.high/low，非「持仓以来」滚动极值 → 名实不符。
        #   默认关闭（cross_day_rolling_extremes=False）→ 维持现状（当日口径，行为零变化）；
        #   开启后维护 `_ext_hi/_ext_lo`（自建仓以来跨日滚动极值）并随 account.json 持久化。
        #   ⛔ 实证（2026-09-04）：risk 引擎 S1–S5 / trailing / ratchet / 硬止损当前**均不读取**
        #   highest_since_entry/lowest_since_entry（观察/审计字段），故开启本开关**不改变任何触发点**；
        #   留开关仅为将来某规则真消费该字段时能闸住语义，且默认保持今日行为。
        self._cross_day_extremes = bool(cross_day_rolling_extremes)
        self._ext_hi: dict[str, float] = {}
        self._ext_lo: dict[str, float] = {}
        # P1-4：已补计入 count 的异常日（幂等，随快照持久化）
        self._counted_anomaly_days: set[str] = set()

    # ------------------------------------------------------------------
    # 执行（核心新增接口，§3.2）
    # ------------------------------------------------------------------
    def execute_plan(self, plan: Plan, quote: Quote, ts: Any = None) -> Optional[TradeEvent]:
        """按计划将持仓调整到 ``target_qty``。

        校验顺序：① 目标与当前一致 → None；② 单品种保证金预算不超限；
        ③ 可用资金 >= 0。任一不满足 → 拒绝（返回 None，记 warning）。
        """
        symbol = plan.symbol
        current = self._broker.position(symbol)
        target = float(plan.target_qty)
        if abs(target - current) < 1e-9:
            return None
        ts = ts if ts is not None else datetime.now()

        marks = self._marks(quote)
        equity = self._broker.equity(marks)
        budget = self._budget_ratio * equity
        margin_new = self._margin_after(symbol, target, marks)
        if margin_new > budget + 1e-6:
            log.warning(
                "拒绝下单：超预算 symbol={} target={:g} margin_new={:.2f} budget={:.2f}",
                symbol, target, margin_new, budget,
            )
            return None
        cash_new = equity - margin_new
        if cash_new < -1e-6:
            log.warning(
                "拒绝下单：可用资金不足 symbol={} target={:g} cash_new={:.2f}",
                symbol, target, cash_new,
            )
            return None

        trade = self._broker.execute(symbol, target, ref_price=quote.price, timestamp=ts)
        if trade is None:
            return None
        self._trade_seq += 1

        # P0-1 修复：平仓/减仓事件 stop/take_profit 必须读「持仓实际档位」，
        # 禁止复用 plan.stop_price / plan.take_profit（原写法导致 ag0 平仓错显
        # rb0 止损等跨品种污染）。SimBroker.positions 仅存数量，故 PaperBroker 自维护。
        new_pos = current + trade.qty
        if trade.is_open:
            stop_value = plan.stop_price
            tp_value = plan.take_profit
            if abs(current) < 1e-12:
                # 首次建仓：以 plan 档位作为持仓实际止损/止盈（加仓沿用，不覆盖）
                self._stops[symbol] = plan.stop_price
                self._take_profits[symbol] = plan.take_profit
                # 任务B：自建仓起初始化跨日滚动极值（seed=开仓均价，后续每 tick 抬升）
                if self._cross_day_extremes:
                    entry_px = float(self._broker.avg_entry.get(symbol, trade.fill_price))
                    self._ext_hi[symbol] = entry_px
                    self._ext_lo[symbol] = entry_px
        else:
            stop_value = self._stops.get(symbol, plan.stop_price)
            tp_value = self._take_profits.get(symbol, plan.take_profit)
            if abs(new_pos) > 1e-12 and ((new_pos > 0) != (current > 0)):
                # 反手：本次平仓事件用原持仓止损；反转后新仓止损以 plan 写入
                self._stops[symbol] = plan.stop_price
                self._take_profits[symbol] = plan.take_profit
                # 任务B：反手视为「旧仓了结 + 新仓开启」→ 滚动极值重置为新开仓均价
                if self._cross_day_extremes:
                    entry_px = float(self._broker.avg_entry.get(symbol, trade.fill_price))
                    self._ext_hi[symbol] = entry_px
                    self._ext_lo[symbol] = entry_px
            elif abs(new_pos) <= 1e-12:
                # P2（2026-09-04）：已平仓至 0 → 清除该品种档位。原实现从不清理，
                # 导致空仓品种长期残留旧止损/止盈（ag0 曾残留 16247.93 /
                # 17275.61）并被每 5 分钟反复写回 account.json。
                # 此处 stop_value / tp_value 已从原档位取出并写入事件，可安全清除。
                self._stops.pop(symbol, None)
                self._take_profits.pop(symbol, None)
                # 任务B：平仓至 0 → 清空该品种滚动极值（不留死数据污染 account.json）
                if self._cross_day_extremes:
                    self._ext_hi.pop(symbol, None)
                    self._ext_lo.pop(symbol, None)

        event = TradeEvent(
            trade_id=f"T{self._trade_seq:06d}",
            ts=ts,
            symbol=symbol,
            direction=1 if trade.qty > 0 else -1,
            qty=trade.qty,
            entry=self._broker.avg_entry.get(symbol, trade.fill_price),
            stop=stop_value,
            take_profit=tp_value,
            price=trade.fill_price,
            fee=trade.fee,
            is_open=trade.is_open,
            is_today_close=trade.is_today_close,
        )
        # 审计 JSON 由 TradeLogger.trade 统一输出（单一审计源）
        self._update_peak(marks)
        return event

    # ------------------------------------------------------------------
    # 资金/保证金
    # ------------------------------------------------------------------
    def _marks(self, quote: Optional[Quote] = None) -> dict[str, float]:
        """构造 mark-to-market 价格：当前报价优先，其余用持仓均价兜底。

        P1-1：陈旧的报价（``stale=True``）不用于盯市，fail-closed 退回成本价
        （陈旧本身已由 ``RealTimeQuoteClient`` 节流告警，此处不重复刷屏）。
        P1-2：``quote is None`` 时全部按开仓成本估值 —— 该分支在 P0-1 事故中
        完全静默，现必须告警。
        """
        marks: dict[str, float] = {}
        for sym in self._broker.positions.keys():
            fallback = self._broker.avg_entry.get(sym, 0.0)
            marks[sym] = fallback if fallback and fallback > 0 else 0.0
        if quote is not None and quote.usable():
            marks[quote.symbol] = quote.price
        elif quote is None and any(v > 0 for v in marks.values()):
            log.warning(
                "盯市兜底：未提供任何报价，持仓全部按开仓成本价估值 {}"
                "（浮盈显示为 0，非真实市值）",
                {s: round(float(v), 4) for s, v in sorted(marks.items())},
            )
        return marks

    def _margin_after(self, symbol: str, target: float, marks: dict[str, float]) -> float:
        """目标持仓状态下的总保证金占用（含新开仓目标品种）。"""
        total = 0.0
        symbols = set(self._broker.positions.keys()) | {symbol}
        for sym in symbols:
            qty = target if sym == symbol else self._broker.positions.get(sym, 0.0)
            if abs(qty) < 1e-12:
                continue
            price = marks.get(sym, 0.0) or self._broker.avg_entry.get(sym, 0.0)
            total += self._cost.margin(price, qty, sym)
        return total

    def margin_used(self, marks: Optional[dict[str, float]] = None) -> float:
        """当前持仓的保证金占用。"""
        marks = marks if marks is not None else self._marks()
        total = 0.0
        for sym, pos in self._broker.positions.items():
            if abs(pos) < 1e-12:
                continue
            price = marks.get(sym, 0.0) or self._broker.avg_entry.get(sym, 0.0)
            total += self._cost.margin(price, pos, sym)
        return total

    def available_cash(self, marks: Optional[dict[str, float]] = None) -> float:
        """可用资金 = equity - margin_used（A4：任意时点 >= 0）。"""
        marks = marks if marks is not None else self._marks()
        return self._broker.equity(marks) - self.margin_used(marks)

    # ------------------------------------------------------------------
    # 账户快照
    # ------------------------------------------------------------------
    def snapshot(self, marks: Optional[dict[str, float]] = None) -> AccountSnapshot:
        """账户快照（未提供 marks 时用均价近似估值）。"""
        marks = marks if marks is not None else self._marks()
        equity = self._broker.equity(marks)
        margin = self.margin_used(marks)
        # ⛔ 分母为零防御：peak<=0（异常快照/未初始化）→ drawdown 记 0，绝不除零。
        drawdown = (self._peak_equity - equity) / self._peak_equity if self._peak_equity > 0 else 0.0
        return AccountSnapshot(
            ts=datetime.now(),
            equity=equity,
            cash=equity - margin,
            margin_used=margin,
            positions=dict(self._broker.positions),
            avg_entry=dict(self._broker.avg_entry),
            realized=dict(self._broker.realized),
            drawdown=max(0.0, drawdown),
            peak_equity=self._peak_equity,
        )

    @property
    def peak_equity(self) -> float:
        """历史权益峰值（只读）。单调递增，重启后从 ``account.json`` 还原。"""
        return self._peak_equity

    def update_peak(self, marks: dict[str, float]) -> None:
        """用**完整且新鲜**的 marks 抬升权益峰值（单调递增，只升不降）。

        ⚠️ 调用方契约：必须传「全品种完整、且报价新鲜」的 marks。
        用部分 marks / 成本价兜底 marks 调用会把峰值抬到虚高，
        令 drawdown **高估** → 硬止损误触发（与漏更新的低估方向相反，
        但同样有害）。故**不要**在 ``snapshot()`` 内部偷偷调用。

        2026-09-04 回撤口径核查：原实现只在 ``execute_plan`` 内被调用
        （即**只在成交时**更新），盘中权益创新高但无成交 → 峰值不抬升 →
        drawdown 被**系统性低估** → ``risk_hard_stop`` 触发偏晚。
        现由调度器在每 tick 与每次快照落盘前显式调用。
        """
        equity = self._broker.equity(marks)
        if equity > self._peak_equity:
            self._peak_equity = equity

    def _update_peak(self, marks: dict[str, float]) -> None:
        """向后兼容别名（旧调用点 ``execute_plan`` 仍在用）→ :meth:`update_peak`。"""
        self.update_peak(marks)

    # ------------------------------------------------------------------
    # 持仓/查询
    # ------------------------------------------------------------------
    def position(self, symbol: str) -> float:
        return self._broker.position(symbol)

    def avg_entry(self, symbol: str) -> float:
        return float(self._broker.avg_entry.get(symbol, 0.0))

    # ------------------------------------------------------------------
    # 持仓止损/止盈档位（P0-C 影子模式读取入口）
    # ------------------------------------------------------------------
    def stop_of(self, symbol: str) -> Optional[float]:
        """该品种持仓实际止损档位（未设返回 ``None``）。

        只读入口：``_stops`` 是 P0-1 引入的「每品种实际档位」表，此前只有
        ``execute_plan`` 内部访问。P0-C 影子模式需要外部读取，故开只读口，
        **字段名保持 ``_stops`` / ``_take_profits`` 原名不变**（改名会撕裂
        ``account.json`` 持久化格式与复盘报表）。
        """
        return self._stops.get(symbol)

    def take_profit_of(self, symbol: str) -> Optional[float]:
        """该品种持仓实际止盈档位（未设返回 ``None``）。"""
        return self._take_profits.get(symbol)

    def position_ctx(self, symbol: str, quote: Quote, atr: float = 0.0) -> PositionCtx:
        """构造持仓上下文（RiskGate 输入）。"""
        pos = self._broker.position(symbol)
        entry = self._broker.avg_entry.get(symbol, 0.0)
        if abs(pos) < 1e-12:
            return PositionCtx(symbol=symbol, position=0.0, entry_price=0.0, atr=atr)
        d_entry = _to_date(self._broker.open_dates.get(symbol))
        # ⚠️ 语义锁定（③语义债评估 24-bars-semantics-assessment.md）：
        # bars_in_position 此处 = 自然日天数（含周末，min 1），非 bar 数；
        # RL/回测侧（rl/futures_env）为真实 bar 数。bar_freq=1d 生产配置下
        # 实盘口径 ≥ 回测口径（偏保守，S1/S4 更晚触发，无实害）。
        # 护栏：bar_freq 改非日线前必须同步改为 session 交易日感知并过 walk-forward QA。
        bars = 1
        if d_entry is not None:
            bars = max(1, (datetime.now().date() - d_entry).days)
        hi = max(entry, quote.high) if quote.high > 0 else max(entry, quote.price)
        lo = min(entry, quote.low) if quote.low > 0 else min(entry, quote.price)
        if self._cross_day_extremes:
            # ⚠️ 任务B 跨日滚动极值（名实不符语义债修复，默认关闭）：
            #   默认分支上两行 = 当日 quote.high/low 口径（highest_today_or_entry），
            #   跨日持仓时昨日极值丢失。开启后改维护「持仓以来」跨日滚动极值：
            #   _ext_hi/_ext_lo 建仓 seed=entry，此后每 tick 用当日 high/low 抬升/下压，
            #   状态随 account.json 持久化（additive 键 ext_hi/ext_lo）。
            #   ⛔ 只 bump 不回落：hi 只升、lo 只降，天然单调。陈旧/异常报价
            #   （high/low 非正）不会改写（保持当前滚动值），故不会污染。
            cur_hi = self._ext_hi.get(symbol, entry)
            cur_lo = self._ext_lo.get(symbol, entry)
            if quote.high and quote.high > 0 and quote.high > cur_hi:
                cur_hi = quote.high
            if quote.low and quote.low > 0 and (cur_lo <= 0 or quote.low < cur_lo):
                cur_lo = quote.low if cur_lo <= 0 else min(cur_lo, quote.low)
            hi = cur_hi if cur_hi > 0 else entry
            lo = cur_lo if cur_lo > 0 else entry
            self._ext_hi[symbol] = cur_hi
            self._ext_lo[symbol] = cur_lo
        return PositionCtx(
            symbol=symbol,
            position=pos,
            entry_price=entry,
            atr=atr,
            bars_in_position=bars,
            highest_since_entry=hi,
            lowest_since_entry=lo,
            open_ts=self._broker.open_dates.get(symbol),
        )

    def trades(self) -> list[Any]:
        """底层成交记录（SimBroker.Trade 列表）。"""
        return list(self._broker.trades)

    def trades_on(self, day: date) -> list[Any]:
        """指定交易日标签（含夜盘跨日）的成交。"""
        return [t for t in self._broker.trades if _to_date(t.timestamp) == day]

    # ------------------------------------------------------------------
    # 交易日计数（Q5：满 20 交易日自动评估）
    # ------------------------------------------------------------------
    def record_trading_day(self, day: date) -> None:
        """收盘时登记一个**已完成**交易日（前向推进，单调递增）。

        P1-4 补记（2026-09-04）：本方法**只接受 >= last_trading_day 的新交易日**，
        杜绝「后登记的更早日期把 last_trading_day 回卷、导致后续重复计数」。
        回卷/重复登记一律告警后忽略。
        """
        if self._last_trading_day is not None and day < self._last_trading_day:
            log.warning(
                "拒绝登记更早交易日（防 last_trading_day 回卷）day={} last={} —— "
                "早于当前已登记日的补记请走 count_anomaly_day()",
                day, self._last_trading_day,
            )
            return
        if self._last_trading_day == day:
            return  # 幂等：同一天重复登记不重复计数
        self._last_trading_day = day
        self._trading_day_count += 1
        # P1-4（2026-09-05）：若该日本身是异常交易日，登记进 counted 集合，
        # 使 load_snapshot 的自动补记钩子对其幂等（避免「正常登记 + 自动补记」双重 +1）。
        if is_anomaly_day(day):
            _k = day.isoformat() if isinstance(day, date) else str(day)
            self._counted_anomaly_days.add(_k)

    def count_anomaly_day(self, day: date) -> bool:
        """把异常交易日计入 count（补记），但**不改变** ``last_trading_day``。

        P1-4：08-24 因三进程并发数据不可信、其复盘从未生成 → 正常前向登记从未
        触发。但该日是一段真实交易时段，策略评估计数应包含它。由于它**早于**
        当前 last_trading_day，走 ``record_trading_day`` 会把 last 回卷 → 改走
        本方法：只 ``count += 1``（幂等：同一 ``day`` 只计一次），不回卷 last。

        返回是否实际计入（重复计入返回 False）。
        """
        if day is None:
            return False
        key = day.isoformat() if isinstance(day, date) else str(day)
        if key in self._counted_anomaly_days:
            return False
        self._counted_anomaly_days.add(key)
        self._trading_day_count += 1
        return True

    @property
    def trading_day_count(self) -> int:
        return self._trading_day_count

    # ------------------------------------------------------------------
    # 快照持久化
    # ------------------------------------------------------------------
    def save_snapshot(self, path: Optional[str | Path] = None) -> Path:
        """原子写账户快照（含 SimBroker 状态，重启可续跑）。"""
        path = Path(path) if path else (self.data_dir / "account.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "initial_capital": self._broker.initial_capital,
            "positions": self._broker.positions,
            "avg_entry": self._broker.avg_entry,
            "realized": self._broker.realized,
            "open_dates": {
                k: (v.isoformat() if hasattr(v, "isoformat") else str(v))
                for k, v in self._broker.open_dates.items()
            },
            "peak_equity": self._peak_equity,
            "trading_day_count": self._trading_day_count,
            "last_trading_day": self._last_trading_day.isoformat() if self._last_trading_day else None,
            "trade_seq": self._trade_seq,
            "stops": self._stops,
            "take_profits": self._take_profits,
            # 任务B（additive，不改既有字段 schema）：跨日滚动极值状态。
            # 缺失/旧快照无此键 → load 默认 {} → on 模式从当前 entry 冷启动，
            # off 模式根本不读（零影响）。
            "ext_hi": self._ext_hi,
            "ext_lo": self._ext_lo,
            # P1-4（additive）：已补计入 count 的异常交易日（幂等去重）
            "counted_anomaly_days": sorted(self._counted_anomaly_days),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load_snapshot(self, path: Optional[str | Path] = None) -> bool:
        """从快照恢复账户状态；文件不存在返回 False（首次启动）。

        P2-5：快照损坏（JSONDecodeError / 非 dict 结构）→ 备份损坏文件为
        ``account.json.corrupt.<ts>`` + 重置为初始资金 + 告警日志，不启动失败。
        """
        path = Path(path) if path else (self.data_dir / "account.json")
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"快照顶层不是对象：{type(payload).__name__}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            backup = path.with_name(f"{path.name}.corrupt.{datetime.now():%Y%m%d%H%M%S}")
            try:
                path.replace(backup)
            except OSError:
                log.warning("快照损坏备份失败 path={} backup={}", path, backup)
            log.warning(
                "账户快照损坏，已重置为初始资金 {:.0f} 并备份损坏文件: {}（err={}）",
                self._broker.initial_capital, backup, exc,
            )
            # 重置状态（当前 _broker 即 __init__ 创建的初始账户）
            self._peak_equity = float(self._broker.initial_capital)
            self._trading_day_count = 0
            self._last_trading_day = None
            self._trade_seq = 0
            self._stops = {}
            self._take_profits = {}
            self._ext_hi = {}
            self._ext_lo = {}
            self._counted_anomaly_days = set()
            return False
        self._broker = SimBroker(self._cost, initial_capital=float(payload.get("initial_capital", 100_000.0)))
        self._broker.positions = {str(k): float(v) for k, v in payload.get("positions", {}).items()}
        self._broker.avg_entry = {str(k): float(v) for k, v in payload.get("avg_entry", {}).items()}
        self._broker.realized = {str(k): float(v) for k, v in payload.get("realized", {}).items()}
        self._broker.open_dates = {
            str(k): datetime.fromisoformat(v) for k, v in payload.get("open_dates", {}).items() if v
        }
        self._stops = {str(k): (float(v) if v is not None else None) for k, v in payload.get("stops", {}).items()}
        self._take_profits = {
            str(k): (float(v) if v is not None else None) for k, v in payload.get("take_profits", {}).items()
        }
        # 任务B：跨日滚动极值（additive 键；旧快照缺失 → {} → on 模式从当前 entry 冷启动）
        self._ext_hi = {str(k): float(v) for k, v in payload.get("ext_hi", {}).items()}
        self._ext_lo = {str(k): float(v) for k, v in payload.get("ext_lo", {}).items()}
        # P1-4：已补计的异常日（幂等去重集合；旧快照缺失 → 空）
        self._counted_anomaly_days = {
            str(v) for v in payload.get("counted_anomaly_days", []) if v
        }
        self._peak_equity = float(payload.get("peak_equity", self._broker.initial_capital))
        self._trading_day_count = int(payload.get("trading_day_count", 0))

        # P1-4 自动补记（2026-09-05）：启动即把已知异常交易日（ANOMALY_TRADING_DAYS，
        # 如 08-24 三进程并发污染）计入 count，且不回卷 last_trading_day。
        # count_anomaly_day 幂等（已计入的日不再重复 +1），故每次重启安全、不会多计。
        # 效果：20 日策略评估的交易日计数包含「真实存在但数据不可信」的 08-24，
        # 同时其成交在 _maybe_evaluation 中按 is_anomaly_day 从评估样本剔除。
        for _ad in ANOMALY_TRADING_DAYS:
            self.count_anomaly_day(_ad)
        ltd = payload.get("last_trading_day")
        self._last_trading_day = date.fromisoformat(ltd) if ltd else None
        self._trade_seq = int(payload.get("trade_seq", 0))
        # ⛔ P1-4（2026-09-01）：原实现打的是 ``initial_capital`` 而非**真实 equity**。
        # 后果极严重：08:58:53 恢复日志显示 equity=100000.00，而 3 秒后的保存日志显示
        # 94967.46 —— 两者其实是**同一个值**（100000 − realized 5032.54），从未变化；
        # 但误导复盘推断出「恢复瞬间 drawdown 被抹平为 0，风控按零回撤放行」，
        # 进而误立了一条 P1 缺陷。实测（artifacts/_tmp/p1_4_realized_restore_audit.py）：
        # realized / peak_equity **均已正确还原**，恢复瞬间 drawdown 就是 5.13%。
        # 故本条只改日志，不改还原逻辑。
        try:
            restored_snap = self.snapshot()
            log.info(
                "账户快照已恢复：equity={:.2f} peak={:.2f} drawdown={:.2%} realized={:.2f} "
                "positions={}",
                restored_snap.equity, restored_snap.peak_equity, restored_snap.drawdown,
                sum(self._broker.realized.values()), self._broker.positions,
            )
        except Exception as exc:  # noqa: BLE001 —— 日志不得阻断启动
            log.warning("账户快照已恢复（equity 计算失败，回退显示初始资金）: {}", exc)
            log.info("账户快照已恢复：positions={}", self._broker.positions)
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @property
    def broker(self) -> SimBroker:
        """底层 SimBroker（测试/审计用）。"""
        return self._broker

    @property
    def cost(self) -> CostModel:
        return self._cost