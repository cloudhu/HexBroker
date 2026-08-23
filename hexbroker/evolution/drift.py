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


def psi(
    actual: np.ndarray,
    expected: np.ndarray,
    n_bins: int = 10,
    floor: float = 5e-3,
) -> float:
    """计算单特征 PSI。

    分箱按 expected 的分位数等宽切分；actual 落入同箱后比较占比。
    V3 修复：① 过滤非有限值；② 分箱去重防止退化（quantile 输出重复边界）；
    ③ 占比钳制到 ``[floor, 1]`` 防止空箱导致 ``log`` 爆炸（实测 8.39 → 收敛到合理区间）。
    """
    a = np.asarray(actual, dtype=float).ravel()
    e = np.asarray(expected, dtype=float).ravel()
    # 两窗长度可能不同（baseline vs 当前窗）；仅比较有效有限值，按各自长度处理
    a = a[np.isfinite(a)]
    e = e[np.isfinite(e)]
    if a.size < 2 or e.size < 2:
        return 0.0
    bins = np.unique(np.quantile(e, np.linspace(0.0, 1.0, n_bins + 1)))
    if len(bins) < 3:
        # 退化：baseline 分位数不足 → 退化为单箱比较（避免空箱爆炸）
        pe = float((e.size > 0))
        pa = float((a.size > 0))
        lo = min(pe, pa, 1.0 - floor)
        pe = max(lo, floor)
        pa = max(lo, floor)
        return float(abs(pa - pe) * np.log(pa / pe)) if pe > 0 and pa > 0 else 0.0
    bins[0] = -np.inf
    bins[-1] = np.inf
    p_e, _ = np.histogram(e, bins=bins)
    p_a, _ = np.histogram(a, bins=bins)
    p_e = p_e / e.size
    p_a = p_a / a.size
    # 空箱/极小占比 → 钳制，防 log 爆炸（V3 修复核心）
    p_e = np.clip(p_e, floor, 1.0)
    p_a = np.clip(p_a, floor, 1.0)
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
