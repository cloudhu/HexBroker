"""预测层（§2.2 L3）：自回归预测模型与 OOS 信号落盘。"""

from .base import ForecastSignal, ForecastModel, TrainLog, build_windows
from . import autoregressive  # noqa: F401  (注册 ar_transformer)
from . import baselines  # noqa: F401  (注册 tcn/gru/lightgbm)
from . import kronos_adapter  # noqa: F401  (注册 kronos)
from . import calibration
from . import threshold
from . import signal_store
from . import ensemble
from . import trainer
from . import kronos_dataio

__all__ = [
    "ForecastSignal",
    "ForecastModel",
    "TrainLog",
    "build_windows",
    "calibration",
    "threshold",
    "signal_store",
    "ensemble",
    "trainer",
    "kronos_dataio",
]
