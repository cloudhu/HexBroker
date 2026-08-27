"""实盘层（§3.8 / §8.8）：受控 CTP 骨架。

**安全约束（不可绕过）**：
- 启动必须显式传 ``--i-understand-the-risk``（否则拒绝启动）；
- 必须提供期货账户凭证（broker/账号/密码/AppId/认证码），缺任一拒绝启动；
- 骨架只做「连接前校验 + 风控包装 + 下单接口占位」，**不包含真实 CTP 网络逻辑**——
  实际接入 vn.py CTP 网关时，在 ``_connect_gateway`` 内实现（需自行向期货公司报备穿透式监管）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .gateway import BrokerGateway, Position, Account
from ..risk.manager import RiskManager


class CTPGuardError(RuntimeError):
    """CTP 启动守卫错误。"""


class CTPCapabilityPrepOnly(NotImplementedError):
    """能力准备占位：真实 CTP/SimNow 接入为后续业务授权项（穿透式监管报备）。"""


@dataclass
class CTPCredentials:
    """实盘凭证（从环境变量读取，禁止硬编码）。"""

    broker_id: str
    user_id: str
    password: str
    app_id: str = ""
    auth_code: str = ""

    @classmethod
    def from_env(cls) -> "CTPCredentials":
        required = ["HEXBROKER_CTP_BROKER_ID", "HEXBROKER_CTP_USER_ID", "HEXBROKER_CTP_PASSWORD"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise CTPGuardError(f"缺少实盘凭证环境变量：{missing}")
        return cls(
            broker_id=os.environ["HEXBROKER_CTP_BROKER_ID"],
            user_id=os.environ["HEXBROKER_CTP_USER_ID"],
            password=os.environ["HEXBROKER_CTP_PASSWORD"],
            app_id=os.environ.get("HEXBROKER_CTP_APP_ID", ""),
            auth_code=os.environ.get("HEXBROKER_CTP_AUTH_CODE", ""),
        )


class CTPLiveGateway(BrokerGateway):
    """受控实盘网关骨架（P1-5：实现 BrokerGateway 抽象，零依赖 vnpy_ctp）。

    用法（仅示例，勿在无凭证时运行）::

        gw = CTPLiveGateway(cfg, understand_risk=True)
        gw.start()   # 校验通过后进入连接占位
    """

    def __init__(self, cfg: Any, understand_risk: bool = False) -> None:
        self.cfg = cfg
        self.understand_risk = bool(understand_risk)
        self.risk = RiskManager(cfg)
        self.credentials: Optional[CTPCredentials] = None
        self._connected = False

    def start(self) -> None:
        if not self.understand_risk:
            raise CTPGuardError(
                "实盘启动被拒绝：必须显式传 --i-understand-the-risk。"
                "实盘交易可能导致真实资金损失，请确认理解全部风险。"
            )
        self.credentials = CTPCredentials.from_env()
        if not self.credentials.app_id or not self.credentials.auth_code:
            # 穿透式监管红线（模块 docstring）：缺 AppId/AuthCode 必须拒绝启动，
            # 不得仅打印后继续连接——避免误启动真实网关。
            raise CTPGuardError(
                "实盘启动被拒绝：缺 AppId/AuthCode（穿透式监管报备所需）。"
                "请向期货公司申请后设置 HEXBROKER_CTP_APP_ID / HEXBROKER_CTP_AUTH_CODE。"
            )
        self._connect_gateway()

    def _connect_gateway(self) -> None:
        """连接占位：接入 vn.py CTP 网关时在此实现（含穿透式监管）。

        注：保持既有 549 测试 ``test_present_appid_authcode_allows_start`` 绿，
        ``start()`` 在校验通过后不抛异常、置 ``_connected=True``（仅能力准备，不建真实连接）。
        真实连接逻辑（含 vnpy_ctp 接入）为后续业务授权项，由 ``CTPCapabilityPrepOnly`` 表达。
        """
        # TODO(实盘): 引入 vnpy_ctp，加载 CTP 网关并连接；加载前先完成监管报备。
        # 骨架阶段不建立真实连接，只打印校验通过。
        print("[CTP] 凭证校验通过（能力准备阶段：未建立真实连接；真实 CTP 接入为后续业务授权项）。")
        self._connected = True

    # ------------------------------------------------------------------
    # BrokerGateway 抽象实现（骨架阶段均拒绝真实动作）
    # ------------------------------------------------------------------
    def connect(self) -> None:
        self.start()

    def disconnect(self) -> None:
        self._connected = False

    def query_position(self, symbol: str) -> Position:
        raise CTPGuardError("骨架阶段未建立真实连接，无法查询持仓")

    def query_account(self) -> Account:
        raise CTPGuardError("骨架阶段未建立真实连接，无法查询账户")

    def cancel_order(self, order_id: str) -> bool:
        raise CTPGuardError("骨架阶段禁止撤单（未建立真实连接）")

    def submit_order(self, symbol: str, direction: int, qty: float) -> None:
        """下单占位：骨架阶段拒绝真实下单。"""
        if not self._connected:
            raise CTPGuardError("网关未连接，禁止下单")
        raise CTPGuardError(
            "骨架阶段禁止真实下单。接入 vn.py CTP 网关后，"
            "请在此处实现订单路由 + 风控二次校验（RiskManager.evaluate）。"
        )
