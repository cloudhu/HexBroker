"""风险预算与仓位缩放（§3.4）。

依据波动率目标（vol_target）与 Kelly 上限（kelly_cap）计算单标的允许的目标仓位比例，
并叠加信号强度（如 p_up 偏离 0.5 的程度）做方向性缩放。
"""

from __future__ import annotations

import numpy as np


def vol_target_fraction(
    volatility: float,
    vol_target: float = 0.20,
    kelly_cap: float = 0.25,
) -> float:
    """由波动率目标给出基础仓位比例（波动率越低，仓位越高）。

    基础比例 = vol_target / max(volatility, eps)，并以 kelly_cap 封顶。
    """
    if volatility <= 0:
        return 0.0
    frac = vol_target / volatility
    return float(np.clip(frac, 0.0, kelly_cap))


def signal_strength(p_up: float) -> float:
    """由 p_up 给出 [-1, 1] 方向强度（偏离 0.5 越远越强）。"""
    return float(np.clip((p_up - 0.5) * 2.0, -1.0, 1.0))


def budget_target(
    p_up: float,
    volatility: float,
    vol_target: float = 0.20,
    kelly_cap: float = 0.25,
    max_position_pct: float = 0.30,
) -> float:
    """综合得到目标仓位比例（含波动率目标与方向强度）。"""
    base = vol_target_fraction(volatility, vol_target, kelly_cap)
    strength = signal_strength(p_up)
    target = base * strength
    return float(np.clip(target, -max_position_pct, max_position_pct))
