"""P0-1 开仓成本门禁单测（净期望收益 > 往返成本 × min_ratio 才开仓）。

覆盖：
① 预期收益覆盖成本 → 开仓；
② 不覆盖 → 拒开且 decision.reason 标注 cost_gate_reject；
③ exp_ret 与 p_up 方向矛盾（模型自相矛盾）→ 拒开；
④ 不传 cost → 门禁跳过（向后兼容，不破坏现有调用语义）；
⑤ cost_gate_enabled=False → 显式关闭门禁（即使注入 cost）；
⑥ R3 滑点纳入往返成本：仅费口径通过、含滑点口径被拒 → 拒开（slippage_in_cost=False 恢复放行）；
⑦ R3 含滑点仍通过 → 开仓。
"""

from __future__ import annotations

from datetime import datetime

from omegaconf import OmegaConf

from hexbroker.backtest.cost import CostModel
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

_TS = datetime(2026, 8, 24, 10, 0)


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


def _quote(price: float = 3000.0) -> Quote:
    return Quote(
        symbol="rb0", ts=_TS, price=price,
        open=price, high=price + 1, low=price - 1, pre_settle=price,
    )


def _acct() -> AccountSnapshot:
    return AccountSnapshot(
        ts=_TS, equity=100_000.0, cash=100_000.0, margin_used=0.0,
        positions={}, avg_entry={}, realized={}, drawdown=0.0, peak_equity=100_000.0,
    )


def _pos(position: float = 0.0, entry: float = 0.0) -> PositionCtx:
    return PositionCtx(
        symbol="rb0", position=position, entry_price=entry, atr=40.0,
        bars_in_position=1 if abs(position) > 1e-12 else 0,
        highest_since_entry=entry if entry else 3000.0,
        lowest_since_entry=entry if entry else 3000.0,
    )


def _sig(p_up: float = 0.7, exp_ret: float = 0.5) -> SignalFrame:
    return SignalFrame(
        symbol="rb0", ts=_TS, p_up=p_up, exp_ret=exp_ret,
        is_effective=True, source="test", freshness_days=1,
    )


def _gate(**kw) -> RiskGate:
    return RiskGate(_RISK, default_intent=0.30, **kw)


# ---------------------------------------------------------------------------
# ① 预期收益覆盖往返成本 → 开仓
# ---------------------------------------------------------------------------
def test_cost_gate_allows_when_expected_pnl_covers_cost() -> None:
    """price=3000, mult=10 → notional=30000；exp_ret=0.5% → expected_pnl=150；
    round_trip=30000×0.00015=4.5；150 > 4.5×2 → 放行开仓。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(_sig(), _quote(), _acct(), _pos())
    assert abs(d.target_position) > 1e-9, f"应开仓，实际 target={d.target_position}"
    assert d.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# ② 净期望收益不覆盖成本 → 拒开且 reason 标注
# ---------------------------------------------------------------------------
def test_cost_gate_rejects_when_expected_pnl_below_cost() -> None:
    """exp_ret=0.001% → expected_pnl=0.3；0.3 > 4.5×2=9 不成立 → 拒开。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(_sig(exp_ret=0.001), _quote(), _acct(), _pos())
    assert d.target_position == 0.0
    assert d.reason == "cost_gate_reject"


