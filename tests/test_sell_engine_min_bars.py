"""S2/S5 开仓保护门（min_bars）单测 —— 2026-09-01 事故回归。

事故背景
--------
2026-09-01 14:00:10 模拟盘 rb0 开多 1 手 @3197，14:01:10 即被 **S5 波动异常**平仓。
根因：生产 bars 来自 sina 日线且**不含当日**，``recent_returns[-1]`` 恒为「昨日
（08-31）已实现收益 +2.731%」，z = 3.6411 > 3.0。该值一整天不变，故一旦成立就会
在每个 tick 重复成立 → 「开仓 → 下一 tick 秒平」循环。

S1 自 P0 期即有 ``sell_s1_min_bars`` 开仓缓冲，S2/S5 缺失 → 本次补齐，并引入
统一门 ``sell_min_bars``（缺省 2），支持逐信号覆写 ``sell_sN_min_bars``。
"""

from types import SimpleNamespace

import numpy as np

from hexbroker.constants import SellSignalCode
from hexbroker.risk.sell_engine import detect_sell_signals
from hexbroker.risk.types import RiskState


def _cfg(**kw) -> SimpleNamespace:
    """最小配置（缺省即生产冻结默认值）。"""
    base = dict(
        sell_min_bars=2,
        sell_s1_window=20,
        sell_s1_band_atr=0.1,
        sell_s3_target=0.10,
        sell_s4_bars=20,
        sell_s5_z=3.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _state(**kw) -> RiskState:
    base = dict(symbol="rb0", position=1.0, current_price=3197.0, entry_price=3198.0,
                atr=30.9286, bars_in_position=1, pnl_pct=0.0, realized_vol=0.0097)
    base.update(kw)
    return RiskState(**base)


def _spike_returns(n: int = 20, spike: float = 0.027314) -> np.ndarray:
    """构造「末根为异常大阳线」的收益序列（复刻 rb0 2026-08-31 的 +2.731%）。

    前 n-1 根在 ±0.4% 内往复（mu≈0, sd≈0.004），末根 spike → z 远超 3.0。
    """
    base = np.array([0.004 if i % 2 == 0 else -0.004 for i in range(n - 1)], dtype=float)
    return np.concatenate([base, [spike]])


# ---------------------------------------------------------------------------
# ① S5 开仓保护（事故直接回归）
# ---------------------------------------------------------------------------
def test_s5_blocked_on_first_bar() -> None:
    """事故复现：持仓第 1 根（bars_in_position=1）时 S5 必须被保护门拦下。"""
    rets = _spike_returns()
    # 自检：该序列在无保护时确实会触发 S5（否则本用例失去意义）
    z = abs((rets[-1] - rets[-20:].mean()) / rets[-20:].std())
    assert z > 3.0, f"构造序列 z={z:.2f} 未超阈值，用例无效"

    st = _state(bars_in_position=1)
    sigs = detect_sell_signals(st, rets, np.array([]), ma_price=None, cfg=_cfg())
    assert SellSignalCode.S5_VOLATILITY_SPIKE not in sigs, "保护门失效：开仓首根即被 S5 平仓"


def test_s5_allowed_after_min_bars() -> None:
    """持仓满 min_bars 后 S5 恢复工作（保护门不是永久禁用）。"""
    rets = _spike_returns()
    st = _state(bars_in_position=2)
    sigs = detect_sell_signals(st, rets, np.array([]), ma_price=None, cfg=_cfg())
    assert SellSignalCode.S5_VOLATILITY_SPIKE in sigs


def test_s5_min_bars_override() -> None:
    """逐信号覆写 sell_s5_min_bars 生效（独立于统一门）。"""
    rets = _spike_returns()
    cfg = _cfg(sell_min_bars=2, sell_s5_min_bars=4)
    assert SellSignalCode.S5_VOLATILITY_SPIKE not in detect_sell_signals(
        _state(bars_in_position=3), rets, np.array([]), ma_price=None, cfg=cfg)
    assert SellSignalCode.S5_VOLATILITY_SPIKE in detect_sell_signals(
        _state(bars_in_position=4), rets, np.array([]), ma_price=None, cfg=cfg)


# ---------------------------------------------------------------------------
# ② S2 开仓保护
# ---------------------------------------------------------------------------
def test_s2_blocked_on_first_bar() -> None:
    """S2 量价背离同样受开仓保护（此前无保护）。"""
    vols = np.array([100.0] * 19 + [500.0])       # 末根放量 5×
    rets = np.concatenate([np.zeros(19), [-0.01]])  # 价格下跌
    st = _state(bars_in_position=1)
    sigs = detect_sell_signals(st, rets, vols, ma_price=None, cfg=_cfg())
    assert SellSignalCode.S2_VOL_DIVERGENCE not in sigs

    st2 = _state(bars_in_position=2)
    sigs2 = detect_sell_signals(st2, rets, vols, ma_price=None, cfg=_cfg())
    assert SellSignalCode.S2_VOL_DIVERGENCE in sigs2


def test_s2_survives_empty_returns() -> None:
    """recent_volumes 非空但 recent_returns 为空时不抛 IndexError。"""
    vols = np.array([100.0, 500.0])
    st = _state(bars_in_position=5)
    sigs = detect_sell_signals(st, np.array([]), vols, ma_price=None, cfg=_cfg())
    assert sigs == []


# ---------------------------------------------------------------------------
# ③ 统一门与缺省值
# ---------------------------------------------------------------------------
def test_unified_min_bars_gate_applies_to_all() -> None:
    """sell_min_bars 统一门同时作用于 S1/S2/S5（大跌 spike 使三者可同现）。

    用**大跌** spike 而非大涨：S2 要求 ``not price_up``，正收益时 S2 天然不成立。
    """
    rets = _spike_returns(spike=-0.027314)
    vols = np.array([100.0] * 19 + [500.0])
    cfg = _cfg(sell_min_bars=3)
    for bars in (1, 2):
        st = _state(bars_in_position=bars, current_price=3000.0)  # 价格远低于 MA → S1 亦满足
        sigs = detect_sell_signals(st, rets, vols, ma_price=4000.0, cfg=cfg)
        assert sigs == [], f"bars={bars} 时不应有任何卖出信号"
    st = _state(bars_in_position=3, current_price=3000.0)
    sigs = detect_sell_signals(st, rets, vols, ma_price=4000.0, cfg=cfg)
    assert SellSignalCode.S1_TREND_BREAK in sigs
    assert SellSignalCode.S2_VOL_DIVERGENCE in sigs
    assert SellSignalCode.S5_VOLATILITY_SPIKE in sigs


def test_defaults_when_cfg_is_none() -> None:
    """cfg=None（无 risk 配置段）时回退统一缺省值 2。"""
    rets = _spike_returns()
    assert SellSignalCode.S5_VOLATILITY_SPIKE not in detect_sell_signals(
        _state(bars_in_position=1), rets, np.array([]), ma_price=None, cfg=None)
    assert SellSignalCode.S5_VOLATILITY_SPIKE in detect_sell_signals(
        _state(bars_in_position=2), rets, np.array([]), ma_price=None, cfg=None)


# ---------------------------------------------------------------------------
# ④ 回归：既有行为不得改变
# ---------------------------------------------------------------------------
def test_s1_behavior_unchanged() -> None:
    """S1 仍受 sell_s1_min_bars 控制，且缺省回退统一门。"""
    st = _state(bars_in_position=1, current_price=3036.0, atr=40.0)
    cfg = _cfg(sell_s1_band_atr=0.0)
    assert SellSignalCode.S1_TREND_BREAK not in detect_sell_signals(
        st, np.array([]), np.array([]), ma_price=3037.0, cfg=cfg)
    assert SellSignalCode.S1_TREND_BREAK in detect_sell_signals(
        _state(bars_in_position=2, current_price=3036.0, atr=40.0),
        np.array([]), np.array([]), ma_price=3037.0, cfg=cfg)


def test_s3_s4_not_gated_by_min_bars() -> None:
    """S3（止盈）/S4（时间止损）不受开仓保护门影响。"""
    st = _state(bars_in_position=1, pnl_pct=0.20)   # 浮盈 20% > target 10%
    sigs = detect_sell_signals(st, np.array([]), np.array([]), ma_price=None, cfg=_cfg())
    assert SellSignalCode.S3_TARGET_REACHED in sigs

    st4 = _state(bars_in_position=25, pnl_pct=-0.01)
    sigs4 = detect_sell_signals(st4, np.array([]), np.array([]), ma_price=None, cfg=_cfg())
    assert SellSignalCode.S4_TIME_STOP in sigs4


def test_flat_position_still_short_circuits() -> None:
    """空仓仍整体短路（无持仓不产生任何卖出信号）。"""
    rets = _spike_returns()
    st = _state(position=0.0, bars_in_position=0)
    assert detect_sell_signals(st, rets, np.array([]), ma_price=3038.6, cfg=_cfg()) == []
