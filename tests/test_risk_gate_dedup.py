"""P2-4 风控门禁去重修复单测（指纹持久化跨 tick + 交易日维度）。

背景：旧版 `risk_gate.py` 在非成本门禁 tick 会 ``pop`` 掉指纹，导致下一 tick 同条件
拒绝又重复打印（60s tick 刷屏，如 ag0 同信号 128 次）。修复后指纹持久化跨 tick，仅在
「拒绝条件变化（p_up/exp_ret/min_ratio 之一变）」或「跨交易日（day_iso 变）」时重打。

覆盖：
① 首拒打印；
② 连续同条件静默（不重打）；
③ 拒绝条件变化（p_up/exp_ret/min_ratio 之一变）重打；
④ 模拟跨日（day_iso 变）重打。

参考 test_scheduler_halt.RecordingLog 思路，直接捕获 RiskGate.evaluate 的 ``log.warning``。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from omegaconf import OmegaConf

from hexbroker.backtest.cost import CostModel
from hexbroker.paper import risk_gate as rg_mod
from hexbroker.paper.risk_gate import RiskGate
from hexbroker.paper.types import AccountSnapshot, PositionCtx, Quote, SignalFrame

_RISK = OmegaConf.create(
    {
        "risk": {
            "vol_target": 0.5,
            "kelly_cap": 1.0,
            "max_position_pct": 0.5,
            "recovery_drawdown_r1": 0.05,
            "recovery_drawdown_r2": 0.10,
            "recovery_drawdown_r3": 0.15,
            "position_scalar_r1": 0.5,
            "position_scalar_r2": 0.0,
            "position_scalar_r3": 0.2,
            "position_scalar_r4": 1.0,
            "vol_low_q": 0.2,
            "vol_high_q": 0.8,
        }
    }
)


class _RecLog:
    """捕获 RiskGate 模块级 ``log`` 的 warning 文本（去重断言用）。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        pass

    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        pass

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)

    def exception(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message.format(*args) if args else message)


class _FrozenDateTime(datetime):
    """可控 ``now()``：测试跨日指纹时替换模块级 ``datetime``。

    ``now()`` 返回 ``cls._frozen``（真实 datetime 实例），因此
    ``datetime.now().date().isoformat()`` 仍能拿到可控的交易日 ISO 字符串。
    """

    _frozen: datetime = datetime(2026, 8, 25, 10, 0, 0)

    @classmethod
    def now(cls, tz=None) -> datetime:  # type: ignore[override]
        return cls._frozen


def _cost() -> CostModel:
    return CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={"rb0": {"multiplier": 10.0, "min_tick": 1.0}},
    )


def _gate() -> RiskGate:
    return RiskGate(
        _RISK, default_intent=0.30, cost=_cost(), cost_gate_enabled=True, cost_gate_min_ratio=2.0
    )


def _acct() -> AccountSnapshot:
    return AccountSnapshot(
        ts=datetime(2026, 8, 25, 10, 0), equity=100_000.0, cash=100_000.0, margin_used=0.0,
        positions={}, avg_entry={}, realized={}, drawdown=0.0, peak_equity=100_000.0,
    )


def _pos() -> PositionCtx:
    return PositionCtx(symbol="rb0")


def _quote() -> Quote:
    return Quote(
        symbol="rb0", ts=datetime(2026, 8, 25, 10, 0), price=3000.0,
        open=3000.0, high=3001.0, low=2999.0, pre_settle=3000.0,
    )


def _sig(p_up: float = 0.7, exp_ret: float = 0.001) -> SignalFrame:
    """默认 ``exp_ret=0.001`` → 成本门禁必拒（净期望收益不覆盖成本）。"""
    return SignalFrame(
        symbol="rb0", ts=datetime(2026, 8, 25, 10, 0), p_up=p_up, exp_ret=exp_ret,
        is_effective=True, source="test", freshness_days=1,
    )


def _eval(rg: RiskGate, p_up: float = 0.7, exp_ret: float = 0.001) -> None:
    rg.evaluate(_sig(p_up=p_up, exp_ret=exp_ret), _quote(), _acct(), _pos())


# ---------------------------------------------------------------------------
# ① 首拒打印
# ---------------------------------------------------------------------------
def test_first_reject_prints(monkeypatch):
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    rg = _gate()
    _eval(rg)
    assert len(rec.warnings) == 1
    assert "成本门禁拦截开仓" in rec.warnings[0]


# ---------------------------------------------------------------------------
# ② 连续同条件静默（不重打）
# ---------------------------------------------------------------------------
def test_consecutive_same_condition_silent(monkeypatch):
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    rg = _gate()
    _eval(rg)  # 首拒：打印
    _eval(rg)  # 同条件：静默
    _eval(rg)  # 同条件：静默
    assert len(rec.warnings) == 1


# ---------------------------------------------------------------------------
# ③ 拒绝条件变化（p_up/exp_ret/min_ratio 之一变）重打
# ---------------------------------------------------------------------------
def test_condition_change_reprints(monkeypatch):
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    rg = _gate()
    _eval(rg, p_up=0.7, exp_ret=0.001)  # 首拒：打印
    _eval(rg, p_up=0.8, exp_ret=0.001)  # p_up 变 → 重打
    assert len(rec.warnings) == 2
    _eval(rg, p_up=0.8, exp_ret=0.002)  # exp_ret 变 → 再重打
    assert len(rec.warnings) == 3
    rg.set_cost(_cost(), cost_gate_min_ratio=3.0)  # min_ratio 变 → 再重打
    _eval(rg, p_up=0.8, exp_ret=0.002)
    assert len(rec.warnings) == 4


# ---------------------------------------------------------------------------
# ④ 模拟跨日（day_iso 变）重打
# ---------------------------------------------------------------------------
def test_cross_day_reprints(monkeypatch):
    rec = _RecLog()
    monkeypatch.setattr(rg_mod, "log", rec)
    monkeypatch.setattr(rg_mod, "datetime", _FrozenDateTime)
    rg = _gate()
    _FrozenDateTime._frozen = datetime(2026, 8, 25, 10, 0, 0)
    _eval(rg, p_up=0.7, exp_ret=0.001)  # day1 首拒：打印
    _FrozenDateTime._frozen = datetime(2026, 8, 25, 11, 0, 0)  # 同日稍后：静默
    _eval(rg, p_up=0.7, exp_ret=0.001)
    assert len(rec.warnings) == 1
    _FrozenDateTime._frozen = datetime(2026, 8, 26, 10, 0, 0)  # 跨日：重打
    _eval(rg, p_up=0.7, exp_ret=0.001)
    assert len(rec.warnings) == 2
