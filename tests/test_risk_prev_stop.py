"""RiskManager 止损记忆按品种隔离单测（P1 修复：_prev_stop/_ratchet 跨品种串扰）。"""

from omegaconf import OmegaConf

from hexbroker.risk.manager import RiskManager
from hexbroker.risk.types import ATRTier, RiskState

_CFG = OmegaConf.create({
    "risk": {
        "vol_target": 0.20,
        "kelly_cap": 0.25,
        "max_position_pct": 0.30,
        "recovery_drawdown_r1": 0.05,
        "recovery_drawdown_r2": 0.10,
        "recovery_drawdown_r3": 0.15,
        "position_scalar_r1": 0.5,
        "position_scalar_r2": 0.0,
        "position_scalar_r3": 0.2,
        "position_scalar_r4": 1.0,
        "vol_low_q": 0.2,
        "vol_high_q": 0.8,
        "sell_s1_min_bars": 2,
        "sell_s1_band_atr": 0.1,
    }
})


def _state(symbol: str, position: float, price: float, entry: float, atr: float) -> RiskState:
    return RiskState(
        symbol=symbol, position=position, entry_price=entry, current_price=price, atr=atr,
        realized_vol=atr / price if price > 0 else 0.02,
        bars_in_position=5, vol_quantile=0.5,
    )


def test_prev_stop_isolated_per_symbol() -> None:
    """ag0 与 rb0 交替 evaluate，止损价互不污染（修复前 _prev_stop 单值会串扰）。"""
    mgr = RiskManager(_CFG)
    # ag0 空头（价格 16896，ATR 300）
    d_ag = mgr.evaluate(_state("ag0", -1.0, 16896.0, 16895.99, 300.0), intent_position=-0.30, p_up=0.267)
    # rb0 多头（价格 3038，ATR 40）——若 _prev_stop 被 ag0 污染，rb0 止损会异常
    d_rb = mgr.evaluate(_state("rb0", 1.0, 3038.0, 3038.0, 40.0), intent_position=0.30, p_up=0.733)
    assert d_ag.stop_price is not None and d_rb.stop_price is not None
    # rb0 多头止损应显著低于其价格（3000 量级），而不是 ag0 的 17000+ 量级
    assert d_rb.stop_price < 3038.0 < d_ag.stop_price
    # ag0 止损 ≈ 16896 × (1+2.5×300/16896) ≈ 17446；rb0 止损 ≈ 3038 × (1-2.5×40/3038) ≈ 2938
    assert 17000 < d_ag.stop_price < 18000, f"ag0 stop={d_ag.stop_price}"
    assert 2900 < d_rb.stop_price < 3000, f"rb0 stop={d_rb.stop_price}"


def test_reset_ratchet_by_symbol() -> None:
    """reset_ratchet(symbol) 只重置该品种，不影响其他品种记忆。"""
    mgr = RiskManager(_CFG)
    mgr.evaluate(_state("ag0", -1.0, 16896.0, 16895.99, 300.0), intent_position=-0.30, p_up=0.267)
    mgr.evaluate(_state("rb0", 1.0, 3038.0, 3038.0, 40.0), intent_position=0.30, p_up=0.733)
    # 重置 rb0 → rb0 记忆清空，ag0 保留
    mgr.reset_ratchet(symbol="rb0")
    assert "rb0" not in mgr._prev_stops
    assert "ag0" in mgr._prev_stops
    # 无参调用（旧兼容）：全部重置
    mgr.reset_ratchet()
    assert mgr._prev_stops == {}
    assert mgr._ratchets == {}
