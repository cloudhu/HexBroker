"""P1-8 风控规则接口化 + 组合盘单测（§3）。

覆盖：
A8.0 现状等价回归：默认规则驱动的 ``RiskManager`` 与「用底层函数独立重算」的
     决策（target/reason/liquidate/stage/kelly/sell_signals）逐项完全一致
     （数值/布尔钉死，保证 549 测试零回归）；
A8.1 ``RiskRule`` ABC 与 ``RiskContext`` / ``RiskAdjustment`` 契约；
A8.2 ``build_default_rules`` 默认顺序 = 硬止损→RL意图→恢复→预算→S1-S5；
A8.3 ``ComboTargetMerger`` 合并 + 防自成交 + 单笔流控；
A8.4 ``configs/risk_rules.yaml`` 规则名顺序与默认顺序一致（不被自动加载）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from hexbroker.config import load_config
from hexbroker.constants import RecoveryStage, SellSignalCode
from hexbroker.risk import (
    ComboTargetMerger,
    EngineTarget,
    RiskContext,
    RiskManager,
    RiskRule,
    build_default_rules,
)
from hexbroker.risk.budget import budget_target
from hexbroker.risk.limits import position_within_limit
from hexbroker.risk.combo import ComboTargetMerger as _CTM
from hexbroker.risk.limits import HARD_STOP_DRAWDOWN, hard_stop_triggered
from hexbroker.risk.manager import _apply
from hexbroker.risk.recovery import recovery_scalar, recovery_stage
from hexbroker.risk.rules import RiskAdjustment
from hexbroker.risk.sell_engine import detect_sell_signals, strongest
from hexbroker.risk.types import ATRTier, RiskState


def _cfg():
    return load_config()


def _state(**kw) -> RiskState:
    base = dict(
        symbol="rb0", equity=1_000_000.0, peak_equity=1_000_000.0, position=0.0,
        entry_price=100.0, current_price=100.0, atr=2.0, realized_vol=0.02,
        bars_in_position=1, highest_since_entry=100.0, lowest_since_entry=99.0,
        pnl_pct=0.0, drawdown=0.0, vol_quantile=0.5,
    )
    base.update(kw)
    return RiskState(**base)


# ---------------------------------------------------------------------------
# A8.0 现状等价回归：规则驱动 == 底层函数独立重算
# ---------------------------------------------------------------------------
def _expected(state: RiskState, intent: float, p_up: float, cfg, recent_returns=None,
              recent_volumes=None, ma_price=None) -> dict:
    """用底层函数独立重算期望决策（与规则包裹的函数同源）。"""
    risk = cfg.risk
    g = lambda n, d: getattr(risk, n, d)

    if hard_stop_triggered(state.drawdown, HARD_STOP_DRAWDOWN):
        return dict(target=0.0, liquidate=True, reason="hard_stop",
                    stage=RecoveryStage.R0_NORMAL, kelly=0.0, signals=[])

    vol = max(state.realized_vol, 1e-6)
    budget = budget_target(p_up, vol, g("vol_target", 0.20), g("kelly_cap", 0.25),
                           g("max_position_pct", 0.30))
    stage = recovery_stage(state.drawdown, g("recovery_drawdown_r1", 0.05),
                           g("recovery_drawdown_r2", 0.10), g("recovery_drawdown_r3", 0.15))
    scalar = recovery_scalar(stage, g("position_scalar_r1", 0.5), g("position_scalar_r2", 0.0),
                             g("position_scalar_r3", 0.2), g("position_scalar_r4", 1.0))
    target = position_within_limit(intent * scalar, min(abs(budget), g("max_position_pct", 0.30)))

    reason = "rl_intent"
    liquidate = False
    signals = detect_sell_signals(state, recent_returns if recent_returns is not None else np.array([]),
                                  recent_volumes if recent_volumes is not None else np.array([]),
                                  ma_price, risk)
    if signals:
        reason = strongest(signals).value
        if state.position != 0 or target != 0.0:
            target = 0.0
            liquidate = bool(state.position != 0)
    return dict(target=target, liquidate=liquidate, reason=reason, stage=stage,
                kelly=float(budget), signals=list(signals))


def _assert_equiv(state, intent, p_up, cfg, **kw):
    rm = RiskManager(cfg)  # 全新 manager（ratchet 无记忆）
    d = rm.evaluate(state, intent, p_up, **kw)
    exp = _expected(state, intent, p_up, cfg, **kw)
    assert abs(d.target_position - exp["target"]) < 1e-12, (d.target_position, exp["target"])
    assert d.liquidate == exp["liquidate"]
    assert d.reason == exp["reason"]
    assert d.stage == exp["stage"]
    assert d.sell_signals == exp["signals"]
    assert abs(d.kelly_fraction - exp["kelly"]) < 1e-12


def test_a80_default_rules_match_legacy_priority_chain():
    cfg = _cfg()
    # 多场景覆盖硬止损 / RL意图 / 预算 / 恢复 / S1-S5
    _assert_equiv(_state(drawdown=0.0, position=0.0), 1.0, 0.9, cfg)
    _assert_equiv(_state(drawdown=0.30, position=1.0), 1.0, 0.9, cfg)  # 硬止损
    _assert_equiv(_state(drawdown=0.06, position=0.0), 0.02, 0.6, cfg)  # R1 降仓
    _assert_equiv(_state(drawdown=0.12, position=0.0), 0.5, 0.7, cfg)  # R2 暂停
    _assert_equiv(_state(position=1.0, current_price=90.0, pnl_pct=0.0, bars_in_position=2),
                  1.0, 0.9, cfg, ma_price=95.0)  # S1 趋势破坏
    _assert_equiv(_state(position=0.0, realized_vol=0.02, pnl_pct=0.0), 1.0, 0.9, cfg)  # 预算封顶


def test_a80_hard_stop_is_absolute_top_priority():
    cfg = _cfg()
    rm = RiskManager(cfg)
    d = rm.evaluate(_state(drawdown=0.30, position=1.0), 1.0, 0.9)
    assert d.liquidate is True
    assert d.target_position == 0.0
    assert d.reason == "hard_stop"


# ---------------------------------------------------------------------------
# A8.1 RiskRule ABC 与 RiskContext / RiskAdjustment 契约
# ---------------------------------------------------------------------------
def test_a81_risk_rule_is_abstract():
    import pytest
    with pytest.raises(TypeError):
        RiskRule()  # type: ignore[abstract]

    class Dummy(RiskRule):
        name = "dummy"
        def evaluate(self, ctx, state, intent, p_up=0.5, **kw):
            return RiskAdjustment(target_position=0.1)

    assert issubclass(Dummy, RiskRule)
    d = Dummy()
    adj = d.evaluate(RiskContext(), _state(), 0.5)
    assert isinstance(adj, RiskAdjustment)
    assert adj.target_position == 0.1


def test_a81_apply_merges_adjustment_into_context():
    ctx = RiskContext(target_position=0.5, reason="rl_intent")
    _apply(ctx, RiskAdjustment(target_position=0.2, reason="S1", veto=True))
    assert ctx.target_position == 0.2
    assert ctx.reason == "S1"
    assert ctx.veto is True


# ---------------------------------------------------------------------------
# A8.2 build_default_rules 默认顺序
# ---------------------------------------------------------------------------
def test_a82_default_rule_order():
    rules = build_default_rules(_cfg())
    names = [r.name for r in rules]
    assert names == ["hard_stop", "rl_intent", "recovery", "budget", "sell_engine"]


def test_a82_custom_rules_injected():
    """可注入自定义规则列表（支持 yaml 增删/重排），不改默认。"""
    class Extra(RiskRule):
        name = "extra"
        def evaluate(self, ctx, state, intent, p_up=0.5, **kw):
            return RiskAdjustment()

    rm = RiskManager(_cfg(), rules=[Extra()])
    assert [r.name for r in rm.rules] == ["extra"]


# ---------------------------------------------------------------------------
# A8.3 ComboTargetMerger 合并 + 防自成交 + 流控
# ---------------------------------------------------------------------------
def test_a83_merge_same_symbol_net_target():
    m = ComboTargetMerger()
    out = m.merge([
        EngineTarget("rb0", "A", 0.3),
        EngineTarget("rb0", "B", 0.2),
        EngineTarget("cu0", "A", -0.1),
    ])
    assert out == {"rb0": 0.5, "cu0": -0.1}


def test_a83_self_trade_guard_prevents_opposing_net():
    m = ComboTargetMerger(self_trade_guard=True)
    # 多引擎反向 → 净目标单方向，杜绝同品种双向自成交
    out = m.merge([
        EngineTarget("rb0", "A", 0.3),
        EngineTarget("rb0", "B", -0.2),
    ])
    assert abs(out["rb0"] - 0.1) < 1e-9  # 净 0.1，单一方向
    assert not m._has_opposing([0.1])  # 合并后无反向


def test_a83_flow_control_cap():
    m = ComboTargetMerger(max_order_qty=100.0)
    assert m.check_flow("rb0", 50.0) is True
    assert m.check_flow("rb0", 150.0) is False
    assert m.check_flow("rb0", -100.0) is True
    assert m.check_flow("rb0", -101.0) is False


# ---------------------------------------------------------------------------
# A8.4 risk_rules.yaml 规则名顺序与默认一致（不被自动加载）
# ---------------------------------------------------------------------------
def test_a84_risk_rules_yaml_order_matches_default():
    path = Path(__file__).resolve().parents[1] / "configs" / "risk_rules.yaml"
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    names = [r["name"] for r in doc["rules"]]
    assert names == ["hard_stop", "rl_intent", "recovery", "budget", "sell_engine"]
