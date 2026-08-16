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


class HexLeakageError(HexError):
    """检测到信息泄漏（防泄漏红线）。一旦抛出必须终止运行。"""


class HexRiskError(HexError):
    """风控规则执行异常。"""


__all__ = [
    "__version__",
    "HexError",
    "HexConfigError",
    "HexDataError",
    "HexLeakageError",
    "HexRiskError",
]
