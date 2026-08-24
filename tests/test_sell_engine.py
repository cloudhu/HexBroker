"""S1 趋势破坏 P0 修复单测（开仓缓冲 + MA 带宽死区）。"""

from types import SimpleNamespace

from hexbroker.risk.sell_engine import detect_sell_signals
from hexbroker.risk.types import RiskState
from hexbroker.constants import SellSignalCode


def _state(**kw) -> RiskState:
    base = dict(symbol="rb0", position=1.0, current_price=3036.0, entry_price=3038.0,
                atr=40.0, bars_in_position=1, pnl_pct=0.0)
    base.update(kw)
    return RiskState(**base)


def _cfg(min_bars: int = 2, band_atr: float = 0.1) -> SimpleNamespace:
    return SimpleNamespace(
        sell_s1_min_bars=min_bars,
        sell_s1_band_atr=band_atr,
        sell_s1_window=20,
        sell_s3_target=0.10,
        sell_s4_bars=20,
        sell_s5_z=3.0,
    )


def test_s1_min_bars_buffer() -> None:
    """开仓缓冲：bars_in_position < 2 时价格跌破 MA 也不触发 S1。"""
    # 多头持仓 1 根 bar、价格 3036 < MA 3037（无带宽）→ 修复前会触发；缓冲应拦截
    st = _state(bars_in_position=1)
    sig = detect_sell_signals(st, [], [], ma_price=3037.0, cfg=_cfg(min_bars=2, band_atr=0.0))
    assert SellSignalCode.S1_TREND_BREAK not in sig
    # 持仓满 2 根 bar → S1 允许触发
    st2 = _state(bars_in_position=2)
    sig2 = detect_sell_signals(st2, [], [], ma_price=3037.0, cfg=_cfg(min_bars=2, band_atr=0.0))
    assert SellSignalCode.S1_TREND_BREAK in sig2


def test_s1_band_deadzone() -> None:
    """MA 带宽死区：价格贴均线（|diff| < band）不触发；深跌破 band 才触发。"""
    # 3036 vs MA 3037：diff=1 < band=4（0.1×ATR40）→ 不触发（贴线抖动被过滤）
    st = _state(bars_in_position=2)
    sig = detect_sell_signals(st, [], [], ma_price=3037.0, cfg=_cfg(band_atr=0.1))
    assert SellSignalCode.S1_TREND_BREAK not in sig
    # 3030 vs MA 3037：diff=7 > band=4 → 触发（真趋势破坏）
    st2 = _state(bars_in_position=2, current_price=3030.0)
    sig2 = detect_sell_signals(st2, [], [], ma_price=3037.0, cfg=_cfg(band_atr=0.1))
    assert SellSignalCode.S1_TREND_BREAK in sig2


def test_s1_short_side_band() -> None:
    """空头对称：价格须升破 ma + band 才触发 S1。"""
    st = _state(position=-1.0, current_price=3037.5, bars_in_position=2)
    sig = detect_sell_signals(st, [], [], ma_price=3037.0, cfg=_cfg(band_atr=0.1))
    assert SellSignalCode.S1_TREND_BREAK not in sig  # 3037.5 - 3037 = 0.5 < 4
    st2 = _state(position=-1.0, current_price=3045.0, bars_in_position=2)
    sig2 = detect_sell_signals(st2, [], [], ma_price=3037.0, cfg=_cfg(band_atr=0.1))
    assert SellSignalCode.S1_TREND_BREAK in sig2  # 3045 - 3037 = 8 > 4


def test_s1_flat_position_no_signal() -> None:
    """无持仓 → 无任何卖出信号。"""
    st = _state(position=0.0)
    assert detect_sell_signals(st, [], [], ma_price=3037.0, cfg=_cfg()) == []
