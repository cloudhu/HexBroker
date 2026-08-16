"""数据契约（§8.4）：``BarFrame`` 与 ``validate_bars``。

标准列名（小写）：``open, high, low, close, volume, amount, open_interest``；
复权列 ``adj_close``；原始列 ``raw_close``；标记列 ``limit_up, limit_down, is_rollover``。
索引：``MultiIndex(symbol, datetime)``，``datetime`` 为 bar **结束时刻**，tz-naive 本地时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .. import HexDataError
from ..constants import OHLCV_COLS

REQUIRED_COLS = OHLCV_COLS + ["adj_close", "raw_close"]
OPTIONAL_COLS = ["limit_up", "limit_down", "is_rollover", "adj_factor"]


@dataclass
class BarFrame:
    """K 线数据帧（数据层跨模块传递的核心契约）。"""

    df: pd.DataFrame
    freq: str = "1d"
    source: str = "unknown"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.df.index, pd.MultiIndex):
            raise HexDataError("BarFrame.df 必须为 MultiIndex(symbol, datetime)")
        if self.df.index.nlevels != 2:
            raise HexDataError("BarFrame.df 索引必须是两级 (symbol, datetime)")

    # ---- 便捷访问 ----
    @property
    def symbols(self) -> list[str]:
        return sorted(self.df.index.get_level_values("symbol").unique().tolist())

    @property
    def length(self) -> int:
        return len(self.df)

    def validate(self) -> "BarFrame":
        """校验数据契约，失败抛 ``HexDataError``。"""
        validate_bars(self.df, freq=self.freq)
        return self

    def by_symbol(self, symbol: str) -> pd.DataFrame:
        """返回某品种的 DataFrame，保留 MultiIndex(symbol, datetime) 两级索引。

        注意：使用列表选择 ``loc[[symbol]]``，标量 ``loc[symbol]`` 会丢弃该索引层级。
        """
        return self.df.loc[[symbol]].sort_index()

    def to_frame(self) -> pd.DataFrame:
        return self.df


def validate_bars(df: pd.DataFrame, freq: str = "1d") -> bool:
    """校验 BarFrame 的 DataFrame 是否符合契约。

    校验项：
    1. 两级 MultiIndex (symbol, datetime)
    2. 必须列齐全；索引单调、无重复
    3. 价格非负、high>=low、high/low 包络 open/close
    4. 缺失值策略：前向填充 ≤1 根后不得残留 NaN（除可选标记列）
    """
    if not isinstance(df.index, pd.MultiIndex) or df.index.nlevels != 2:
        raise HexDataError("索引必须为 MultiIndex(symbol, datetime)")
    syms = df.index.get_level_values(0)
    dts = df.index.get_level_values(1)
    # 重复判定必须基于完整 (symbol, datetime) 索引：
    # 多品种天然共享日期，datetime 层级本身重复是正常的。
    if df.index.has_duplicates:
        dup = df.index[df.index.duplicated()].unique()[:3].tolist()
        raise HexDataError(f"存在重复 (symbol, datetime) 索引: {dup}")

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise HexDataError(f"缺失必需列: {missing}")

    # 单调性：按 (symbol, datetime) 排序
    for sym, grp in df.groupby(level=0):
        if not grp.index.get_level_values(1).is_monotonic_increasing:
            raise HexDataError(f"品种 {sym} 的时间索引非单调")

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    if (h < l).any():
        raise HexDataError("存在 high < low 的异常 bar")
    if (h < o).any() or (h < c).any() or (l > o).any() or (l > c).any():
        raise HexDataError("存在 OHLC 包络关系被破坏的 bar")
    if (c <= 0).any() or (o <= 0).any():
        raise HexDataError("存在非正价格")

    # 缺失值：前向填充 1 根后剩余 NaN 视为错误（除可选标记列）
    fillable = df[REQUIRED_COLS].copy()
    ffill = fillable.ffill(limit=1)
    residual = ffill.isna().sum().sum()
    if residual > 0:
        raise HexDataError(f"前向填充≤1根后仍存在 {residual} 个 NaN，数据不连续")

    return True


def as_barframe(df: pd.DataFrame, freq: str = "1d", source: str = "unknown") -> BarFrame:
    """便捷构造并校验 BarFrame。"""
    return BarFrame(df=df, freq=freq, source=source).validate()
