"""T03 风控优先级链测试（§3.4）：硬止损 > S1–S5 > 预算 > R1–R4 恢复 > RL 意图。"""

from __future__ import annotations


from hexbroker.config import load_config
from hexbroker.constants import RecoveryStage, SellSignalCode
from hexbroker.risk.manager import RiskManager
from hexbroker.risk.stoploss import ATRRatchet
from hexbroker.risk.types import ATRTier, RiskState


def _cfg():
    return load_config()


def _state(**kw) -> RiskState:
    base = dict(
        equity=1_000_000.0, peak_equity=1_000_000.0, position=0.0, entry_price=100.0,
        current_price=100.0, atr=2.0, realized_vol=0.02, bars_in_position=1,
        highest_since_entry=100.0, lowest_since_entry=99.0, pnl_pct=0.0, drawdown=0.0,
        vol_quantile=0.5,
    )
    base.update(kw)
    return RiskState(**base)


def test_hard_stop_is_absolute_top_priority():
    rm = RiskManager(_cfg(), hard_stop=0.20)
    state = _state(drawdown=0.30, position=1.0)
    d = rm.evaluate(state, intent_position=1.0, p_up=0.9)
    assert d.liquidate is True
    assert d.target_position == 0.0
    assert d.reason == "hard_stop"


def test_s1_trend_break_beats_rl_intent_and_budget():
    rm = RiskManager(_cfg())
    state = _state(position=1.0, current_price=90.0, pnl_pct=0.0)
    d = rm.evaluate(state, intent_position=1.0, p_up=0.9, ma_price=95.0)
    assert d.liquidate is True
    assert d.target_position == 0.0
    assert SellSignalCode.S1_TREND_BREAK in d.sell_signals
    assert d.reason == "S1"


def test_budget_caps_rl_intent():
    rm = RiskManager(_cfg())
    state = _state(position=0.0, realized_vol=0.02, pnl_pct=0.0)
    d = rm.evaluate(state, intent_position=1.0, p_up=0.9)
    assert abs(d.target_position) <= 0.30 + 1e-9
    assert d.reason == "rl_intent"  # 无信号时意图放行（但受预算封顶）


def test_recovery_r1_reduces_position():
    rm = RiskManager(_cfg())
    # R1: 回撤 ≥5% → 仓位缩放 0.5
    # 注意：P4 修复后预算(budget≈0.05)为生效上限，与回撤档位无关；
    # 为使恢复缩放可见，intent 取低于预算上限的值，避免被预算封顶掩盖差异。
    state = _state(position=0.0, drawdown=0.06, peak_equity=1_000_000.0, equity=940_000.0)
    d = rm.evaluate(state, intent_position=0.02, p_up=0.6)
    assert d.stage == RecoveryStage.R1_REDUCE
    assert d.target_position <= 0.02 * 0.5 + 1e-9
    # 对照组：无回撤 → 意图完整放行
    state0 = _state(position=0.0, drawdown=0.0, pnl_pct=0.0)
    d0 = rm.evaluate(state0, intent_position=0.02, p_up=0.6)
    assert d0.stage == RecoveryStage.R0_NORMAL
    assert d0.target_position > d.target_position


def test_recovery_r2_halt_blocks_open():
    rm = RiskManager(_cfg())
    state = _state(position=0.0, drawdown=0.12, peak_equity=1_000_000.0, equity=880_000.0)
    d = rm.evaluate(state, intent_position=0.5, p_up=0.7)
    assert d.stage == RecoveryStage.R2_HALT
    assert d.target_position == 0.0  # R2 暂停开仓


def test_budget_is_effective_cap():
    """P4 修复：预算(budget)须以 min(budget, max_position_pct) 生效，而非恒被 0.30 覆盖。

    将 max_position_pct 压到 0.05（< 预算≈0.25），RL 意图拉满时生效上限应为 0.05，
    而非旧实现的 max(budget,0.30)=0.30。
    """
    cfg = _cfg()
    cfg.risk.max_position_pct = 0.05
    rm = RiskManager(cfg)
    state = _state(position=0.0, realized_vol=0.02, pnl_pct=0.0)
    d = rm.evaluate(state, intent_position=1.0, p_up=0.9)
    assert abs(d.target_position) <= 0.05 + 1e-9
    assert abs(d.target_position) < 0.20  # 显著小于旧实现的 0.30 上限


def test_atr_ratchet_only_widens():
    """ATR ratchet：止损距离「只增不减」（档位索引只减不增，HIGH=0 最宽 / LOW=2 最窄）。

    规格（交易系统 v4.0 / 审计红线）：波动放大（高分位）收紧到最宽 HIGH(0)；
    波动回落（低分位）本应收窄到最窄 LOW(2)，但 ratchet **禁止收窄**，
    保持当前或更宽档位，确保持仓期间止损距离永不缩小。
    旧实现 ``if int(new) >= int(self.tier)`` 方向反了（会锁定最窄距离），已修正。
    """
    # 起点最窄 LOW(2)：高波动应放宽到 HIGH(0)（索引减小 → 更宽 → 允许）
    r = ATRRatchet(ATRTier.LOW)
    r.update(0.9)
    assert r.tier == ATRTier.HIGH
    # 此后波动回落想收窄，ratchet 禁止收窄 → 维持最宽 HIGH(0)
    r.update(0.1)
    assert r.tier == ATRTier.HIGH
    r.update(0.5)  # MID(1) 索引 1 > 0 → 维持 HIGH
    assert r.tier == ATRTier.HIGH

    # 反向路径：起点 LOW，低波动无法再收窄（已是最窄），高波动可放宽
    r2 = ATRRatchet(ATRTier.LOW)
    r2.update(0.1)  # LOW 已是最窄，维持
    assert r2.tier == ATRTier.LOW
    r2.update(0.5)  # MID(1) 索引 1 < 2 → 更宽 → 允许
    assert r2.tier == ATRTier.MID


def test_atr_stop_multiplier_tiers():
    # 底仓 2.5 / 配置 2.0 / 机动 1.5（与用户交易系统 v4.0 一致）
    assert ATRTier.HIGH.multiplier == 2.5
    assert ATRTier.MID.multiplier == 2.0
    assert ATRTier.LOW.multiplier == 1.5
