"""实盘层（§3.8 / §8.8 / P1-5）。

- ``gateway.BrokerGateway``：统一交易通道抽象（回测/模拟/实盘三级共用执行契约）。
- ``gateway.LiveBroker``：Protocol（execute 同语义）。
- ``gateway.{Fake,Sim,Paper}BrokerGateway``：薄适配实现。
- ``ctp_skeleton.CTPLiveGateway``：受控实盘骨架（--i-understand-the-risk 守卫 + 凭证校验）。
- ``vnpy_ctp_glue``：可选 glue 骨架（函数内 try import vnpy_ctp，顶层绝不 import）。

默认不接实盘；接入需期货账户/AppId 并向期货公司报备穿透式监管。
"""

from __future__ import annotations

from .ctp_skeleton import (
    CTPCapabilityPrepOnly,
    CTPCredentials,
    CTPGuardError,
    CTPLiveGateway,
)
from .gateway import (
    Account,
    BrokerGateway,
    FakeBrokerGateway,
    LiveBroker,
    Order,
    PaperBrokerGateway,
    Position,
    SimBrokerGateway,
)
from .vnpy_ctp_glue import build_vnpy_ctp_gateway

__all__ = [
    "BrokerGateway",
    "LiveBroker",
    "Position",
    "Account",
    "Order",
    "FakeBrokerGateway",
    "SimBrokerGateway",
    "PaperBrokerGateway",
    "CTPLiveGateway",
    "CTPGuardError",
    "CTPCredentials",
    "CTPCapabilityPrepOnly",
    "build_vnpy_ctp_gateway",
]