# ---------------------------------------------------------------------------
# ③ exp_ret 与 p_up 方向矛盾 → 拒开
# ---------------------------------------------------------------------------
def test_cost_gate_rejects_direction_contradiction() -> None:
    """p_up=0.7（多头意图）但 exp_ret=-0.5（预期下跌）→ 模型自相矛盾，拦截。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(_sig(p_up=0.7, exp_ret=-0.5), _quote(), _acct(), _pos())
    assert d.target_position == 0.0
    assert d.reason == "cost_gate_reject"

    # 反向：p_up=0.4（空头意图）但 exp_ret=+0.5（预期上涨）→ 同样拦截
    d2 = gate.evaluate(_sig(p_up=0.4, exp_ret=0.5), _quote(), _acct(), _pos())
    assert d2.target_position == 0.0
    assert d2.reason == "cost_gate_reject"


# ---------------------------------------------------------------------------
# ④ 不传 cost → 门禁跳过（向后兼容）
# ---------------------------------------------------------------------------
def test_cost_gate_skipped_without_cost() -> None:
    """现有调用不传 cost → 即使 exp_ret 极低也照常开仓，语义不变。"""
    gate = _gate()  # 无 cost → 门禁默认关闭
    d = gate.evaluate(_sig(exp_ret=0.001), _quote(), _acct(), _pos())
    assert abs(d.target_position) > 1e-9
    assert d.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# ⑤ cost_gate_enabled=False → 显式关闭
# ---------------------------------------------------------------------------
def test_cost_gate_disabled_by_flag() -> None:
    """注入 cost 但 cost_gate_enabled=False → 门禁不生效（运维可临时关停）。"""
    gate = _gate(cost=_cost(), cost_gate_enabled=False)
    d = gate.evaluate(_sig(exp_ret=0.001), _quote(), _acct(), _pos())
    assert abs(d.target_position) > 1e-9
    assert d.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# 已有持仓 → 门禁不拦截风控动作（平仓/止损照常）
# ---------------------------------------------------------------------------
def test_cost_gate_ignored_when_position_exists() -> None:
    """已有持仓时（非新开仓）不施加成本门禁，风控动作照常执行。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(
        _sig(exp_ret=0.001), _quote(), _acct(), _pos(position=1.0, entry=3000.0)
    )
    # 门禁不拦截（不置 0 / 不标 cost_gate_reject）；意图照常进入风控链
    assert d.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# ⑥ R3 滑点纳入往返成本：仅费口径通过、含滑点口径被拒
# ---------------------------------------------------------------------------
def test_cost_gate_rejects_when_slippage_makes_cost_too_high() -> None:
    """price=3000, mult=10, min_tick=1, slippage_ticks=1 → notional=30000；
    手续费往返 = 30000×0.00015 = 4.5；滑点往返 = 2×1×1×10 = 20.0；
    round_trip_cost = 24.5，门槛 = 24.5×2 = 49.0。
    exp_ret=0.06% → expected_pnl = 0.06/100×30000 = 18.0：
    18.0 > 4.5×2=9.0（纯费口径放行）但 18.0 > 49.0 不成立 → 含滑点口径拒开。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(_sig(exp_ret=0.06), _quote(), _acct(), _pos())
    assert d.target_position == 0.0
    assert d.reason == "cost_gate_reject"

    # 同一信号在 slippage_in_cost=False（纯费口径）下应放行
    gate_fee_only = _gate(cost=_cost(), slippage_in_cost=False)
    d2 = gate_fee_only.evaluate(_sig(exp_ret=0.06), _quote(), _acct(), _pos())
    assert abs(d2.target_position) > 1e-9
    assert d2.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# ⑦ R3 含滑点仍通过 → 开仓
# ---------------------------------------------------------------------------
def test_cost_gate_allows_when_expected_pnl_covers_cost_with_slippage() -> None:
    """exp_ret=0.2% → expected_pnl = 0.2/100×30000 = 60.0；
    60.0 > (4.5+20.0)×2 = 49.0 → 含滑点口径仍放行。"""
    gate = _gate(cost=_cost())
    d = gate.evaluate(_sig(exp_ret=0.2), _quote(), _acct(), _pos())
    assert abs(d.target_position) > 1e-9
    assert d.reason != "cost_gate_reject"


# ---------------------------------------------------------------------------
# ⑧ 技术兜底信号（is_effective=False）→ 不驱动新开仓（P1-1/P1-2 硬禁开）
# ---------------------------------------------------------------------------
def test_noneffective_signal_does_not_open() -> None:
    """技术兜底返回 ``is_effective=False``（降级方向提示，无模型 edge）→
    ``RiskGate._intent`` 返回 0 → 不触发成本门禁、``target_position=0``、不开仓。

    回归锁：审计发现旧版技术兜底把「上一日涨跌幅」当 exp_ret，
    在「昨日涨+弱多」时会绕过成本门禁误开仓（P1-1/P1-2）。
    """
    gate = _gate(cost=_cost())
    sig = SignalFrame(
        symbol="rb0", ts=_TS, p_up=0.65, exp_ret=0.0,
        is_effective=False, source="technical", freshness_days=0,
    )
    d = gate.evaluate(sig, _quote(), _acct(), _pos())
    assert d.target_position == 0.0
    assert d.reason != "cost_gate_reject"  # 由 _intent=0 阻断，而非成本门禁
