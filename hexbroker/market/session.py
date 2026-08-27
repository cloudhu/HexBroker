"""交易时段 / 交易日标签共享模块（§3 / P1-9）。

复用 ``hexbroker.data.calendar.Session``（已支持 crosses_midnight），并提供纯函数
``day_label``（原 paper ``TradingSession.day_label`` 的共享实现）。paper ``TradingSession``
委托本模块，消除 ``data.calendar`` 与 ``paper/sessions`` 的重复实现。

时间口径：全系统 tz-naive；夜盘（>= night_boundary，默认 21:00）归属下一交易日。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Optional, cast

from ..data.calendar import Session

# 共享模块导出：Session 复用 data.calendar；day_label 为共享交易日标签实现
__all__ = ["day_label", "Session"]


def _to_date(ts: Any) -> Optional[date]:
    """把时间戳归一为 date（date / datetime / pd.Timestamp / ISO 字符串）。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return cast(date, ts.date())
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


def _is_trading_day(d: date, holidays: Optional[set[date]]) -> bool:
    """是否为交易日：周一至周五且不在节假日表内。"""
    return d.weekday() < 5 and d not in (holidays or set())


def _pd_time(ts: Any) -> time:
    """时间戳 → time（兼容 pd.Timestamp）。"""
    if isinstance(ts, datetime):
        return ts.time()
    try:
        import pandas as pd

        return cast(time, pd.Timestamp(ts).time())
    except Exception:
        return datetime.fromisoformat(str(ts)).time()


def day_label(
    ts: Any,
    night_boundary: time,
    holidays: Optional[set[date]],
) -> Optional[date]:
    """交易日标签：夜盘（>= night_boundary）归属下一交易日，跳过周末/节假日。

    与 paper ``TradingSession.day_label`` 行为一致（委托此处实现，消除重复）。
    """
    d = _to_date(ts)
    if d is None:
        return None
    t = _pd_time(ts)
    if t >= night_boundary:
        d = d + timedelta(days=1)
    while not _is_trading_day(d, holidays):
        d = d + timedelta(days=1)
    return d
