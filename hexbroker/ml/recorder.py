"""实验记录器（P1-7，§1/§3）。

默认自研轻量记录器（``JsonRecorder`` / ``SqliteRecorder`` sidecar）；
``MlflowRecorder`` 为可选后端，函数内 ``try import mlflow``，**无硬依赖**。

所有指纹写入复用 P0-3 ``FourLayerFingerprint.to_dict``，与 SignalStore sidecar
同源，保证实验库与信号库口径一致、可交叉追溯。
"""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..utils.fingerprint import FourLayerFingerprint


def _fp_to_dict(fp: Any) -> dict:
    """把四层指纹规范化为纯 dict（``FourLayerFingerprint`` 或 dict 均可）。"""
    if isinstance(fp, FourLayerFingerprint):
        return fp.to_dict()
    if isinstance(fp, dict):
        return dict(fp)
    return {}


class ExperimentRecorder(ABC):
    """实验记录器抽象（自研轻量 / mlflow 可选）。"""

    @abstractmethod
    def log(self, run_id: str, fp: Any, metrics: dict) -> None:
        """记录一次实验运行（含四层指纹与指标）。"""

    @abstractmethod
    def get(self, run_id: str) -> dict:
        """读取一次实验运行的记录（缺省返回空 dict）。"""


class JsonRecorder(ExperimentRecorder):
    """自研 JSON sidecar 记录器（按 run_id 索引的单文件存储）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load_all(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def log(self, run_id: str, fp: Any, metrics: dict) -> None:
        allm = self._load_all()
        allm[run_id] = {
            "run_id": run_id,
            "fingerprint": _fp_to_dict(fp),
            "metrics": dict(metrics),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：tmp + os.replace，避免并发/中断损坏
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(allm, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, run_id: str) -> dict:
        return self._load_all().get(run_id, {})


class SqliteRecorder(ExperimentRecorder):
    """自研 SQLite sidecar 记录器（便于多 run 检索与去重）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS experiments "
            "(run_id TEXT PRIMARY KEY, fingerprint TEXT, metrics TEXT)"
        )
        self._conn.commit()

    def log(self, run_id: str, fp: Any, metrics: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO experiments (run_id, fingerprint, metrics) "
            "VALUES (?, ?, ?)",
            (
                run_id,
                json.dumps(_fp_to_dict(fp), default=str, ensure_ascii=False),
                json.dumps(dict(metrics), default=str, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def get(self, run_id: str) -> dict:
        cur = self._conn.execute(
            "SELECT fingerprint, metrics FROM experiments WHERE run_id = ?", (run_id,)
        )
        row = cur.fetchone()
        if row is None:
            return {}
        return {
            "run_id": run_id,
            "fingerprint": json.loads(row[0]),
            "metrics": json.loads(row[1]),
        }

    def close(self) -> None:
        """关闭底层连接（资源清理）。"""
        self._conn.close()


class MlflowRecorder(ExperimentRecorder):
    """可选 mlflow 后端；未安装时构造即抛 ``RuntimeError``（零硬依赖纪律）。"""

    def __init__(self, tracking_uri: Optional[str] = None) -> None:
        try:
            import mlflow
        except ImportError as exc:  # 仅函数内 try import，绝不顶层 import
            raise RuntimeError(
                "mlflow 未安装；用 JsonRecorder/SqliteRecorder 或 "
                "pip install -e .[optional]"
            ) from exc
        self._mlflow = mlflow
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)

    def log(self, run_id: str, fp: Any, metrics: dict) -> None:
        self._mlflow.log_params(_fp_to_dict(fp))
        self._mlflow.log_metrics(dict(metrics))

    def get(self, run_id: str) -> dict:
        try:
            run = self._mlflow.get_run(run_id)
            return {"run_id": run_id, "metrics": dict(run.data.metrics)}
        except Exception:
            return {}
