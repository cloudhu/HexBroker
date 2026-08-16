"""重采样：Tick/1m → 60m/1d 合成，**不跨夜盘边界聚合**（§1.1 D1）。

把交易时段切分为「日盘」与「夜盘」两个独立 session，在每个 session 内部按目标频率
分桶聚合；夜盘 21:00 起始的 bar 与次日日盘 09:00 起始的 bar 永远落在不同 session，
从而不会出现跨夜盘的 60m bar。
"""

from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from .schema import BarFrame

_DAY_OPEN = time(9, 0)
_NIGHT_OPEN = time(21, 0)


def _session_key(elem) -> tuple[str, int]:
    """返回 (session_label, session_local_minute_offset_from_session_open)。

    兼容 MultiIndex（label 为 (symbol, datetime) 元组）与单列 DatetimeIndex。
    """
    ts = elem[1] if isinstance(elem, tuple) else elem
    t = pd.Timestamp(ts).time()
    date = ts.date()
    if _NIGHT_OPEN <= t <= time(23, 0):
        # 夜盘：以当天 21:00 为 session 起点
        start = pd.Timestamp.combine(date, _NIGHT_OPEN)
        offset = int((ts - start).total_seconds() // 60)
        return (f"{date.isoformat()}-night", offset)
    # 日盘：以当天 09:00 为 session 起点
    start = pd.Timestamp.combine(date, _DAY_OPEN)
    offset = int((ts - start).total_seconds() // 60)
    return (f"{date.isoformat()}-day", offset)


def _aggregate(group: pd.DataFrame) -> pd.Series:
    """单组聚合为 1 根 bar。"""
    return pd.Series(
        {
            "open": group["open"].iloc[0],
            "high": group["high"].max(),
            "low": group["low"].min(),
            "close": group["close"].iloc[-1],
            "volume": group["volume"].sum(),
            "amount": group["amount"].sum(),
            "open_interest": group["open_interest"].iloc[-1],
            "raw_close": group["raw_close"].iloc[-1] if "raw_close" in group else group["close"].iloc[-1],
        }
    )


def resample_to_freq(bars: BarFrame, target_freq: str = "60m") -> BarFrame:
    """把高频 BarFrame 重采样为目标频率（默认 60m），不跨夜盘边界。

    返回的 bar 时间戳为「该桶结束时刻」（左闭右开约定）。
    """
    minutes = _freq_to_minutes(target_freq)
    parts = []
    for sym in bars.symbols:
        grp = bars.by_symbol(sym).sort_index()
        records = []
        # 计算 session key 与桶序号
        keys = grp.index.map(_session_key)
        labels = [k[0] for k in keys]
        offsets = np.array([k[1] for k in keys])
        bucket = (offsets // minutes).astype(int)
        df = grp.copy()
        df["_label"] = labels
        df["_bucket"] = bucket
        for (lab, b), g in df.groupby(["_label", "_bucket"]):
            agg = _aggregate(g)
            # session 起点时间
            date_part, sess = lab.rsplit("-", 1)
            base = pd.Timestamp.combine(pd.Timestamp(date_part).date(), _NIGHT_OPEN if sess == "night" else _DAY_OPEN)
            bar_end = base + pd.Timedelta(minutes=int(b + 1) * minutes)
            agg["symbol"] = sym
            agg["datetime"] = bar_end
            agg["adj_close"] = agg["close"]
            agg["limit_up"] = False
            agg["limit_down"] = False
            agg["is_rollover"] = False
            records.append(agg)
        res = pd.DataFrame(records)
        res = res.set_index(["symbol", "datetime"]).sort_index()
        parts.append(res)
    out = pd.concat(parts)
    return BarFrame(df=out, freq=target_freq, source=bars.source)


def _freq_to_minutes(freq: str) -> int:
    mapping = {"60m": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1, "1d": 1440}
    if freq not in mapping:
        raise ValueError(f"不支持的重采样频率: {freq}")
    return mapping[freq]
