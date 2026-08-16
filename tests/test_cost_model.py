"""T03 成本模型精确性测试（误差 < 1e-8）。"""

from __future__ import annotations

from hexbroker.backtest.cost import CostModel


def _model() -> CostModel:
    return CostModel(
        fee_open=1e-4,
        fee_close=1e-4,
        fee_close_today=2e-4,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
    )


def test_open_trade_cost_hand_calc():
    cm = _model()
    ref = 1000.0
    qty = 5.0
    fp, fee, slip, total = cm.trade_cost(ref, qty, is_open=True)
    # 成交价 = 1000 + 1*10 = 1010（买入方向 + 滑点不利）
    assert abs(fp - 1010.0) < 1e-8
    # 手续费 = 1010 * 10 * 5 * 1e-4 = 5.05
    assert abs(fee - 5.05) < 1e-8
    # 滑点成本 = 10 * 1 * 10 * 5 = 500
    assert abs(slip - 500.0) < 1e-8
    assert abs(total - 505.05) < 1e-8


def test_close_today_doubled_fee():
    cm = _model()
    ref = 2000.0
    qty = -3.0  # 卖出平仓
    _, fee_close, _, _ = cm.trade_cost(ref, qty, is_open=False, is_today_close=False)
    _, fee_today, _, _ = cm.trade_cost(ref, qty, is_open=False, is_today_close=True)
    # 平今费率 = 2e-4 = 2 倍平昨
    assert abs(fee_today - 2.0 * fee_close) < 1e-8


def test_sell_side_fill_price_slippage_down():
    cm = _model()
    ref = 5000.0
    fp, _, _, _ = cm.trade_cost(ref, -1.0, is_open=True)
    assert abs(fp - (5000.0 - 10.0)) < 1e-8  # 卖出：成交价低于参考价（滑点不利）


def test_fill_price_no_slippage_when_zero_ticks():
    cm = CostModel(slippage_ticks=0.0, min_tick=10.0, multiplier=10.0)
    fp, _, _, _ = cm.trade_cost(100.0, 1.0, is_open=True)
    assert abs(fp - 100.0) < 1e-8
