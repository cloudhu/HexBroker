"""P1-5 交易通道抽象测试（A5.1~A5.6）。

A5.1：BrokerGateway ABC 契约（Fake/Sim/Paper 均为 BrokerGateway）；
A5.2：零依赖纪律（live 包顶层不 import vnpy_ctp）；
A5.3：FakeBrokerGateway submit→query→cancel 往返；
A5.4：LiveBroker Protocol 执行契约（execute 同语义）；
A5.5：Sim/Paper 薄适配（不改被包对象）；
A5.6：CTPLiveGateway 继承 BrokerGateway 且守卫行为不变；vnpy_ctp_glue 未安装即抛。
"""

from __future__ import annotations

import pytest

from hexbroker.backtest.cost import CostModel
from hexbroker.live import (
    Account,
    BrokerGateway,
    FakeBrokerGateway,
    LiveBroker,
    Order,
    PaperBrokerGateway,
    Position,
    SimBrokerGateway,
    build_vnpy_ctp_gateway,
)
from hexbroker.live.ctp_skeleton import CTPGuardError, CTPLiveGateway
from hexbroker.paper.broker import PaperBroker


# ---------------------------------------------------------------------------
# A5.1 ABC 契约
# ---------------------------------------------------------------------------
def test_adapters_are_broker_gateways():
    assert isinstance(FakeBrokerGateway(), BrokerGateway)
    assert isinstance(SimBrokerGateway(CostModel()), BrokerGateway)
    assert isinstance(PaperBrokerGateway(PaperBroker(CostModel())), BrokerGateway)
    assert isinstance(CTPLiveGateway(cfg=object(), understand_risk=False), BrokerGateway)


# ---------------------------------------------------------------------------
# A5.2 零依赖纪律：live 包顶层不 import vnpy_ctp
# ---------------------------------------------------------------------------
def test_no_top_level_vnpy_ctp_import():
    import importlib
    import hexbroker.live.gateway as gw_mod
    import hexbroker.live as live_pkg

    for mod in (live_pkg, gw_mod):
        assert "vnpy_ctp" not in getattr(mod, "__dict__", {}), "live 包顶层禁止 import vnpy_ctp"


# ---------------------------------------------------------------------------
# A5.3 FakeBrokerGateway 往返
# ---------------------------------------------------------------------------
def test_fake_gateway_submit_query_cancel_roundtrip():
    gw = FakeBrokerGateway(initial_capital=200_000.0)
    oid = gw.submit_order(Order("X", direction=1, qty=10, ref_price=100.0))
    assert isinstance(oid, str) and oid
    pos = gw.query_position("X")
    assert isinstance(pos, Position)
    assert pos.qty == pytest.approx(10.0)
    assert gw.cancel_order(oid) is True
    assert gw.cancel_order(oid) is False  # 已移除


def test_fake_gateway_execute_contract():
    gw = FakeBrokerGateway()
    gw.execute("X", 5.0, 100.0)
    assert gw.query_position("X").qty == pytest.approx(5.0)
    gw.execute("X", 0.0, 100.0)  # 平仓
    assert gw.query_position("X").qty == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# A5.4 LiveBroker 执行契约
# ---------------------------------------------------------------------------
def test_adapters_satisfy_live_broker_protocol():
    assert isinstance(FakeBrokerGateway(), LiveBroker)
    assert isinstance(SimBrokerGateway(CostModel()), LiveBroker)
    assert isinstance(PaperBrokerGateway(PaperBroker(CostModel())), LiveBroker)


# ---------------------------------------------------------------------------
# A5.5 Sim / Paper 薄适配（不改被包对象）
# ---------------------------------------------------------------------------
def test_sim_gateway_wraps_simbroker():
    gw = SimBrokerGateway(CostModel())
    gw.execute("X", 1.0, 100.0)
    assert gw.query_position("X").qty == pytest.approx(1.0)
    acc = gw.query_account()
    assert isinstance(acc, Account)


def test_paper_gateway_translates_to_execute_plan():
    paper = PaperBroker(CostModel())
    gw = PaperBrokerGateway(paper)
    event = gw.execute("X", 1.0, 100.0)  # translate → execute_plan
    assert event is not None
    assert gw.query_position("X").qty == pytest.approx(1.0)
    acc = gw.query_account()
    assert isinstance(acc, Account)


# ---------------------------------------------------------------------------
# A5.6 CTPLiveGateway 守卫行为不变 + glue
# ---------------------------------------------------------------------------
def test_ctp_live_gateway_inherits_and_guards(monkeypatch):
    # 无 --i-understand-the-risk → 拒绝
    gw = CTPLiveGateway(cfg=object(), understand_risk=False)
    with pytest.raises(CTPGuardError, match="i-understand-the-risk"):
        gw.start()
    # 守卫齐全 → 通过（_connected=True，不建真实连接）
    monkeypatch.setenv("HEXBROKER_CTP_BROKER_ID", "b")
    monkeypatch.setenv("HEXBROKER_CTP_USER_ID", "u")
    monkeypatch.setenv("HEXBROKER_CTP_PASSWORD", "p")
    monkeypatch.setenv("HEXBROKER_CTP_APP_ID", "app")
    monkeypatch.setenv("HEXBROKER_CTP_AUTH_CODE", "code")
    gw2 = CTPLiveGateway(cfg=object(), understand_risk=True)
    gw2.start()
    assert gw2._connected is True
    # 骨架阶段拒绝真实下单
    with pytest.raises(CTPGuardError, match="真实下单"):
        gw2.submit_order("SHFE.cu", direction=1, qty=1)
    # 撤销亦拒绝
    with pytest.raises(CTPGuardError):
        gw2.cancel_order("any-id")


def test_vnpy_ctp_glue_raises_without_dep():
    # vnpy_ctp 未安装 → RuntimeError（业务授权项）
    with pytest.raises(RuntimeError):
        build_vnpy_ctp_gateway(cfg=object())
