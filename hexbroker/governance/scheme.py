"""六方案治理寄存器（P2-3 落地①）。

纯增量模块：方案级（策略级）治理控制平面，与 paper.yaml 的 symbols.mode（合约级）维度无关。
仅用标准库 + 项目既有 utils.logging，零顶层重依赖，红线安全。

- SchemeStatus：PASS / SHADOW_ONLY / DEPRECATED / NOT_PASS
- Scheme：单方案登记（status + 数据根因 + 门禁指标 + 校准引用）
- SchemeRegistry：加载 YAML + verify_all（非 SHADOW_ONLY 须有校准记录，联锁前置不变量）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import yaml  # 顶层 import，与 market/rule.py、factor/registry.py 同款（pyyaml 轻量已用）

from ..utils.logging import get_logger

log = get_logger("GOV")


class SchemeStatus(str, Enum):
    """方案治理状态（双闸门验收口径）。"""

    PASS = "PASS"                 # 过双闸门，可晋升 LIVE（须有校准记录）
    SHADOW_ONLY = "SHADOW_ONLY"   # 仅影子/模拟，不可实盘
    DEPRECATED = "DEPRECATED"     # 弃用
    NOT_PASS = "NOT_PASS"         # 未过门禁，维持影子

    @classmethod
    def parse(cls, value: str) -> "SchemeStatus":
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"未知 SchemeStatus: {value!r}（允许 {[s.value for s in cls]}）"
            ) from exc


# 可切 LIVE 的状态集合（其余一律强制 SHADOW_ONLY）
LIVE_ALLOWED = frozenset({SchemeStatus.PASS})


@dataclass
class Scheme:
    """单方案治理登记项。"""

    scheme_id: str
    name: str
    status: SchemeStatus
    data_root_cause: str = ""
    gate_metrics: Dict[str, Any] = field(default_factory=dict)
    calibration_ref: Optional[str] = None

    @property
    def is_live_allowed(self) -> bool:
        return self.status in LIVE_ALLOWED

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Scheme":
        return cls(
            scheme_id=str(d["scheme_id"]),
            name=str(d.get("name", d["scheme_id"])),
            status=SchemeStatus.parse(str(d["status"])),
            data_root_cause=str(d.get("data_root_cause", "")),
            gate_metrics=dict(d.get("gate_metrics", {}) or {}),
            calibration_ref=d.get("calibration_ref"),
        )


class SchemeRegistry:
    """六方案治理寄存器：加载 YAML + 启动期不变量校验。"""

    def __init__(self, schemes: Dict[str, Scheme]) -> None:
        self._schemes = schemes

    @classmethod
    def load(cls, yaml_path: str | Path) -> "SchemeRegistry":
        """从 YAML 加载（``schemes`` 段为方案登记，键为 scheme_id）。"""
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
        raw = data.get("schemes", data) if isinstance(data, dict) else {}
        schemes: Dict[str, Scheme] = {}
        for sid, item in raw.items():
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["scheme_id"] = sid
            schemes[sid] = Scheme.from_dict(item)
        reg = cls(schemes)
        reg.verify_all()
        return reg

    def ids(self):
        return list(self._schemes.keys())

    def get(self, scheme_id: str) -> Optional[Scheme]:
        return self._schemes.get(scheme_id)

    def has_calibration(self, scheme_id: str) -> bool:
        s = self._schemes.get(scheme_id)
        return bool(s and s.calibration_ref)

    def verify_all(self) -> None:
        """启动期不变量：非 SHADOW_ONLY/DEPRECATED 方案须有校准引用。

        - PASS 必须有 calibration_ref（否则联锁前置失败）
        - NOT_PASS/DEPRECATED/SHADOW_ONLY 不强制（联锁本就拦截 LIVE）
        """
        for sid, s in self._schemes.items():
            if s.status is SchemeStatus.PASS and not s.calibration_ref:
                raise ValueError(
                    f"方案 {sid} status=PASS 但缺失 calibration_ref（联锁前置不变量违例）"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            sid: {
                "name": s.name,
                "status": s.status.value,
                "data_root_cause": s.data_root_cause,
                "gate_metrics": s.gate_metrics,
                "calibration_ref": s.calibration_ref,
            }
            for sid, s in self._schemes.items()
        }
