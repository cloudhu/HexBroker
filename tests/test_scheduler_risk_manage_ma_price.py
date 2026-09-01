"""scheduler._risk_manage_only 显式透传 ma_price（③加固）单测。

fake-object 探针：用假 scheduler 捕获 ``self._risk_gate.evaluate`` 收到的 ``ma_price``，
验证「仅风控」模式与 ``_process_symbol`` 一样把 ma_price 透传到风控链
（否则 RiskState.ma_price=None → sell_engine S1 趋势破坏止损在仅风控模式下被整体跳过）。
"""
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from hexbroker.paper.scheduler import TradingScheduler as Scheduler


def _make_recorder() -> SimpleNamespace:
    obj: SimpleNamespace = SimpleNamespace()
    obj._default_atr_pct = 0.02
    obj._captured: dict = {}

    def evaluate(sig, quote, acct, pos_ctx, recent_returns=None, recent_volumes=None, ma_price=None):
        obj._captured["ma_price"] = ma_price
        obj._captured["pos_ctx"] = pos_ctx
        return SimpleNamespace(target_position=0.0, liquidate=False, reason="")

    obj._day_signals = {}
    obj._risk_gate = SimpleNamespace(evaluate=evaluate)
    obj._signals = SimpleNamespace(neutral_signal=lambda s, n: SimpleNamespace(symbol=s))
    obj._broker = SimpleNamespace(
        snapshot=lambda m: SimpleNamespace(equity=1_000_000.0, peak_equity=1_000_000.0, drawdown=0.0),
        position_ctx=lambda s, q, a: SimpleNamespace(
            symbol=s, position=0.0, entry_price=0.0, atr=a, bars_in_position=1,
        ),
        execute_plan=lambda *a, **k: None,
    )
    obj._planner = SimpleNamespace(update_from_signal=lambda *a, **k: SimpleNamespace())
    obj._record_trade = lambda *a, **k: None
    obj._cached_bars = lambda s, d: _bars(30)
    obj._aux_from_bars = Scheduler._aux_from_bars.__get__(obj)
    # P1-1：_risk_manage_only 新增「决策 trace」协作者。假 self 绑定**真实实现**
    # （而非 stub 掉），顺带覆盖该路径下 emitter 对 fake 输入的健壮性。
    obj._trace_fp = {}
    obj._halt = False
    # P0-1：trace 会附带冷却观测字段，假 self 需提供空冷却表（否则 emitter 取不到属性）
    obj._last_sig_fp = {}
    obj._emit_decision_trace = Scheduler._emit_decision_trace.__get__(obj)
    return obj


def _bars(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    closes = list(range(100, 100 + n))
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "volume": [10] * n,
        },
        index=idx,
    )


def test_risk_manage_only_forwards_ma_price() -> None:
    obj = _make_recorder()
    rec = Scheduler._risk_manage_only.__get__(obj)
    quote = SimpleNamespace(price=130.0, high=131.0, low=129.0, symbol="rb0")
    rec("rb0", quote, {"rb0": 130.0}, date(2026, 8, 31), datetime(2026, 8, 31, 21, 0))
    # 30 日收盘均线 = mean(110..129) = 119.5（最后 20 根），非 None → S1 趋势破坏止损可生效
    assert obj._captured["ma_price"] is not None
    assert abs(obj._captured["ma_price"] - 119.5) < 1e-6
