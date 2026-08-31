"""RiskManager 'default' 哨兵可观测告警（④加固）单测。

验证：收到 symbol 缺失的 RiskState 时，回落 'default' 共享桶并**告警一次**
（不 fail-fast，避免误伤单测中对 manager 的 symbol-less 隔离测试），且返回正常决策、无异常。
正常生产流 state.symbol 恒非空（来自 self._symbols），此路径仅当上游 bug 才触发。
"""
from hexbroker.config import load_config
from hexbroker.risk.manager import RiskManager
from hexbroker.risk.types import RiskState


def _state(**kw) -> RiskState:
    base = dict(
        symbol="", equity=1_000_000.0, peak_equity=1_000_000.0, position=0.0,
        entry_price=100.0, current_price=100.0, atr=2.0, realized_vol=0.02,
        bars_in_position=1, highest_since_entry=100.0, lowest_since_entry=99.0,
        pnl_pct=0.0, drawdown=0.0, vol_quantile=0.5,
    )
    base.update(kw)
    return RiskState(**base)


def test_default_sentinel_warns_once() -> None:
    mgr = RiskManager(load_config())
    assert getattr(mgr, "_warned_default_symbol", False) is False
    # 第一次：symbol 缺失 → 触发告警 + 置位标记，返回正常（不抛）
    d = mgr.evaluate(_state(symbol=""), intent_position=0.0, p_up=0.5)
    assert d is not None
    assert mgr._warned_default_symbol is True
    # 第二次：不再重复告警（标记保持），行为仍正常
    mgr.evaluate(_state(symbol=""), intent_position=0.0, p_up=0.5)
    assert mgr._warned_default_symbol is True
