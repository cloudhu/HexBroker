"""Qlib 数据源 adapter（可选依赖，import 失败时优雅降级）。"""

from __future__ import annotations

from typing import Optional

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame


class QlibSource(DataSource):
    """Qlib 数据/表达式 adapter（可选）。"""

    name = "qlib"

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        try:
            import qlib  # noqa: F401
        except ImportError as e:
            raise HexConfigError(
                "未安装 pyqlib，无法使用 Qlib 数据源。请于独立虚拟环境 pip install pyqlib。"
            ) from e
        # adapter 骨架
        raise HexDataError("QlibSource 为 adapter 骨架，需在隔离环境实现表达式数据读取。")

    def health_check(self) -> bool:
        try:
            import qlib  # noqa: F401

            return True
        except ImportError:
            return False
