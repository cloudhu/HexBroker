"""时间工具（§8.3）。

- bar 时间戳 = bar **结束时刻**，区间左闭右开。
- 全系统时区 ``Asia/Shanghai``，存储为 tz-naive 本地时间。
- 提供「禁止同 bar 成交」判定与下一 bar 时刻推算。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

_FREQ_DELTA: dict[str, timedelta] = {
    "1d": timedelta(days=1),
    "60m": timedelta(hours=1),
    "30m": timedelta(minutes=30),
    "5m": timedelta(minutes=5),
    "1m": timedelta(minutes=1),
    "tick": timedelta(seconds=1),
}


def normalize_ts(ts: Any) -> pd.Timestamp:
    """把任意时间输入规范为 tz-naive 的 ``pd.Timestamp``。"""
    return pd.Timestamp(ts).tz_localize(None)


def make_bar_endindex(dates: Iterable[Any], freq: str = "1d") -> pd.DatetimeIndex:
    """构造以「结束时刻」标注的 DatetimeIndex（左闭右开）。"""
    arr = sorted(pd.Timestamp(d).tz_localize(None) for d in dates)
    return pd.DatetimeIndex(arr, name="datetime")


def next_bar_ts(ts: Any, freq: str = "1d") -> pd.Timestamp:
    """推算下一根 bar 的结束时刻。"""
    return normalize_ts(ts) + _FREQ_DELTA.get(freq, timedelta(days=1))


def prev_bar_ts(ts: Any, freq: str = "1d") -> pd.Timestamp:
    """推算上一根 bar 的结束时刻。"""
    return normalize_ts(ts) - _FREQ_DELTA.get(freq, timedelta(days=1))


def can_execute_on_bar(signal_ts: Any, bar_ts: Any, freq: str = "1d") -> bool:
    """判断信号能否在该 bar 执行：必须「下一根及之后」才允许成交（禁止同 bar 成交）。"""
    return normalize_ts(bar_ts) >= next_bar_ts(signal_ts, freq)


def to_serializable(dt: Any) -> str:
    """Timestamp -> ISO 字符串，便于落盘。"""
    return normalize_ts(dt).isoformat()
