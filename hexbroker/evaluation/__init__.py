"""评估层（§2.2 L7 / §3.6）。

P0-4 新增导出：``block_bootstrap`` / ``bootstrap_metrics_ci`` /
``BootstrapResult`` / ``MetricCI`` / ``bootstrap_report_block``。
"""

from .metrics import MetricsReport, compute_metrics
from .baseline import run_baselines, BaselineStrategy
from .bootstrap import (
    BootstrapResult,
    MetricCI,
    block_bootstrap,
    bootstrap_metrics_ci,
    bootstrap_report_block,
)

__all__ = [
    "MetricsReport",
    "compute_metrics",
    "run_baselines",
    "BaselineStrategy",
    "BootstrapResult",
    "MetricCI",
    "block_bootstrap",
    "bootstrap_metrics_ci",
    "bootstrap_report_block",
]
