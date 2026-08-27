"""ML/RL 滚动重训调度器（P1-7，§1/§3）。

``RetrainScheduler`` 是新编排器，组合调用既有 ``ForecastTrainer``（**零改动**），
每轮产出 model + ``FourLayerFingerprint``，注册到 ``ModelRegistry``，
并通过 ``ExperimentRecorder`` 记录实验（可选后端）。

设计要点：
- 滑动窗口逐折重训的**实际折叠逻辑**仍由 ``ForecastTrainer.run`` 内部
  ``WalkForwardSplitter`` 负责（含防泄漏校验），调度器只做**编排与资产化**，
  不重写任何训练业务逻辑，确保双闸门口径与防泄漏红线不动。
- 指纹复用 P0-3 ``compute_four_layer``；若调用方已传入 ``fingerprint`` 则直接使用，
  否则用首模型的 ``_params`` 做一次最佳努力计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..forecast.trainer import TrainResult
from ..utils.fingerprint import FourLayerFingerprint, compute_four_layer
from .recorder import ExperimentRecorder
from .registry import ModelRegistry


@dataclass
class RetrainConfig:
    """滚动重训调度配置（字段语义对齐 ``data`` / ``forecast`` 配置）。"""

    retrain_freq: str = "walk_forward"   # walk_forward | calendar
    train_period: int = 0                # 0 = 用 data.train_len
    backtest_period: int = 0
    train_len: int = 250
    test_len: int = 60
    purge: int = 5
    embargo: int = 2
    mode: str = "rolling"


@dataclass
class ScheduleResult:
    """一次 ``run`` 的汇总结果。"""

    model_id: str = ""
    n_folds: int = 0
    n_oos_signals: int = 0
    versions: list[str] = field(default_factory=list)
    fingerprint: Optional[FourLayerFingerprint] = None


class RetrainScheduler:
    """组合 ``ForecastTrainer`` 的滑动窗口重训编排器。"""

    def __init__(
        self,
        cfg: Any,
        trainer: Any,
        store: Any,
        reg: Optional[ModelRegistry] = None,
        recorder: Optional[ExperimentRecorder] = None,
    ) -> None:
        self.cfg = cfg
        self.trainer = trainer
        self.store = store
        self.reg = reg or ModelRegistry()
        self.recorder = recorder

    def run(
        self,
        barframe: Any,
        feature_frame: Any,
        fingerprint: Optional[FourLayerFingerprint] = None,
    ) -> ScheduleResult:
        """委托 ``ForecastTrainer`` 滑动窗口重训；注册模型 + 记录实验。

        参数
        ----
        barframe, feature_frame : 与 ``ForecastTrainer.run`` 一致的数据契约。
        fingerprint : 可选四层指纹；None 时由首模型 ``_params`` 最佳努力推导。
        """
        result: TrainResult = self.trainer.run(
            barframe, feature_frame, fingerprint=fingerprint
        )

        fp = fingerprint
        if fp is None and result.models:
            m0 = result.models[0]
            try:
                params = dict(getattr(m0, "_params", {}) or {})
                fp = compute_four_layer(
                    self.cfg, barframe, result.model_id, None, params
                )
            except Exception:
                fp = None

        versions: list[str] = []
        for model in result.models:
            version = f"v{len(self.reg.list_versions()) + 1}"
            self.reg.register(model, fp, version)
            versions.append(version)

        if self.recorder is not None and result.model_id:
            metrics = {
                "n_folds": result.n_folds,
                "n_oos_signals": result.n_oos_signals,
                "n_models": len(result.models),
            }
            self.recorder.log(result.model_id, fp, metrics)

        return ScheduleResult(
            model_id=result.model_id,
            n_folds=result.n_folds,
            n_oos_signals=result.n_oos_signals,
            versions=versions,
            fingerprint=fp,
        )
