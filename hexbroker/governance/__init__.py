"""六方案治理控制平面（P2 双治理 + 落地）。

导出：方案状态/登记、防误开联锁、PASS 校准记录账本。
纯增量模块，不改动任何既有交易/信号/风控逻辑；默认 fail-safe（未知方案强制 SHADOW_ONLY）。
"""
from .scheme import SchemeStatus, Scheme, SchemeRegistry, LIVE_ALLOWED
from .interlock import (
    MisOpenBlocked,
    ResolvedMode,
    assert_safe_to_activate,
    resolve_scheme_mode,
)
from .ledger import CalibrationRecord, CalibrationLedger

__all__ = [
    "SchemeStatus",
    "Scheme",
    "SchemeRegistry",
    "LIVE_ALLOWED",
    "MisOpenBlocked",
    "ResolvedMode",
    "assert_safe_to_activate",
    "resolve_scheme_mode",
    "CalibrationRecord",
    "CalibrationLedger",
]
