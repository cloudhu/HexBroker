"""V5 回归：contracts=None 时按品种规格回退乘数/最小跳动（au×1000/ag×15/m×10）。"""

from __future__ import annotations

import pytest

from hexbroker.backtest.cost import CostModel


def test_spec_multiplier_fallback_when_contracts_none():
    cm = CostModel(contracts=None)
    assert cm._multiplier("au") == pytest.approx(1000.0)
    assert cm._multiplier("au0") == pytest.approx(1000.0)
    assert cm._multiplier("ag") == pytest.approx(15.0)
    assert cm._multiplier("ag0") == pytest.approx(15.0)
    assert cm._multiplier("m") == pytest.approx(10.0)
    # 非上市品种回退全局默认
    assert cm._multiplier("rb") == pytest.approx(10.0)


def test_spec_min_tick_fallback_when_contracts_none():
    cm = CostModel(contracts=None)
    assert cm._min_tick("au") == pytest.approx(0.02)
    assert cm._min_tick("ag") == pytest.approx(0.01)
    assert cm._min_tick("m") == pytest.approx(1.0)
    assert cm._min_tick("rb") == pytest.approx(10.0)  # 默认


def test_explicit_contracts_override_spec_fallback():
    cm = CostModel(contracts={"au0": {"multiplier": 999.0, "min_tick": 5.0}})
    assert cm._multiplier("au0") == pytest.approx(999.0)
    assert cm._min_tick("au0") == pytest.approx(5.0)
    # 未配置品种仍走规格回退
    assert cm._multiplier("ag0") == pytest.approx(15.0)


def test_fee_uses_spec_multiplier_for_au():
    cm = CostModel(contracts=None, fee_open=0.00005)
    # au 1 手：手续费 = 100 × 1000 × 1 × 0.00005 = 5.0（旧实现为 0.05）
    fee = cm.fee(100.0, 1.0, is_open=True, symbol="au")
    assert fee == pytest.approx(5.0)


def test_fill_price_uses_spec_min_tick_for_au():
    cm = CostModel(contracts=None)
    # au 最小跳动 0.02，买入滑点 +0.02
    fp = cm.fill_price(500.0, +1, "au")
    assert fp == pytest.approx(500.02)
