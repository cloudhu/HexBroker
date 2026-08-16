"""T05 实盘骨架守卫测试：无 --i-understand-the-risk 拒绝启动；无凭证拒绝连接；骨架拒绝真实下单。"""

from __future__ import annotations

import os

import pytest

from hexbroker.config import load_config
from hexbroker.live.ctp_skeleton import CTPGuardError, CTPLiveGateway, CTPCredentials


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("HEXBROKER_CTP_BROKER_ID", "HEXBROKER_CTP_USER_ID", "HEXBROKER_CTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


def test_refuses_without_risk_flag():
    gw = CTPLiveGateway(load_config(), understand_risk=False)
    with pytest.raises(CTPGuardError, match="i-understand-the-risk"):
        gw.start()


def test_refuses_without_credentials():
    gw = CTPLiveGateway(load_config(), understand_risk=True)
    with pytest.raises(CTPGuardError, match="环境变量"):
        gw.start()


def test_accepts_with_credentials_and_flag(monkeypatch):
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "9999")
    monkeypatch.setenv("HEXBROKER_CTP_USER_ID", "demo")
    monkeypatch.setenv("HEXBROKER_CTP_PASSWORD", "pw")
    gw = CTPLiveGateway(load_config(), understand_risk=True)
    gw.start()  # 骨架阶段只校验，不建真实连接
    assert gw._connected is True


def test_skeleton_refuses_real_order(monkeypatch):
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "9999")
    monkeypatch.setenv("HEXBROKER_CTP_USER_ID", "demo")
    monkeypatch.setenv("HEXBROKER_CTP_PASSWORD", "pw")
    gw = CTPLiveGateway(load_config(), understand_risk=True)
    gw.start()
    with pytest.raises(CTPGuardError, match="真实下单"):
        gw.submit_order("SHFE.cu", direction=1, qty=1)


def test_credentials_from_env_requires_all(monkeypatch):
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "9999")
    with pytest.raises(CTPGuardError):
        CTPCredentials.from_env()
