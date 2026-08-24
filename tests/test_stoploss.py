"""ATR 止损 ratchet / trailing_stop 回归测试（P5）。"""

from __future__ import annotations

import numpy as np

from hexbroker.risk.manager import RiskManager
from hexbroker.risk.stoploss import ATRRatchet, compute_stop, trailing_stop
from hexbroker.risk.types import ATRTier, RiskState


def test_ratchet_never_narrows():
    """P5 回归：ratchet 只减不增（止损距离只增不减），绝不向更窄方向收窄。"""
    # 从最宽 HIGH 起：波动走低（本可收窄到 LOW）也必须保持 HIGH
    r = ATRRatchet(ATRTier.HIGH)
    assert r.update(0.1) == ATRTier.HIGH
    # 从最窄 LOW 起：波动走高必须能加宽到 HIGH
    r2 = ATRRatchet(ATRTier.LOW)
    assert r2.update(0.95) == ATRTier.HIGH
    # 从最窄 LOW 起：波动走低保持 LOW（已是最窄，不收窄）
    r3 = ATRRatchet(ATRTier.LOW)
    assert r3.update(0.1) == ATRTier.LOW
    # 中间 MID：波动走高加宽到 HIGH；波动走低保持 MID（不收窄到 LOW）
    r4 = ATRRatchet(ATRTier.MID)
    assert r4.update(0.95) == ATRTier.HIGH
    r5 = ATRRatchet(ATRTier.MID)
    assert r5.update(0.1) == ATRTier.MID


def test_trailing_stop_favorable_only():
    """trailing_stop：多头止损只上移、绝不放松。"""
    # 首根：prev=None → 返回 base
    s0 = trailing_stop(110.0, 1.0, 2.0, ATRTier.HIGH, None)
    assert abs(s0 - 105.0) < 1e-6  # 110*(1-2.5*2/110)
    # 价格上行 → base 更高 → 取 max → 上移（有利）
    s_up = trailing_stop(110.0, 1.0, 2.0, ATRTier.HIGH, 100.0)
    assert s_up > 100.0
    # 价格下行 → base 更低 → 取 max(prev, base) = prev → 不放松
    s_down = trailing_stop(90.0, 1.0, 2.0, ATRTier.HIGH, 100.0)
    assert s_down == 100.0


class _RiskCfg:
    vol_target = 0.20
    kelly_cap = 0.25
    max_position_pct = 0.30
    recovery_drawdown_r1 = 0.05
    recovery_drawdown_r2 = 0.10
    recovery_drawdown_r3 = 0.15
    position_scalar_r1 = 0.5
    position_scalar_r2 = 0.0
    position_scalar_r3 = 0.2
    position_scalar_r4 = 1.0
    vol_low_q = 0.2
    vol_high_q = 0.8


class _Cfg:
    risk = _RiskCfg()


def test_manager_wires_trailing_stop():
    """P5 回归：manager 接入 trailing_stop，多头止损价只向有利方向移动。"""
    rm = RiskManager(_Cfg())
    s1 = RiskState(position=0.5, entry_price=100.0, current_price=100.0,
                   atr=2.0, atr_tier=ATRTier.HIGH, drawdown=0.0, vol_quantile=0.5)
    d1 = rm.evaluate(s1, intent_position=0.5, p_up=0.6)
    assert d1.stop_price is not None

    # 价格上行、atr 变小 → base 上移 → trailing 上移（有利）
    s2 = RiskState(position=0.5, entry_price=100.0, current_price=110.0,
                   atr=1.0, atr_tier=ATRTier.HIGH, drawdown=0.0, vol_quantile=0.5)
    d2 = rm.evaluate(s2, intent_position=0.5, p_up=0.6)
    assert d2.stop_price is not None
    assert d2.stop_price >= d1.stop_price

    # 价格下行、atr 变大 → base 下移 → trailing 不得放松（保持前值）
    s3 = RiskState(position=0.5, entry_price=100.0, current_price=90.0,
                   atr=3.0, atr_tier=ATRTier.HIGH, drawdown=0.0, vol_quantile=0.5)
    d3 = rm.evaluate(s3, intent_position=0.5, p_up=0.6)
    assert d3.stop_price >= d2.stop_price


def test_manager_s1_fires_with_context():
    """P6 回归（风控层）：提供行情上下文后 S1 趋势破坏应触发并清仓。"""
    rm = RiskManager(_Cfg())
    state = RiskState(
        position=0.5, entry_price=100.0, current_price=90.0, atr=2.0,
        atr_tier=ATRTier.HIGH, drawdown=0.0, vol_quantile=0.5, pnl_pct=0.0,
        bars_in_position=2,  # P0：S1 开仓缓冲（<2 根不判趋势破坏）
    )
    recent_returns = np.array([-0.01, -0.02, -0.03, -0.01, -0.02])
    recent_volumes = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    ma_price = 100.0  # 当前价 90 < 100 → 多头趋势破坏
    d = rm.evaluate(
        state, intent_position=0.5, p_up=0.6,
        recent_returns=recent_returns, recent_volumes=recent_volumes, ma_price=ma_price,
    )
    assert any(s.name == "S1_TREND_BREAK" for s in d.sell_signals)
    assert d.target_position == 0.0  # 卖出信号触发清仓
