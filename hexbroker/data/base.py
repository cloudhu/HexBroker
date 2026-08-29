"""数据源抽象基类（§2.2）。

所有数据源实现 ``fetch_bars(symbols, start, end, freq) -> BarFrame``，
并通过 ``health_check()`` 报告可用性（缺失依赖时优雅降级，不抛异常）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

from .freshness import DEFAULT_MAX_STALE_DAYS, check_fetch_result
from .schema import BarFrame


class DataSource(ABC):
    """数据源基类。"""

    #: 数据源名称（用于日志与路由）
    name: str = "base"

    #: 结果门禁默认容忍天数（日历日）。子类可在 ``__init__`` 中覆盖。
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS

    #: 是否启用新鲜度门禁。离线/合成源可置 False。
    check_freshness: bool = True

    #: 新鲜度判定的"当前日期"注入口，默认 None 表示取 ``date.today()``。
    #: 仅供单测确定性使用，生产环境不要设置。
    today: date | None = None

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

    def _finalize(
        self,
        out: pd.DataFrame,
        start: str,
        end: str,
        freq: str = "1d",
        *,
        symbols: object = None,
        source: str | None = None,
        check_freshness: bool | None = None,
        max_stale_days: int | None = None,
        allow_empty: bool = False,
    ) -> BarFrame:
        """裁剪 → 空结果/新鲜度门禁 → 构造 BarFrame → 契约校验。

        所有 ``fetch_bars`` 实现都应走这里收口，避免"裁剪成 0 行却静默成功"。

        * ``check_freshness`` / ``max_stale_days`` 为 ``None`` 时取实例属性。
        * 历史回填（``end`` 早于 今日-容差）自动豁免新鲜度判定。
        """
        src = source or self.name
        clipped = self._clip_range(out, start, end)
        if not allow_empty or len(clipped) > 0:
            check_fetch_result(
                clipped,
                end,
                source=src,
                symbols=symbols,
                max_stale_days=(
                    self.max_stale_days if max_stale_days is None else max_stale_days
                ),
                check_freshness=(
                    self.check_freshness if check_freshness is None else check_freshness
                ),
                today=self.today,
            )
        return BarFrame(df=clipped, freq=freq, source=src).validate(
            allow_empty=allow_empty
        )
