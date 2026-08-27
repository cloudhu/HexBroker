"""中国市场规则包（§3 / P1-9）。

- ``MarketRule`` / ``MarketRuleTable``：分品种保证金率 / 涨跌停幅度 / 交割月禁开仓规则表。
- ``Session`` / ``day_label``：复用 ``data.calendar`` 的交易日历原语，提升为全系统共享模块
  （paper ``TradingSession`` 的 ``day_label`` 委托本模块，消除重复实现）。
"""

from __future__ import annotations

from .rule import MarketRule, MarketRuleTable
from .session import Session, day_label

__all__ = ["MarketRule", "MarketRuleTable", "Session", "day_label"]
