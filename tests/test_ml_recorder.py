"""P1-7 实验记录器单测（§3）：四层指纹往返 + mlflow 可选后端纪律。

覆盖：
A) JsonRecorder log/get 往返（四层指纹完整保留，多 run 互不覆盖）；
B) SqliteRecorder log/get 往返；
C) 抽象基类不可实例化；
D) MlflowRecorder 零依赖纪律：mlflow 未安装时构造即抛 RuntimeError（已安装则 skip）。
"""

from __future__ import annotations

import pytest

from hexbroker.ml.recorder import (
    ExperimentRecorder,
    JsonRecorder,
    MlflowRecorder,
    SqliteRecorder,
)
from hexbroker.utils.fingerprint import FourLayerFingerprint


def _fp() -> FourLayerFingerprint:
    return FourLayerFingerprint("d1", "f1", "m1:te:mc1", "p1", "c1")


# ---------------------------------------------------------------------------
# A) JsonRecorder 往返
# ---------------------------------------------------------------------------
def test_json_recorder_roundtrip(tmp_path) -> None:
    rec = JsonRecorder(tmp_path / "exp.json")
    fp = _fp()
    rec.log("run-1", fp, {"sharpe": 1.2, "n_folds": 3})
    data = rec.get("run-1")
    assert data["fingerprint"] == fp.to_dict()
    assert data["metrics"]["sharpe"] == 1.2

    # 多次写入（不同 run）互不覆盖
    rec.log("run-2", fp, {"sharpe": 2.0})
    assert rec.get("run-2")["metrics"]["sharpe"] == 2.0
    assert rec.get("run-1")["metrics"]["sharpe"] == 1.2


# ---------------------------------------------------------------------------
# B) SqliteRecorder 往返
# ---------------------------------------------------------------------------
def test_sqlite_recorder_roundtrip(tmp_path) -> None:
    rec = SqliteRecorder(tmp_path / "exp.db")
    fp = _fp()
    rec.log("run-1", fp, {"sharpe": 1.5, "n_folds": 5})
    data = rec.get("run-1")
    assert data["fingerprint"] == fp.to_dict()
    assert data["metrics"]["sharpe"] == 1.5
    rec.close()


# ---------------------------------------------------------------------------
# C) 抽象基类不可实例化
# ---------------------------------------------------------------------------
def test_recorder_is_abstract() -> None:
    with pytest.raises(TypeError):
        ExperimentRecorder()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# D) mlflow 零依赖纪律
# ---------------------------------------------------------------------------
def test_mlflow_recorder_optional_dependency_discipline() -> None:
    """零依赖纪律：mlflow 仅 optional，未安装时构造抛 RuntimeError。"""
    try:
        import mlflow  # noqa: F401

        pytest.skip("mlflow 已安装，跳过未安装路径断言")
    except ImportError:
        pass
    with pytest.raises(RuntimeError):
        MlflowRecorder()
