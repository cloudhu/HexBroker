"""期货交易日历（§1.1 D1）：交易日 / 日盘夜盘时段 / 下一 bar 时刻。

MVP 采用通用商品期货时段（多数品种）：
- 日盘：09:00–10:15, 10:30–11:30, 13:30–15:00
- 夜盘：21:00–23:00（部分品种到次日 02:30，此处以 21:00–23:00 为通用近似）

``is_trading_time`` 同时用于实盘/回测中「是否为有效交易时段」判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import pandas as pd

from ..utils.timeutil import next_bar_ts as _next_bar_ts


@dataclass
class Session:
    """交易时段（含跨日标志）。"""

    start: time
    end: time
    crosses_midnight: bool = False

    def __contains__(self, t: time) -> bool:
        if not self.crosses_midnight:
            return self.start <= t <= self.end
        # 跨午夜：如 21:00–02:30
        return t >= self.start or t <= self.end


# 通用商品期货时段
_DAY_SESSIONS = [
    Session(time(9, 0), time(10, 15)),
    Session(time(10, 30), time(11, 30)),
    Session(time(13, 30), time(15, 0)),
]
_NIGHT_SESSIONS = [
    Session(time(21, 0), time(23, 0)),
]


class FuturesCalendar:
    """期货交易日历。"""

    def __init__(self, symbol_sessions: Optional[dict[str, list[Session]]] = None) -> None:
        self._symbol_sessions = symbol_sessions or {}

    def sessions(self, symbol: str = "") -> list[Session]:
        return self._symbol_sessions.get(symbol, _DAY_SESSIONS + _NIGHT_SESSIONS)

    def is_trading_time(self, ts: object, symbol: str = "") -> bool:
        """判断 ``ts`` 是否落在交易时段内。"""
        t = pd.Timestamp(ts).time()
        return any(t in s for s in self.sessions(symbol))

    def next_bar_ts(self, ts: object, freq: str = "1d") -> pd.Timestamp:
        """下一根 bar 的结束时刻（委托 timeutil）。"""
        return _next_bar_ts(ts, freq)

    def is_trading_day(self, ts: object) -> bool:
        """简版：周一至周五视为交易日。"""
        return pd.Timestamp(ts).weekday() < 5
