"""vnpy_ctp 可选 glue 骨架（§3 / P1-5）。

**红线**：本模块顶层绝不 ``import vnpy_ctp``；仅在 ``build_vnpy_ctp_gateway`` 函数体内
``try import``。vnpy_ctp 属后续业务授权项（需穿透式监管报备），未安装即抛 RuntimeError。
"""

from __future__ import annotations

from typing import Any

from .ctp_skeleton import CTPCapabilityPrepOnly, CTPGuardError
from .gateway import BrokerGateway


def build_vnpy_ctp_gateway(
    cfg: Any, understand_risk: bool = False
) -> "BrokerGateway":
    """构造 vnpy_ctp 适配网关（函数内 try import，绝不顶层 import）。

    未安装 vnpy_ctp → 抛 RuntimeError（提示走 optional 依赖 + 穿透式监管报备）。
    已安装 → 仍仅做能力准备占位（真实接线为后续里程碑）。
    """
    try:
        import vnpy_ctp  # 仅函数内 import，红线纪律
    except ImportError as exc:
        raise RuntimeError(
            "vnpy_ctp 未安装，属后续业务授权项"
            "（pip install -e .[optional] 后仍需穿透式监管报备）"
        ) from exc
    # 已安装但本里程碑仅做能力准备：真实网关接线（CTP 行情/交易 API 订阅、回调注册）
    # 为后续里程碑实现，此处显式抛能力准备占位。
    raise CTPCapabilityPrepOnly("vnpy_ctp 适配骨架已就位，真实接线为后续里程碑")
