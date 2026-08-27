"""交易通道抽象层（§3 / P1-5）。

零依赖（绝不顶层 import vnpy_ctp）：``BrokerGateway`` ABC 统一回测/模拟/实盘三级交易
执行契约；``LiveBroker`` Protocol 约定 ``execute(symbol, target_qty, ref_price, ts)``
与 ``SimBroker.execute`` / ``PaperBroker.execute_plan`` 同语义，使回测-模拟-实盘可互换。

适配实现均为**薄包装**（SimBroker / PaperBroker / 内存 Fake），不改被包对象任何代码。
"""

from __future__ import annotations

import numpy as np
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class Position:
    """持仓快照。"""

    symbol: str
    qty: float
    avg_price: float


@dataclass
class Account:
    """账户快照。"""

    equity: float
    cash: float
    margin_used: float


@dataclass
class Order:
    """下单指令（vn.py Gateway 插件化模式）。"""

    symbol: str
    direction: int  # +1 买 / -1 卖
    qty: float
    ref_price: float
    order_id: str = ""


class BrokerGateway(ABC):
    """统一交易通道抽象（vn.py Gateway 插件化模式，零依赖）。"""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def query_position(self, symbol: str) -> Position: ...

    @abstractmethod
    def query_account(self) -> Account: ...

    @abstractmethod
    def submit_order(self, order: Order) -> str:
        """提交订单，返回 order_id。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...


@runtime_checkable
class LiveBroker(Protocol):
    """回测-模拟-实盘共用执行契约。"""

    def execute(
        self, symbol: str, target_qty: float, ref_price: float, timestamp: Any = None
    ) -> Any: ...


class FakeBrokerGateway(BrokerGateway):
    """内存版交易网关（供单测 submit→query→cancel 往返，行为对齐 SimBroker 已知场景）。"""

    def __init__(self, initial_capital: float = 100_000.0) -> None:
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.positions: dict[str, float] = {}
        self.avg_price: dict[str, float] = {}
        self._orders: dict[str, Order] = {}
        self._seq = 0

    # BrokerGateway 抽象实现
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def submit_order(self, order: Order) -> str:
        self._seq += 1
        oid = order.order_id or f"F{self._seq:06d}"
        self._orders[oid] = order
        target = order.direction * abs(order.qty)
        self.execute(order.symbol, target, order.ref_price)
        return oid

    def cancel_order(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def query_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            qty=self.positions.get(symbol, 0.0),
            avg_price=self.avg_price.get(symbol, 0.0),
        )

    def query_account(self) -> Account:
        margin = sum(
            abs(q) * self.avg_price.get(s, 0.0) * 0.12 for s, q in self.positions.items()
        )
        return Account(equity=self.cash, cash=self.cash, margin_used=margin)

    # LiveBroker 执行契约
    def execute(
        self, symbol: str, target_qty: float, ref_price: float, timestamp: Any = None
    ) -> Any:
        cur = self.positions.get(symbol, 0.0)
        delta = float(target_qty) - cur
        if abs(delta) < 1e-12:
            return None
        if abs(cur) < 1e-12:
            self.avg_price[symbol] = float(ref_price)
        elif np.sign(delta) == np.sign(cur):
            ac, aq = abs(cur), abs(delta)
            self.avg_price[symbol] = (
                self.avg_price[symbol] * ac + float(ref_price) * aq
            ) / (ac + aq)
        self.positions[symbol] = cur + delta
        return delta


class SimBrokerGateway(BrokerGateway):
    """薄包 SimBroker（hexbroker/backtest/broker.py），零改动被包对象。"""

    def __init__(self, cost: Any, initial_capital: float = 1_000_000.0) -> None:
        from ..backtest.broker import SimBroker

        self.broker = SimBroker(cost, initial_capital=float(initial_capital))

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def submit_order(self, order: Order) -> str:
        target = order.direction * abs(order.qty)
        self.broker.execute(order.symbol, target, order.ref_price, timestamp=None)
        return f"SIM-{order.symbol}-{id(order)}"

    def cancel_order(self, order_id: str) -> bool:
        return False

    def query_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            qty=self.broker.position(symbol),
            avg_price=self.broker.avg_entry.get(symbol, 0.0),
        )

    def query_account(self) -> Account:
        return Account(
            equity=self.broker.equity({}),
            cash=self.broker.initial_capital,
            margin_used=0.0,
        )

    def execute(
        self, symbol: str, target_qty: float, ref_price: float, timestamp: Any = None
    ) -> Any:
        return self.broker.execute(symbol, target_qty, ref_price, timestamp=timestamp)


class PaperBrokerGateway(BrokerGateway):
    """薄包 PaperBroker（hexbroker/paper/broker.py），内部 translate 为 execute_plan。"""

    def __init__(self, paper: Any) -> None:
        self.paper = paper

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def submit_order(self, order: Order) -> str:
        target = order.direction * abs(order.qty)
        event = self.execute(order.symbol, target, order.ref_price)
        if event is None:
            return f"P-{order.symbol}-{id(order)}"
        return event.trade_id

    def cancel_order(self, order_id: str) -> bool:
        return False  # 模拟盘无独立订单簿管理

    def query_position(self, symbol: str) -> Position:
        return Position(
            symbol=symbol,
            qty=self.paper.position(symbol),
            avg_price=self.paper.avg_entry(symbol),
        )

    def query_account(self) -> Account:
        snap = self.paper.snapshot()
        return Account(equity=snap.equity, cash=snap.cash, margin_used=snap.margin_used)

    def execute(
        self, symbol: str, target_qty: float, ref_price: float, timestamp: Any = None
    ) -> Any:
        from datetime import datetime

        from ..paper.types import Plan, Quote

        ts = timestamp if timestamp is not None else datetime.now()
        plan = Plan(
            symbol=symbol,
            direction=int(np.sign(target_qty)),
            target_qty=float(target_qty),
            target_pos_pct=0.0,
        )
        quote = Quote(symbol=symbol, ts=ts, price=float(ref_price))
        return self.paper.execute_plan(plan, quote, ts)
