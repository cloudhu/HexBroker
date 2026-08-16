"""仓位限额与硬止损（§3.4 红线）。

硬止损为最高优先级：一旦账户回撤超过阈值，立即清仓，任何信号/RL 意图均不可覆盖。
"""

from __future__ import annotations

import numpy as np


# 硬止损回撤阈值（冻结默认；可在 RiskConfig 覆盖）
HARD_STOP_DRAWDOWN = 0.25


def hard_stop_triggered(drawdown: float, threshold: float = HARD_STOP_DRAWDOWN) -> bool:
    """回撤超过硬止损阈值即触发。"""
    return float(drawdown) >= float(threshold)


def position_within_limit(target: float, max_position_pct: float = 0.30) -> float:
    """将目标仓位裁剪到单标的上限内。"""
    return float(np.clip(target, -abs(max_position_pct), abs(max_position_pct)))
