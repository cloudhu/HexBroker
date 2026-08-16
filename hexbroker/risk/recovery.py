"""回撤恢复分级 R0–R4（§3.4）。

依据当前回撤比例映射到恢复阶段与对应仓位缩放标量：
- R0 正常：缩放 1.0
- R1 轻度回撤：缩放 0.5
- R2 中度回撤：暂停开仓（缩放 0.0）
- R3 重度回撤：小仓试探（缩放 0.2）
- R4 极端回撤：恢复满仓（缩放 1.0，仅用于走出泥潭后）

阈值与缩放标量由 ``RiskConfig`` 提供。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..constants import RecoveryStage


def recovery_stage(
    drawdown: float,
    dd_r1: float = 0.05,
    dd_r2: float = 0.10,
    dd_r3: float = 0.15,
) -> RecoveryStage:
    """由回撤比例映射恢复阶段。"""
    if drawdown >= dd_r3:
        return RecoveryStage.R3_PROBE
    if drawdown >= dd_r2:
        return RecoveryStage.R2_HALT
    if drawdown >= dd_r1:
        return RecoveryStage.R1_REDUCE
    return RecoveryStage.R0_NORMAL


def recovery_scalar(
    stage: RecoveryStage,
    s_r1: float = 0.5,
    s_r2: float = 0.0,
    s_r3: float = 0.2,
    s_r4: float = 1.0,
) -> float:
    """恢复阶段 -> 仓位缩放标量。"""
    return {
        RecoveryStage.R0_NORMAL: 1.0,
        RecoveryStage.R1_REDUCE: float(s_r1),
        RecoveryStage.R2_HALT: float(s_r2),
        RecoveryStage.R3_PROBE: float(s_r3),
        RecoveryStage.R4_RESTORE: float(s_r4),
    }[stage]
