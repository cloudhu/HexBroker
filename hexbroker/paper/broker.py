"""模拟盘券商（§3.1 PaperBroker / D1）。

薄封装 ``SimBroker``（同口径成本/保证金/平今费），叠加资金约束（A4）：
- 可用资金 = equity - margin_used >= 0；
- 单品种保证金占用 <= 预算上限（budget_ratio * equity）；
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
        event = TradeEvent(
            trade_id=f"T{self._trade_seq:06d}",
            ts=ts,
            symbol=symbol,
            direction=1 if trade.qty > 0 else -1,
            qty=trade.qty,
            entry=self._broker.avg_entry.get(symbol, trade.fill_price),
            stop=plan.stop_price,
            take_profit=plan.take_profit,
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
        """构造 mark-to-market 价格：当前报价优先，其余用持仓均价兜底。"""
        marks: dict[str, float] = {}
        for sym in self._broker.positions.keys():
            fallback = self._broker.avg_entry.get(sym, 0.0)
            marks[sym] = fallback if fallback and fallback > 0 else 0.0
        if quote is not None and quote.price > 0:
            marks[quote.symbol] = quote.price
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
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load_snapshot(self, path: Optional[str | Path] = None) -> bool:
        """从快照恢复账户状态；文件不存在返回 False（首次启动）。"""
        path = Path(path) if path else (self.data_dir / "account.json")
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._broker = SimBroker(self._cost, initial_capital=float(payload.get("initial_capital", 100_000.0)))
        self._broker.positions = {str(k): float(v) for k, v in payload.get("positions", {}).items()}
        self._broker.avg_entry = {str(k): float(v) for k, v in payload.get("avg_entry", {}).items()}
        self._broker.realized = {str(k): float(v) for k, v in payload.get("realized", {}).items()}
        self._broker.open_dates = {
            str(k): datetime.fromisoformat(v) for k, v in payload.get("open_dates", {}).items() if v
        }
        self._peak_equity = float(payload.get("peak_equity", self._broker.initial_capital))
        self._trading_day_count = int(payload.get("trading_day_count", 0))
        ltd = payload.get("last_trading_day")
        self._last_trading_day = date.fromisoformat(ltd) if ltd else None
        self._trade_seq = int(payload.get("trade_seq", 0))
        log.info("账户快照已恢复：equity={:.2f} positions={}", self._broker.initial_capital, self._broker.positions)
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