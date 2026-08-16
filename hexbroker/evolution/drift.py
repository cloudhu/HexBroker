"""PSI（Population Stability Index）漂移检测（§3.7 / §8.7）。

比较基准窗与最近窗的特征分布，PSI > 阈值（默认 0.2）触发 ``DriftEvent``，
通知进化层外层 C 启动在线神经架构搜索（对抗市场非平稳）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class DriftEvent:
    """漂移事件。"""

    feature: str
    psi: float
    threshold: float
    trigger_ts: Optional[object] = None

    @property
    def triggered(self) -> bool:
        return self.psi > self.threshold


def psi(actual: np.ndarray, expected: np.ndarray, n_bins: int = 10, eps: float = 1e-6) -> float:
    """计算单特征 PSI。

    分箱按 expected 的分位数等宽切分；actual 落入同箱后比较占比。
    """
    a = np.asarray(actual, dtype=float)
    e = np.asarray(expected, dtype=float)
    if a.size < 2 or e.size < 2:
        return 0.0
    bins = np.quantile(e, np.linspace(0.0, 1.0, n_bins + 1))
    bins[0] = -np.inf
    bins[-1] = np.inf
    p_e, _ = np.histogram(e, bins=bins)
    p_a, _ = np.histogram(a, bins=bins)
    p_e = (p_e + eps) / e.size
    p_a = (p_a + eps) / a.size
    return float(np.sum((p_a - p_e) * np.log(p_a / p_e)))


class DriftDetector:
    """多特征漂移检测器。"""

    def __init__(self, threshold: float = 0.2, n_bins: int = 10, baseline_len: int = 120) -> None:
        self.threshold = float(threshold)
        self.n_bins = int(n_bins)
        self.baseline_len = int(baseline_len)

    def detect(
        self, df: pd.DataFrame, feature_cols: Optional[list[str]] = None
    ) -> list[DriftEvent]:
        """df 为单标的 datetime 索引特征帧；前 baseline_len 为基准窗，其后为当前窗。"""
        cols = feature_cols or [c for c in df.columns if c.startswith("f_") or c in ("p_up", "vol_hat")]
        if len(df) < 2 * self.baseline_len:
            return []
        events: list[DriftEvent] = []
        expected = df.iloc[: self.baseline_len]
        actual = df.iloc[self.baseline_len :]
        for c in cols:
            p = psi(actual[c].to_numpy(), expected[c].to_numpy(), n_bins=self.n_bins)
            events.append(DriftEvent(feature=c, psi=p, threshold=self.threshold, trigger_ts=df.index[-1]))
        return events

    def any_triggered(self, events: list[DriftEvent]) -> bool:
        return any(e.triggered for e in events)
