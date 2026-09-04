"""模拟券商（§3.5）。

``SimBroker`` 在标的维度上维护持仓、均价、已实现盈亏，按 ``CostModel`` 精确记账。
回测引擎与 RL 环境共用同一个 broker 实例，保证训练-回测一致性。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import numpy as np


def _to_date(ts: Any) -> Optional[date]:
    """把时间戳（datetime / date / pd.Timestamp / ISO 字符串）归一为 date；无法解析返回 None。"""
    if ts is None:
        return None
    if hasattr(ts, "date"):  # datetime / date / pd.Timestamp 均暴露 .date()
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
        # 当前净持仓的开仓日（用于平今判定；反手/全平后清除）
        self.open_dates: dict[str, Any] = {}

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
        # P0-B（2026-09-04）：``np.sign(delta) == np.sign(current)`` 返回的是
        # **numpy.bool_**，不是 Python bool。当 current != 0（减仓/平仓/反手）
        # 时，``or`` 短路不到左边的 Python bool，is_open 就变成 numpy.bool_。
        # 后果：json.dumps 不认 numpy.bool_ → 掉进 _json_default → 旧实现
        # str(o) → 审计日志落成字符串 "False"（实证 trades.log：
        # "is_open": "False" 与 "is_today_close": true 并列）。
        # 下游不得不在 trade_intent.py:9 / trade_stats.py:46 到处加
        # _as_bool / _norm_bool 兜底，即为该缺陷的历史佐证。
        # ⛔ 必须显式套 bool()，保证落盘为 JSON 原生 true/false。
        is_open = bool((current == 0.0) or (np.sign(delta) == np.sign(current)))

        # 平今判定：依据当前净持仓开仓日 vs 平仓 bar 日（覆盖调用方硬编码的 False）
        is_today_close = self._compute_is_today_close(symbol, current, delta, timestamp)

        fp, fee, _slip, _total = self.cost.trade_cost(ref_price, delta, is_open, is_today_close, symbol)

        if is_open:
            # 开仓/加仓：更新加权均价
            self.realized[symbol] = self.realized.get(symbol, 0.0) - fee
            abs_cur = abs(current)
            abs_del = abs(delta)
            if abs_cur < 1e-12:
                self.avg_entry[symbol] = fp
                self.open_dates[symbol] = timestamp  # 新开仓：记录开仓日
            else:
                self.avg_entry[symbol] = (
                    self.avg_entry[symbol] * abs_cur + fp * abs_del
                ) / (abs_cur + abs_del)
                # 加仓：沿用最早开仓日（不更新 open_dates）
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
                self.avg_entry[symbol] = 0.0  # 全平
                self.open_dates.pop(symbol, None)
            elif np.sign(current + delta) != np.sign(current):
                # 反手：剩余仓位为反向新开仓，以成交价重置加权均价与开仓日
                self.avg_entry[symbol] = fp
                self.open_dates[symbol] = timestamp
            # 同方向减仓：保留 open_dates（仍是原开仓日）

        self.positions[symbol] = current + delta
        trade = Trade(symbol, timestamp, delta, fp, fee, is_open, is_today_close)
        self.trades.append(trade)
        return trade

    def _compute_is_today_close(
        self, symbol: str, current: float, delta: float, timestamp: Any
    ) -> bool:
        """平仓/减仓时，若当前净持仓开仓日与平仓 bar 日相同则判为平今（双倍手续费）。

        近似：以当前净持仓的整体开仓日为准（适用于单日建仓/持仓后平仓的常见情形）；
        跨多日分批建仓再部分平仓的边界情形为已知近似，影响很小。
        """
        if current == 0.0 or np.sign(delta) == np.sign(current):
            return False  # 开仓/加仓不是平今
        entry = self.open_dates.get(symbol)
        if entry is None or timestamp is None:
            return False
        d_entry = _to_date(entry)
        d_close = _to_date(timestamp)
        return d_entry is not None and d_close is not None and d_entry == d_close

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
