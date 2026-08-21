"""P18-P0 回归测试：SimBroker 实现/未实现盈亏按品种级 multiplier 记账。

修复背景（P17 一致性验证量化发现，QA 三重证据确认）：
  - ``SimBroker.unrealized()`` / ``execute()`` 平仓盈亏此前使用全局
    ``self.cost.multiplier``（默认 10.0），未按品种级合约乘数记账；
  - 而 ``CostModel.fee()`` 已按品种级乘数 → 口径不一致；
  - 后果：ni0（×1）PnL 被放大 10 倍（P17 实测 ni0 真实 +84,469 被记成 +859,656），
    jm0（×60）被缩小 0.167 倍（亏损 -33,412 被记成 -5,982）。

修复（P18-P0，QA 指定最小方案）：两处 ``self.cost.multiplier`` →
``self.cost._multiplier(symbol)``（CostModel 私有方法，symbol 可空、无配置回退全局）。

本文件用例覆盖：
  1. ni0（×1）买卖平仓：已实现 PnL 按 ×1（≈99.895），非 ×10（999.895）—— QA 最小复现；
  2. jm0（×60）买卖平仓：已实现 PnL 按 ×60（≈5993.7），非 ×10（993.7）；
  3. ni0 持仓未实现：mark-to-market 按 ×1（≈100），非 ×10（1000）；
  4. 无 contracts 配置回退全局 multiplier=10（向后兼容，行为不变）。
"""
from __future__ import annotations

import pytest

from hexbroker.backtest.broker import SimBroker
from hexbroker.backtest.cost import CostModel


def _cost_with_contracts() -> CostModel:
    """滑点 0、手续费 0.005%、品种级合约参数（ni0=×1/10、jm0=×60/0.5），全局默认 ×10。"""
    return CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=0.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={
            "ni0": {"multiplier": 1.0, "min_tick": 10.0},
            "jm0": {"multiplier": 60.0, "min_tick": 0.5},
        },
    )


def _broker(cost: CostModel) -> SimBroker:
    return SimBroker(cost, initial_capital=1_000_000.0)


# ---------------------------------------------------------------------------
# 1. 已实现盈亏：ni0（×1）—— QA 最小复现（买1手@1000→卖@1100，真实 ≈ +99.89）
# ---------------------------------------------------------------------------
def test_realized_pnl_uses_per_symbol_multiplier_ni0():
    broker = _broker(_cost_with_contracts())
    broker.execute("ni0", 1, 1000.0, timestamp="2026-01-02")
    broker.execute("ni0", 0, 1100.0, timestamp="2026-01-03")

    # 真实：1 手 × (1100-1000) × 1 - 手续费(0.05 + 0.055) = 99.895
    assert broker.realized["ni0"] == pytest.approx(99.895, abs=1e-6)
    # 修复前（全局 ×10）会记成 999.895 —— 显式排除回归
    assert broker.realized["ni0"] != pytest.approx(999.895, abs=1e-6)
    # 持仓已清空、均价归零
    assert broker.position("ni0") == 0.0
    assert broker.avg_entry["ni0"] == 0.0


# ---------------------------------------------------------------------------
# 2. 已实现盈亏：jm0（×60）
# ---------------------------------------------------------------------------
def test_realized_pnl_uses_per_symbol_multiplier_jm0():
    broker = _broker(_cost_with_contracts())
    broker.execute("jm0", 1, 1000.0, timestamp="2026-01-02")
    broker.execute("jm0", 0, 1100.0, timestamp="2026-01-03")

    # 真实：1 手 × (1100-1000) × 60 - 手续费(3.0 + 3.3) = 5993.7
    assert broker.realized["jm0"] == pytest.approx(5993.7, abs=1e-6)
    # 修复前（全局 ×10）会记成 993.7 —— 显式排除回归
    assert broker.realized["jm0"] != pytest.approx(993.7, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. 未实现盈亏（mark-to-market）：ni0（×1）
# ---------------------------------------------------------------------------
def test_unrealized_pnl_uses_per_symbol_multiplier_ni0():
    broker = _broker(_cost_with_contracts())
    broker.execute("ni0", 1, 1000.0, timestamp="2026-01-02")

    marks = {"ni0": 1100.0}
    # 未实现 = 1 手 × (1100-1000) × 1 = 100；权益 = 1e6 - 开仓费0.05 + 100 = 1,000,099.95
    assert broker.unrealized(marks) == pytest.approx(100.0, abs=1e-6)
    assert broker.equity(marks) == pytest.approx(1_000_099.95, abs=1e-6)
    # 修复前（全局 ×10）未实现会是 1000、权益 1,000,999.95 —— 显式排除回归
    assert broker.unrealized(marks) != pytest.approx(1000.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. 无 contracts 配置 → 回退全局 multiplier=10（向后兼容，行为不变）
# ---------------------------------------------------------------------------
def test_fallback_global_multiplier_when_no_contracts():
    cost = CostModel(slippage_ticks=0.0, multiplier=10.0, min_tick=10.0)  # 无 contracts
    broker = _broker(cost)
    broker.execute("xx0", 1, 1000.0, timestamp="2026-01-02")
    broker.execute("xx0", 0, 1100.0, timestamp="2026-01-03")

    # 未配置品种：1 手 × (1100-1000) × 10 - 手续费(0.5 + 0.55) = 998.95（沿用全局 ×10）
    assert broker.realized["xx0"] == pytest.approx(998.95, abs=1e-6)
