"""数据源实现集合：CSV（默认离线）/ pytdx（主力免费源）/ sina（新浪免费源）/
TQSDK / AkShare / Qlib（可选）。

未安装依赖的数据源在 ``health_check()`` 返回 False，且 ``fetch_bars`` 抛明确异常，
不影响 CSV 路径（§T01 验收 6）。
"""

from .akshare_source import AkshareSource
from .csv_source import CsvSource
from .pytdx_source import PytdxSource
from .qlib_source import QlibSource
from .sina_source import SinaSource
from .tqsdk_source import TqsdkSource

__all__ = [
    "AkshareSource",
    "CsvSource",
    "PytdxSource",
    "SinaSource",
    "TqsdkSource",
]
