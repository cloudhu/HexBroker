"""模拟盘交易系统（实时模拟盘包）。

管道：报价 → 信号 → 风控 → 计划 → 撮合 → 日志 → 情报 → 复盘。
组件间用 :mod:`hexbroker.paper.types` 的 DTO 解耦；配置见 ``configs/paper.yaml``。

入口：``scripts/paper_trading_main.py`` / ``start_paper_trading.bat``。
"""

from __future__ import annotations

from .types import (
    AccountSnapshot,
    NewsItem,
    Plan,
    PlanChange,
    PositionCtx,
    Quote,
    SignalFrame,
    TradeEvent,
)

__all__ = [
    "AccountSnapshot",
    "NewsItem",
    "Plan",
    "PlanChange",
    "PositionCtx",
    "Quote",
    "SignalFrame",
    "TradeEvent",
]
