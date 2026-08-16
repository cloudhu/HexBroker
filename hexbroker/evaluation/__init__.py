"""评估层（§2.2 L7 / §3.6）。"""

from .metrics import MetricsReport, compute_metrics
from .baseline import run_baselines, BaselineStrategy

__all__ = ["MetricsReport", "compute_metrics", "run_baselines", "BaselineStrategy"]
