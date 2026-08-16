"""ATR 止损计算与三档 ratchet（§3.4）。

``compute_stop`` 给出基于 ATR 的止损价；``ATRRatchet`` 维护「只增不减」的档位状态，
确保持仓期间止损距离不会收窄（防止在波动放大后反而放松保护）。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .types import ATRTier, RiskState


class ATRRatchet:
    """ATR 三档 ratchet：档位索引（越大止损越窄）只增不减。"""

    def __init__(self, initial: ATRTier = ATRTier.HIGH) -> None:
        self.tier = ATRTier(int(initial))

    def update(self, vol_quantile: float, vol_low_q: float = 0.2, vol_high_q: float = 0.8) -> ATRTier:
        """根据波动分位推进档位。

        - 波动处于高分位（> vol_high_q）：收紧到最宽（HIGH，索引 0）。
        - 波动处于低分位（< vol_low_q）：可收窄到最窄（LOW，索引 2）。
        - 其余维持不动。
        ratchet 保证索引只增不减。
        """
        if vol_quantile >= vol_high_q:
            new = ATRTier.HIGH
        elif vol_quantile <= vol_low_q:
            new = ATRTier.LOW
        else:
            new = ATRTier.MID
        # 只增不减：新档位索引 >= 当前索引 才更新
        if int(new) >= int(self.tier):
            self.tier = new
        return self.tier

    def reset(self, tier: ATRTier = ATRTier.HIGH) -> None:
        self.tier = ATRTier(int(tier))


def compute_stop(
    entry_price: float,
    position: float,
    atr: float,
    tier: ATRTier,
) -> Optional[float]:
    """计算 ATR 止损价（基于建仓价与当前档位倍率）。

    - 多头：stop = entry * (1 - mult * atr/entry)
    - 空头：stop = entry * (1 + mult * atr/entry)
    返回 None 表示无有效止损（atr<=0 或空仓）。
    """
    if position == 0 or atr <= 0 or entry_price <= 0:
        return None
    ratio = tier.multiplier * atr / entry_price
    if position > 0:
        return entry_price * (1.0 - ratio)
    return entry_price * (1.0 + ratio)


def trailing_stop(
    current_price: float,
    position: float,
    atr: float,
    tier: ATRTier,
    prev_stop: Optional[float] = None,
) -> float:
    """移动止损：取 ATR 止损与历史止损的更优（对持仓更有利）值。"""
    base = compute_stop(current_price, position, atr, tier)
    if base is None:
        return prev_stop if prev_stop is not None else current_price
    if prev_stop is None:
        return base
    # 多头时止损只能上移；空头时只能下移
    if position > 0:
        return max(prev_stop, base)
    return min(prev_stop, base)
