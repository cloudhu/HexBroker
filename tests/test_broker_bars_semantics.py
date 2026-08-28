"""③语义债锁定测试：PaperBroker.position_ctx 的 bars_in_position = 自然日天数（非 bar 数）。

背景：sell_engine（S1 开仓缓冲/S4 时间止损）以 bar 数语义消费此字段，而 paper 实盘注入
自然日天数（RL/回测侧为真实 bar 数）。三端口径差异经 24-bars-semantics-assessment.md
评估裁决为"维持现状+语义锁定"。本测试防止未来在无 walk-forward QA 的前提下无意改写。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from hexbroker.backtest.cost import CostModel
from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.types import Quote


def _pb() -> PaperBroker:
    return PaperBroker(
        CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                  slippage_ticks=1.0, margin_rate=0.12, multiplier=10.0, min_tick=1.0),
        initial_capital=100_000.0,
        data_dir="data/paper_test_bars_semantics",
    )


def _quote(price: float = 3000.0) -> Quote:
    return Quote(symbol="rb0", ts=datetime(2026, 8, 28, 10, 0), price=price,
                 open=price, high=price, low=price, pre_settle=price)


def test_bars_same_day_open_is_one():
    """当日开仓 → days=0 → max(1, 0)=1（自然日语义锁定）。"""
    pb = _pb()
    pb._broker.open_dates["rb0"] = datetime.now().date()
    pb._broker.positions["rb0"] = 1
    ctx = pb.position_ctx("rb0", _quote())
    assert ctx.bars_in_position == 1


def test_bars_is_natural_calendar_days_including_weekend():
    """3 自然日前开仓 → bars=3（含周末，非交易日数）。语义锁定：若有人改为
    交易日/session 感知，本测试失败即触发 walk-forward QA 评审流程。"""
    pb = _pb()
    pb._broker.open_dates["rb0"] = date.today() - timedelta(days=3)
    pb._broker.positions["rb0"] = 1
    ctx = pb.position_ctx("rb0", _quote())
    assert ctx.bars_in_position == 3


def test_bars_defaults_to_one_without_open_date():
    """无 open_date（防御）→ bars=1。"""
    pb = _pb()
    pb._broker.positions["rb0"] = 1
    ctx = pb.position_ctx("rb0", _quote())
    assert ctx.bars_in_position == 1


def test_flat_position_ctx_bars_zero():
    """无持仓 → bars_in_position 保持 dataclass 默认 0。"""
    pb = _pb()
    ctx = pb.position_ctx("rb0", _quote())
    assert ctx.position == 0.0 and ctx.bars_in_position == 0
