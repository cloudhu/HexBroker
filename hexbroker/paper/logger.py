"""交易日志（§3.1 TradeLogger / A3 / §7.3）。

双输出（命令窗口 + 日志文件同一 sink 内容一致）：
- 成交 5 要素：``TRADE|ts|symbol|dir|qty|entry|stop|tp|price|fee``
- 计划变更：``PLAN|ts|symbol|change_type|detail``
- 审计 JSON：``log_structured("trade"/"plan_change", {...})``
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Optional

from loguru import logger

from ..utils.logging import log_structured
from .types import EVT_PLAN_CHANGE, EVT_TRADE, PlanChange, TradeEvent

# 模块级 sink 注册标记（避免测试/多次实例化重复添加）
_SINKS_READY = False
_ADDED_FILES: set[str] = set()


def _ensure_sinks(log_file: str) -> None:
    """注册双输出 sink（幂等）。

    与 ``utils.logging.init_logging`` 共存：只移除 loguru 默认 stderr sink（id 0，
    避免双 stderr 重复输出），**保留其它模块注册的 sink**——避免 init_logging 先
    注册的 run 日志 sink 被本模块 ``logger.remove()`` 误清（QA P2 提示③）。
    """
    global _SINKS_READY
    fmt = "{time:YYYY-MM-DD HH:mm:ss} | {message}"
    if not _SINKS_READY:
        try:
            logger.remove(0)  # 仅移除默认 stderr（若已被其它模块移除则跳过）
        except (ValueError, TypeError):
            pass
        logger.add(sys.stderr, level="INFO", format=fmt, colorize=False)
        _SINKS_READY = True
    if log_file not in _ADDED_FILES:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file),
            level="INFO",
            format=fmt,
            encoding="utf-8",
            rotation="10 MB",
            enqueue=False,
        )
        _ADDED_FILES.add(log_file)


def _fmt_stop(value: Optional[float]) -> str:
    return f"{value:g}" if value is not None else ""


class TradeLogger:
    """双输出日志器（文件 + stdout）。"""

    def __init__(self, log_file: str | Path = "data/paper/trades.log") -> None:
        self._log_file = str(log_file)
        _ensure_sinks(self._log_file)

    # ------------------------------------------------------------------
    # 成交
    # ------------------------------------------------------------------
    def trade(self, event: TradeEvent) -> None:
        """输出成交 5 要素（A3）+ 结构化审计。"""
        line = (
            f"TRADE|{event.ts.isoformat(timespec='seconds')}|{event.symbol}|"
            f"{event.direction_label()}|{event.qty:g}|{event.entry:g}|"
            f"{_fmt_stop(event.stop)}|{_fmt_stop(event.take_profit)}|"
            f"{event.price:g}|{event.fee:.6f}"
        )
        logger.info(line)
        log_structured(EVT_TRADE, event.to_dict())

    # ------------------------------------------------------------------
    # 计划变更
    # ------------------------------------------------------------------
    def plan_change(self, change: PlanChange) -> None:
        line = (
            f"PLAN|{change.ts.isoformat(timespec='seconds')}|{change.symbol}|"
            f"{change.change_type}|{change.detail}"
        )
        logger.info(line)
        log_structured(EVT_PLAN_CHANGE, change.to_dict())

    # ------------------------------------------------------------------
    # 每日摘要（命令窗口）
    # ------------------------------------------------------------------
    def daily_summary(self, day: date, trades: list[TradeEvent], acct=None) -> None:
        n = len(trades)
        line = f"[复盘] {day.isoformat()} 交易日：成交 {n} 笔"
        if acct is not None:
            line += (
                f"，权益 {acct.equity:.2f}，可用资金 {acct.cash:.2f}，"
                f"保证金 {acct.margin_used:.2f}，回撤 {acct.drawdown * 100:.2f}%"
            )
        logger.info(line)
