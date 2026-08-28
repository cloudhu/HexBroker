"""PASS 校准记录账本（P2-2 治理机制②）。

结构化、可审计、可回放的晋升/降级校准账本。JSON 落盘，原子写（.tmp + os.replace，
与 paper/broker.py snapshot 同款安全写约定）。append-only 留痕。

零顶层重依赖：仅标准库 + 项目既有 utils.logging。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import get_logger
from .scheme import SchemeStatus

log = get_logger("GOV")


@dataclass
class CalibrationRecord:
    """单方案校准记录（双闸门证据 + 数据根因 + 主理人裁决）。"""

    scheme_id: str
    status: SchemeStatus
    verdict: str                              # "PASS" / "NOT_PASS" / "DEPRECATED"
    gate1_dir_acc: Optional[float] = None    # 闸门1 方向准确率（≥54%）
    gate2_oos_cost: Optional[bool] = None     # 闸门2 OOS计成本达标
    gate2_pbo: Optional[float] = None         # PBO < 0.5
    gate2_dsr: Optional[float] = None         # DSR > 0
    n: Optional[int] = None
    maxdd: Optional[float] = None
    net_pnl_vs_cost: Optional[float] = None
    data_root_cause: str = ""
    lead_verdict: str = ""
    calibrated_at: str = ""
    source_doc: str = ""
    pending_qa: bool = False                  # 1B/2B 缺 WR/n 标记

    def __post_init__(self) -> None:
        if not self.calibrated_at:
            self.calibrated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalibrationRecord":
        d = dict(d)
        d["status"] = SchemeStatus.parse(str(d.get("status", "SHADOW_ONLY")))
        return cls(**d)


class CalibrationLedger:
    """校准账本：current（最新版）+ history（append-only 留痕）。"""

    def __init__(self, records: Optional[Dict[str, CalibrationRecord]] = None,
                 history: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
        self._current: Dict[str, CalibrationRecord] = records or {}
        self._history: Dict[str, List[Dict[str, Any]]] = history or {}

    # ---- 持久化 ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "current": {k: v.to_dict() for k, v in self._current.items()},
            "history": self._history,
        }

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationLedger":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        cur = {k: CalibrationRecord.from_dict(v) for k, v in (data.get("current") or {}).items()}
        return cls(records=cur, history=data.get("history") or {})

    def save(self, path: str | Path) -> None:
        """原子写：.tmp + os.replace（防半写损坏，与 broker snapshot 同款）。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, p)

    # ---- 读写 ----
    def get(self, scheme_id: str) -> Optional[CalibrationRecord]:
        return self._current.get(scheme_id)

    def append(self, record: CalibrationRecord) -> None:
        """追加/覆盖 current；旧版移入 history（append-only 留痕）。"""
        sid = record.scheme_id
        if sid in self._current:
            self._history.setdefault(sid, []).append(self._current[sid].to_dict())
        self._current[sid] = record

    def ids(self):
        return list(self._current.keys())

    def append_history(self, scheme_id: str, event: Dict[str, Any]) -> None:
        """向 history[sid] 追加任意留痕事件（P2-D 运行时降级用，append-only）。"""
        self._history.setdefault(scheme_id, []).append(dict(event))

    def has(self, scheme_id: str) -> bool:
        return scheme_id in self._current
