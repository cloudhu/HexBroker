"""模拟券商（§3.5）。

``SimBroker`` 在标的维度上维护持仓、均价、已实现盈亏，按 ``CostModel`` 精确记账。
回测引擎与 RL 环境共用同一个 broker 实例，保证训练-回测一致性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np


@dataclass
class Trade:
    """一笔成交记录。"""

    symbol: str
    timestamp: Any
    qty: float  # 成交数量（带符号：+ 买入 / - 卖出）
    fill_price: float
    fee: float
    is_open: bool
    is_today_close: bool = False


class SimBroker:
    """模拟券商（逐标的记账，mark-to-market 权益）。"""

    def __init__(self, cost: Any, initial_capital: float = 1_000_000.0) -> None:
        self.cost = cost
        self.initial_capital = float(initial_capital)
        self.positions: dict[str, float] = {}
        self.avg_entry: dict[str, float] = {}
        self.realized: dict[str, float] = {}
        self.trades: list[Trade] = []

    # --------------------------- 执行 ---------------------------
    def execute(
        self,
        symbol: str,
        target_qty: float,
        ref_price: float,
        is_today_close: bool = False,
        timestamp: Any = None,
    ) -> Optional[Trade]:
        """将 symbol 的持仓调整到 target_qty，返回成交（无变化返回 None）。"""
        current = float(self.positions.get(symbol, 0.0))
        delta = float(target_qty) - current
        if abs(delta) < 1e-12:
            return None
        side = 1 if delta > 0 else -1
        is_open = (current == 0.0) or (np.sign(delta) == np.sign(current))

        fp, fee, _slip, _total = self.cost.trade_cost(ref_price, delta, is_open, is_today_close, symbol)

        if is_open:
            # 开仓/加仓：更新加权均价
            self.realized[symbol] = self.realized.get(symbol, 0.0) - fee
            abs_cur = abs(current)
            abs_del = abs(delta)
            if abs_cur < 1e-12:
                self.avg_entry[symbol] = fp
            else:
                self.avg_entry[symbol] = (
                    self.avg_entry[symbol] * abs_cur + fp * abs_del
                ) / (abs_cur + abs_del)
        else:
            # 平仓/减仓：结算已实现盈亏（P18-P0：按品种级 multiplier 记账，修复全局 ×10 bug）
            closed = min(abs(delta), abs_cur := abs(current))
            direction = np.sign(current)
            self.realized[symbol] = (
                self.realized.get(symbol, 0.0)
                + direction * (fp - self.avg_entry[symbol]) * closed * self.cost._multiplier(symbol)
                - fee
            )
            if abs(current + delta) < 1e-9:
                self.avg_entry[symbol] = 0.0  # 清空

        self.positions[symbol] = current + delta
        trade = Trade(symbol, timestamp, delta, fp, fee, is_open, is_today_close)
        self.trades.append(trade)
        return trade

    # --------------------------- 估值 ---------------------------
    def unrealized(self, marks: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            if abs(pos) < 1e-12 or sym not in marks:
                continue
            # P18-P0：按品种级 multiplier 记账（修复全局 ×10 bug）
            total += pos * (marks[sym] - self.avg_entry[sym]) * self.cost._multiplier(sym)
        return float(total)

    def equity(self, marks: dict[str, float]) -> float:
        realized = sum(self.realized.values())
        return self.initial_capital + realized + self.unrealized(marks)

    def position(self, symbol: str) -> float:
        return float(self.positions.get(symbol, 0.0))
