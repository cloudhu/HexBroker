"""HexFutures-AI —— 中国商品期货高胜率预测 + RL 双层 + 自回归进化研究框架。

包级导出与版本声明。所有跨层数据契约集中在 ``hexbroker.data.schema``、
``hexbroker.forecast.base``、``hexbroker.risk.types``、``hexbroker.evaluation.report``。
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "software-hexfutures-ai"

# 包级异常（跨层统一错误分层）
class HexError(Exception):
    """所有 HexFutures-AI 自定义异常的基类。"""


class HexConfigError(HexError):
    """配置错误（含 Kronos tokenizer/模型配对校验失败）。"""


class HexDataError(HexError):
    """数据契约校验失败。"""


class HexEmptyDataError(HexDataError):
    """取数成功但结果为 0 行。

    与"网络失败"区分：本异常表示链路通、请求成功，但没有任何数据返回
    （如请求区间内无交易日、或源本身已停更导致裁剪后为空）。
    """

    def __init__(self, message: str, *, source: str = "", symbol: str = "") -> None:
        super().__init__(message)
        self.source = source
        self.symbol = symbol


class HexStaleDataError(HexDataError):
    """取到数据但最新日期落后于期望日期（数据陈旧）。

    典型场景：数据源停更，请求 ``[start, end]`` 拿到的是数月前的最后一根 bar，
    被误当作"刷新成功"。这是 2026-08-28 停摆事故的故障模式。
    """

    def __init__(
        self,
        message: str,
        *,
        source: str = "",
        symbol: str = "",
        latest: str = "",
        expected: str = "",
    ) -> None:
        super().__init__(message)
        self.source = source
        self.symbol = symbol
        self.latest = latest
        self.expected = expected


class HexQuotaError(HexDataError):
    """数据源配额/额度耗尽（可重试，通常需等待配额重置）。"""


class HexNetworkError(HexDataError):
    """网络层失败（连接超时、DNS、被 WAF 拦截等），与"数据为空"区分。"""


class HexLeakageError(HexError):
    """检测到信息泄漏（防泄漏红线）。一旦抛出必须终止运行。"""


class HexRiskError(HexError):
    """风控规则执行异常。"""


__all__ = [
    "__version__",
    "HexError",
    "HexConfigError",
    "HexDataError",
    "HexEmptyDataError",
    "HexStaleDataError",
    "HexQuotaError",
    "HexNetworkError",
    "HexLeakageError",
    "HexRiskError",
]
