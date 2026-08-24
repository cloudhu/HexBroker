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
    # R3-1：rb/c 依据 paper.yaml 显式声明（multiplier=10，与全局默认一致）
    assert cm._multiplier("rb") == pytest.approx(10.0)
    assert cm._multiplier("rb0") == pytest.approx(10.0)
    assert cm._multiplier("c0") == pytest.approx(10.0)
    # 未声明品种回退全局默认
    assert cm._multiplier("cu0") == pytest.approx(10.0)


def test_spec_min_tick_fallback_when_contracts_none():
    cm = CostModel(contracts=None)
    assert cm._min_tick("au") == pytest.approx(0.02)
    assert cm._min_tick("ag") == pytest.approx(0.01)
    assert cm._min_tick("m") == pytest.approx(1.0)
    # R3-1：rb/c 依据 paper.yaml 显式声明（min_tick=1），不再回退全局 10.0（修复 10× 虚高）
    assert cm._min_tick("rb") == pytest.approx(1.0)
    assert cm._min_tick("rb0") == pytest.approx(1.0)
    assert cm._min_tick("c") == pytest.approx(1.0)
    assert cm._min_tick("c0") == pytest.approx(1.0)
    # 未声明品种仍回退全局默认（保守方向不破坏）
    assert cm._min_tick("cu0") == pytest.approx(10.0)


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


def test_r3_1_min_tick_fallback_rb_c():
    """R3-1：rb/c 兜底路径 min_tick=1（依据 paper.yaml 显式声明），修复滑点 10× 虚高。

    修复前：rb0/c0 未声明 → 回退全局 min_tick=10.0 → 单边滑点 2×10×1×10=¥200（应为 ¥20）。
    修复后：rb/c 走 _SPEC_MIN_TICK=1.0 → ¥20。
    """
    cm = CostModel()  # contracts=None，走兜底表路径
    assert cm._min_tick("rb0") == pytest.approx(1.0)
    assert cm._min_tick("c0") == pytest.approx(1.0)
    assert cm._multiplier("rb0") == pytest.approx(10.0)
    # 未覆盖品种仍回退全局默认（保守方向不破坏）
    assert cm._min_tick("cu0") == pytest.approx(10.0)
    # 兜底滑点成本：rb 1 手 1 tick = 1×1×10×1 = ¥10（单边），往返 ¥20
    fp, fee, slip, total = cm.trade_cost(3038.0, 1.0, is_open=True, symbol="rb0")
    assert fp == pytest.approx(3039.0)
    assert slip == pytest.approx(10.0)
    assert total == pytest.approx(fee + slip)


def test_r3_1_contracts_provided_take_priority():
    """R3-1：contracts 显式提供时优先于兜底表（口径不回归）。"""
    cm = CostModel(contracts={"rb0": {"min_tick": 2.0, "multiplier": 20.0}})
    assert cm._min_tick("rb0") == pytest.approx(2.0)
    assert cm._multiplier("rb0") == pytest.approx(20.0)
    # 同短名其他合约（rb1）未在 contracts 内 → 走兜底表
    assert cm._min_tick("rb1") == pytest.approx(1.0)
