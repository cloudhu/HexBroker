"""P1-7 重训调度 + 模型注册表单测（§3）。

覆盖：
A) RetrainConfig 默认值；
B) RetrainScheduler 组合 ForecastTrainer（fake，零训练耗时），每折注册 model +
   四层指纹到 ModelRegistry，结果可复现（同输入 → 同 versions）；
C) ModelRegistry rollback 字节一致（rollback 设 current=version，load 返回该模型）；
D) 与 ExperimentRecorder 联动：scheduler.run 写入实验记录。
"""

from __future__ import annotations

from types import SimpleNamespace

from hexbroker.ml.registry import ModelRegistry
from hexbroker.ml.schedule import RetrainConfig, RetrainScheduler, ScheduleResult
from hexbroker.utils.fingerprint import FourLayerFingerprint


class _FakeTrainer:
    """轻量伪造 ForecastTrainer.run，避免真实模型训练（零耗时、可复现）。"""

    def __init__(self, model_id: str = "m1", n_models: int = 2) -> None:
        self.model_id = model_id
        self.n_models = n_models

    def run(self, barframe, feature_frame, fingerprint=None):
        models = [
            SimpleNamespace(model_id=self.model_id, _params={"lr": 1e-3})
            for _ in range(self.n_models)
        ]
        return SimpleNamespace(
            model_id=self.model_id,
            n_oos_signals=10,
            n_folds=3,
            models=models,
        )


def _fp() -> FourLayerFingerprint:
    return FourLayerFingerprint("d", "f", "m:te:mc", "p", "c")


# ---------------------------------------------------------------------------
# A) RetrainConfig 默认值
# ---------------------------------------------------------------------------
def test_retrain_config_defaults() -> None:
    cfg = RetrainConfig()
    assert cfg.retrain_freq == "walk_forward"
    assert cfg.train_period == 0
    assert cfg.backtest_period == 0
    assert cfg.train_len == 250
    assert cfg.test_len == 60
    assert cfg.purge == 5
    assert cfg.embargo == 2
    assert cfg.mode == "rolling"


# ---------------------------------------------------------------------------
# B) 调度器注册模型 + 指纹，结果可复现
# ---------------------------------------------------------------------------
def test_scheduler_registers_models_and_fingerprint() -> None:
    reg = ModelRegistry()
    sched = RetrainScheduler(cfg=None, trainer=_FakeTrainer(), store=None, reg=reg)
    fp = _fp()
    res = sched.run(barframe=None, feature_frame=None, fingerprint=fp)

    assert isinstance(res, ScheduleResult)
    assert res.model_id == "m1"
    assert res.n_folds == 3
    assert res.n_oos_signals == 10
    assert res.versions == ["v1", "v2"]

    # 每个版本都注册了带指纹的模型
    assert reg.list_versions() == ["v1", "v2"]
    assert reg.fingerprint("v1") == fp
    assert reg.fingerprint("v2") == fp
    # load 返回原始模型对象
    assert reg.load("v1").model_id == "m1"


def test_scheduler_reproducible_same_input() -> None:
    sched_a = RetrainScheduler(
        cfg=None, trainer=_FakeTrainer(), store=None, reg=ModelRegistry()
    )
    sched_b = RetrainScheduler(
        cfg=None, trainer=_FakeTrainer(), store=None, reg=ModelRegistry()
    )
    fp = _fp()
    ra = sched_a.run(None, None, fingerprint=fp)
    rb = sched_b.run(None, None, fingerprint=fp)
    assert ra.versions == rb.versions
    assert ra.model_id == rb.model_id


# ---------------------------------------------------------------------------
# C) ModelRegistry rollback 字节一致
# ---------------------------------------------------------------------------
def test_model_registry_rollback_byte_consistent() -> None:
    reg = ModelRegistry()
    m1 = SimpleNamespace(model_id="m1", tag="first")
    m2 = SimpleNamespace(model_id="m2", tag="second")
    reg.register(m1, _fp(), "v1")
    reg.register(m2, _fp(), "v2")
    assert reg.current == "v1"  # 首个注册为 current

    loaded = reg.rollback("v1")
    assert reg.current == "v1"
    assert loaded is m1  # 字节一致（同一对象引用）

    loaded2 = reg.rollback("v2")
    assert reg.current == "v2"
    assert loaded2 is m2

    # 未知版本回滚抛 KeyError
    import pytest

    with pytest.raises(KeyError):
        reg.rollback("v99")


# ---------------------------------------------------------------------------
# D) 与 ExperimentRecorder 联动
# ---------------------------------------------------------------------------
def test_scheduler_logs_experiment_when_recorder_present(tmp_path) -> None:
    from hexbroker.ml.recorder import JsonRecorder

    reg = ModelRegistry()
    rec = JsonRecorder(tmp_path / "exp.json")
    sched = RetrainScheduler(
        cfg=None, trainer=_FakeTrainer(), store=None, reg=reg, recorder=rec
    )
    fp = _fp()
    res = sched.run(None, None, fingerprint=fp)

    rec_data = rec.get(res.model_id)
    assert rec_data.get("fingerprint") == fp.to_dict()
    assert rec_data.get("metrics", {}).get("n_folds") == 3
