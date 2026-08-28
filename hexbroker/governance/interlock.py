"""防误开联锁（P2-1 治理机制①）。

方案从 SHADOW_ONLY 切 LIVE 前，必须经 walk-forward QA 门禁且有有效校准记录，
否则代码级硬拒绝（fail-safe：resolve_scheme_mode 返回 shadow + reason，不抛异常）。

零顶层重依赖：仅标准库 + 项目既有 utils.logging。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

from .scheme import SchemeRegistry, SchemeStatus

from ..utils.logging import get_logger

log = get_logger("GOV")


class MisOpenBlocked(Exception):
    """联锁拒绝：方案不可切 LIVE。reason 枚举：unknown_scheme/no_calibration/not_pass/deprecated/shadow_only。"""

    def __init__(self, scheme_id: str, reason: str) -> None:
        self.scheme_id = scheme_id
        self.reason = reason
        super().__init__(f"方案 {scheme_id} 未过治理联锁({reason})，禁止切 LIVE")


class ResolvedMode(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"


# status -> 拦截 reason（非 PASS 一律拦截）
_STATUS_REASON = {
    SchemeStatus.NOT_PASS: "not_pass",
    SchemeStatus.DEPRECATED: "deprecated",
    SchemeStatus.SHADOW_ONLY: "shadow_only",
}


def _reason_of(status: SchemeStatus) -> str:
    return _STATUS_REASON.get(status, "not_pass")


def assert_safe_to_activate(scheme_id: str, registry: SchemeRegistry) -> None:
    """硬断言：方案可切 LIVE。否则抛 MisOpenBlocked（fail-fast 语义，供单测/显式调用）。"""
    s = registry.get(scheme_id)
    if s is None:
        raise MisOpenBlocked(scheme_id, "unknown_scheme")  # 默认 fail-safe：未知方案禁实盘
    if s.status is not SchemeStatus.PASS:
        raise MisOpenBlocked(scheme_id, _reason_of(s.status))
    if not registry.has_calibration(scheme_id):
        raise MisOpenBlocked(scheme_id, "no_calibration")


def resolve_scheme_mode(
    requested_mode: str, scheme_id: str, registry: SchemeRegistry
) -> Tuple[ResolvedMode, Optional[str]]:
    """fail-safe 决议：请求 LIVE/trade 且联锁拒绝 → 强制 SHADOW + reason；不抛异常。

    requested_mode 为 shadow/accumulate 等非 LIVE → 原样返回 SHADOW。
    """
    if requested_mode in ("live", "trade"):
        try:
            assert_safe_to_activate(scheme_id, registry)
            return ResolvedMode.LIVE, None
        except MisOpenBlocked as e:
            return ResolvedMode.SHADOW, e.reason
    return ResolvedMode.SHADOW, None
