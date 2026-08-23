"""实盘 CTP 启动守卫测试（V4 修复）：缺 AppId/AuthCode 必须拒绝启动。"""

from __future__ import annotations

import pytest

from hexbroker.live.ctp_skeleton import CTPGuardError, CTPLiveGateway


def test_missing_appid_authcode_rejects_start(monkeypatch):
    """V4 修复：穿透式监管需 AppId/AuthCode，缺任一必须 raise（不再仅 print）。"""
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "b")
    monkeypatch.setenv("HEXBROKER_CTP_USER_ID", "u")
    monkeypatch.setenv("HEXBROKER_CTP_PASSWORD", "p")
    monkeypatch.delenv("HEXBROKER_CTP_APP_ID", raising=False)
    monkeypatch.delenv("HEXBROKER_CTP_AUTH_CODE", raising=False)
    gw = CTPLiveGateway(cfg=object(), understand_risk=True)
    with pytest.raises(CTPGuardError):
        gw.start()


def test_present_appid_authcode_allows_start(monkeypatch):
    """凭证齐全时通过守卫（仍不建立真实连接，骨架阶段）。"""
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "b")
    monkeypatch.setenv("HEXBROKER_CTP_USER_ID", "u")
    monkeypatch.setenv("HEXBROKER_CTP_PASSWORD", "p")
    monkeypatch.setenv("HEXBROKER_CTP_APP_ID", "app")
    monkeypatch.setenv("HEXBROKER_CTP_AUTH_CODE", "code")
    gw = CTPLiveGateway(cfg=object(), understand_risk=True)
    gw.start()  # 不应抛（_connect_gateway 仅打印占位）
    assert gw._connected is True


def test_understand_risk_required(monkeypatch):
    """未显式 understand_risk 必须拒绝启动。"""
    gw = CTPLiveGateway(cfg=object(), understand_risk=False)
    with pytest.raises(CTPGuardError):
        gw.start()
