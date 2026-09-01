"""S1–S5 卖出信号引擎（§3.4）。

依据持仓状态与行情上下文检测五类风控卖出信号：
- S1 趋势破坏：价格跌破 N 日均线。
- S2 量价背离：放量但价格走平/下跌。
- S3 目标达成：浮盈达到目标比例（止盈）。
- S4 时间止损：持仓超过阈值根数仍未盈利。
- S5 波动异常：单根收益 z 分数超过阈值。

所有阈值由 ``RiskConfig`` 提供，缺省为冻结友好值。

⚠️ 开仓保护门（min_bars）——2026-09-01 事故修复
------------------------------------------------
S1/S2/S5 三者都依赖**日频历史序列**（MA / 成交量 / 收益 z 分数）。在实盘 tick 级
（60s）循环里，这些序列**一整天不变**，一旦成立就会在每个 tick 重复成立，导致
「开仓 → 下一 tick 立刻平仓 → 再开 → 再平」的循环（09-01 rb0 实测：14:00 开仓，
14:01 被 S5 平仓，z=3.64 来自**昨日** 08-31 的 2.731% 涨幅，与当前 tick 无关）。

故 S1/S2/S5 **一律**受 ``bars_in_position >= min_bars`` 保护（S1 自 P0 期即有，
S2/S5 于本次补齐）。S3（止盈）与 S4（时间止损）不在此列：S4 本就以 bars 为门，
S3 用实时浮盈判定且不因历史序列滞后而误伤。

⚠️ 实盘 bars 滞后一根（客观约束，勿当作 bug 修）
------------------------------------------------
生产 bars 来自 sina 日线（``paper/quotes.fetch_bars``），**不含当日**，末根为
上一交易日。故实盘 ``recent_returns[-1]`` 语义是「昨日已实现收益」而非「当前波动」，
相对回测（``rl/futures_env._market_context`` 含当前 bar）**整体滞后一根**。

实测两种口径触发率：滞后口径 0.70%，改为「盘中实时收益」口径 2.62%
（因 r 不再落入 mu/sd 窗口，z 失去自抑制而上界消失 → 显著更激进）。
故**不做**口径替换——回测与实盘应保持同一分布，差异仅是可接受的一天时滞。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..constants import SellSignalCode
from .types import RiskState


def detect_sell_signals(
    state: RiskState,
    recent_returns: np.ndarray,  # 最近若干根的单根收益（用于 S5）
    recent_volumes: np.ndarray,  # 最近若干根成交量
    ma_price: Optional[float] = None,  # 当前均线价（用于 S1）
    cfg: Optional[object] = None,
) -> list[SellSignalCode]:
    """返回当前触发的卖出信号列表（可能为空）。"""
    signals: list[SellSignalCode] = []
    if state.position == 0:
        return signals

    # 读取阈值（优先 cfg，否则冻结默认）
    # 统一开仓保护门：S1/S2/S5 共用缺省值，可逐信号覆写（sell_sN_min_bars）。
    min_bars = getattr(cfg, "sell_min_bars", 2) if cfg else 2
    s1_window = getattr(cfg, "sell_s1_window", 20) if cfg else 20
    s1_min_bars = getattr(cfg, "sell_s1_min_bars", min_bars) if cfg else min_bars
    s1_band_atr = getattr(cfg, "sell_s1_band_atr", 0.1) if cfg else 0.1  # P0：MA 穿越带宽死区（×ATR）
    # S2/S5 开仓保护（2026-09-01 事故补齐）：与 S1 同门的缺省值。
    s2_min_bars = getattr(cfg, "sell_s2_min_bars", min_bars) if cfg else min_bars
    s3_target = getattr(cfg, "sell_s3_target", 0.10) if cfg else 0.10
    s4_bars = getattr(cfg, "sell_s4_bars", 20) if cfg else 20
    s5_z = getattr(cfg, "sell_s5_z", 3.0) if cfg else 3.0
    s5_min_bars = getattr(cfg, "sell_s5_min_bars", min_bars) if cfg else min_bars

    # S1 趋势破坏：价格 < 均线（多头）或 > 均线（空头）。
    # P0 修复（避免「开仓后 60s 秒平」的贴线穿越循环）：
    #   ① 开仓缓冲：bars_in_position < s1_min_bars 时跳过 S1，给新仓保护期；
    #      （⚠️ 语义提示：bars_in_position 三端口径不同——RL/回测=bar 数，paper 实盘=自然日
    #       天数，详见 24-bars-semantics-assessment.md；S4 同字段，bar_freq 改频前须对齐）
    #   ② 带宽死区：须跌破 ma − band（多头）/ 升破 ma + band（空头）才判趋势破坏，
    #      band = s1_band_atr × ATR，过滤价格贴均线微幅往返的误触发。
    if (
        state.bars_in_position >= s1_min_bars
        and ma_price is not None
        and state.current_price > 0
        and state.atr > 0
    ):
        band = s1_band_atr * state.atr
        if state.position > 0 and state.current_price < ma_price - band:
            signals.append(SellSignalCode.S1_TREND_BREAK)
        elif state.position < 0 and state.current_price > ma_price + band:
            signals.append(SellSignalCode.S1_TREND_BREAK)

    # S2 量价背离：最新成交量显著放大但价格未创新高（多头情景）
    # 开仓保护（2026-09-01）：与 S1 同门，避免「开仓 → 下一 tick 秒平」循环。
    # 顺带免疫 recent_volumes 非空但 recent_returns 为空时的 IndexError。
    if (
        len(recent_volumes) >= 2
        and len(recent_returns) >= 1
        and state.bars_in_position >= s2_min_bars
    ):
        vol_ratio = recent_volumes[-1] / max(recent_volumes[:-1].mean(), 1e-9)
        price_up = recent_returns[-1] > 0
        if vol_ratio > 2.0 and not price_up:
            signals.append(SellSignalCode.S2_VOL_DIVERGENCE)

    # S3 目标达成（止盈）：浮盈达到目标
    if state.pnl_pct >= s3_target:
        signals.append(SellSignalCode.S3_TARGET_REACHED)

    # S4 时间止损：持仓过久且未盈利
    if state.bars_in_position >= s4_bars and state.pnl_pct <= 0.0:
        signals.append(SellSignalCode.S4_TIME_STOP)

    # S5 波动异常：最新单根收益 z 分数超阈值
    # 开仓保护（2026-09-01 事故）：实盘 bars 不含当日 → recent_returns[-1] 恒为
    # 「昨日已实现收益」，一整天不变；若无持仓门，开仓后首个 tick 即被平仓。
    if len(recent_returns) >= 5 and state.bars_in_position >= s5_min_bars:
        r = recent_returns[-1]
        mu = recent_returns[-s4_bars:].mean()
        sd = recent_returns[-s4_bars:].std()
        if sd > 1e-9 and abs((r - mu) / sd) > s5_z:
            signals.append(SellSignalCode.S5_VOLATILITY_SPIKE)

    return signals


# 优先级：数字越小越优先（用于排序/取最强信号）
SELL_PRIORITY = {
    SellSignalCode.S1_TREND_BREAK: 1,
    SellSignalCode.S2_VOL_DIVERGENCE: 2,
    SellSignalCode.S3_TARGET_REACHED: 3,
    SellSignalCode.S4_TIME_STOP: 4,
    SellSignalCode.S5_VOLATILITY_SPIKE: 5,
}


def strongest(signals: list[SellSignalCode]) -> Optional[SellSignalCode]:
    """返回优先级最高的卖出信号（数值最小）。"""
    if not signals:
        return None
    return min(signals, key=lambda s: SELL_PRIORITY.get(s, 99))
