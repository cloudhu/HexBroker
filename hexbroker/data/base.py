"""数据源抽象基类（§2.2）。

所有数据源实现 ``fetch_bars(symbols, start, end, freq) -> BarFrame``，
并通过 ``health_check()`` 报告可用性（缺失依赖时优雅降级，不抛异常）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from .schema import BarFrame


class DataSource(ABC):
    """数据源基类。"""

    #: 数据源名称（用于日志与路由）
    name: str = "base"

    @abstractmethod
    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        """拉取指定品种/区间/频率的 BarFrame。"""

    def health_check(self) -> bool:
        """探测数据源是否可用（默认 True）。子类可覆盖。"""
        return True

    @staticmethod
    def _clip_range(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
        """按 datetime 级别裁剪到 [start, end]。"""
        dts = df.index.get_level_values("datetime")
        mask = (dts >= pd.Timestamp(start)) & (dts <= pd.Timestamp(end))
        return df[mask]
