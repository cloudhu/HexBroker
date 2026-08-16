"""信号阈值映射（§3.2 / §3.5）。

把校准后的 ``p_up`` 映射为交易方向 ``SignalDirection``，构成
「信号阈值」基线策略，也是 RL 对齐奖励的参照基准。
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from ..constants import SignalDirection
from .base import ForecastSignal


class SignalThreshold:
    """阈值 -> 方向（多头 / 空头 / 空仓）。"""

    def __init__(self, long_thr: float = 0.55, short_thr: float = 0.45, require_effective: bool = True) -> None:
        self.long_thr = float(long_thr)
        self.short_thr = float(short_thr)
        self.require_effective = bool(require_effective)

    def direction(self, signal: ForecastSignal) -> SignalDirection:
        if self.require_effective and not signal.is_effective:
            return SignalDirection.FLAT
        if signal.p_up >= self.long_thr:
            return SignalDirection.LONG
        if signal.p_up <= self.short_thr:
            return SignalDirection.SHORT
        return SignalDirection.FLAT

    def to_frame(self, signals: Iterable[ForecastSignal]) -> pd.DataFrame:
        """返回 MultiIndex(symbol, datetime) 的方向 DataFrame（列 ``direction``）。"""
        rows = []
        for s in signals:
            rows.append(
                {
                    "symbol": s.symbol,
                    "datetime": s.ts,
                    "direction": int(self.direction(s)),
                    "p_up": s.p_up,
                    "exp_ret": s.exp_ret,
                    "conf": s.conf,
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(
                columns=["direction", "p_up", "exp_ret", "conf"],
                index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "datetime"]),
            )
        df = df.set_index(["symbol", "datetime"]).sort_index()
        df.index = df.index.set_names(["symbol", "datetime"])
        return df


def apply_threshold(signals: Iterable[ForecastSignal], long_thr: float = 0.55, short_thr: float = 0.45) -> list[int]:
    """便捷函数：返回每个信号的方向整数列表。"""
    return [int(SignalThreshold(long_thr, short_thr).direction(s)) for s in signals]
