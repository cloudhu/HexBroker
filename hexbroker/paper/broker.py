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
        else:
            stop_value = self._stops.get(symbol, plan.stop_price)
            tp_value = self._take_profits.get(symbol, plan.take_profit)
            if abs(new_pos) > 1e-12 and ((new_pos > 0) != (current > 0)):
                # 反手：本次平仓事件用原持仓止损；反转后新仓止损以 plan 写入
                self._stops[symbol] = plan.stop_price
                self._take_profits[symbol] = plan.take_profit
            elif abs(new_pos) <= 1e-12:
                # P2（2026-09-04）：已平仓至 0 → 清除该品种档位。原实现从不清理，
                # 导致空仓品种长期残留旧止损/止盈（ag0 曾残留 16247.93 /
                # 17275.61）并被每 5 分钟反复写回 account.json。
                # 此处 stop_value / tp_value 已从原档位取出并写入事件，可安全清除。
                self._stops.pop(symbol, None)
                self._take_profits.pop(symbol, None)

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

    def _update_peak(self, marks: dict[str, float]) -> None:
        equity = self._broker.equity(marks)
        if equity > self._peak_equity:
            self._peak_equity = equity

    # ------------------------------------------------------------------
    # 持仓/查询
    # ------------------------------------------------------------------
    def position(self, symbol: str) -> float:
        return self._broker.position(symbol)

    def avg_entry(self, symbol: str) -> float:
        return float(self._broker.avg_entry.get(symbol, 0.0))

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
        """收盘时登记一个已完成交易日。"""
        if self._last_trading_day == day:
            return
        self._last_trading_day = day
        self._trading_day_count += 1

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
        self._peak_equity = float(payload.get("peak_equity", self._broker.initial_capital))
        self._trading_day_count = int(payload.get("trading_day_count", 0))
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