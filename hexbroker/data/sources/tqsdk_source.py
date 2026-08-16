"""天勤 TQSDK 数据源（可选，仅留接口；默认不启用）。

依赖缺失时 ``health_check`` 返回 False，``fetch_bars`` 抛明确异常，不影响 CSV 路径。
"""

from __future__ import annotations

from typing import Optional

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame


class TqsdkSource(DataSource):
    """TQSDK 主力/指数/单合约行情源（骨架）。"""

    name = "tqsdk"

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None) -> None:
        self.username = username
        self.password = password
        self._api = None

    def _import_tqsdk(self):
        try:
            import tqsdk  # noqa: F401
        except ImportError as e:
            raise HexConfigError(
                "未安装 tqsdk，无法使用 TQSDK 数据源。请 pip install tqsdk，或使用 --source csv。"
            ) from e

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        self._import_tqsdk()
        # 实际接入需账户与实时下载，MVP 留骨架
        raise HexDataError(
            "TQSDK 数据源为骨架实现：需配置账户并接入行情下载逻辑（见 docs/system_design.md §2.2）。"
        )

    def health_check(self) -> bool:
        try:
            import tqsdk  # noqa: F401

            return True
        except ImportError:
            return False
