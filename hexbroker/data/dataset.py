"""数据集（§2.2 L1）：表格 ``BarDataset`` 与自回归滑窗 ``WindowDataset``，
以及标签生成 ``make_labels``。

``WindowDataset`` 返回 numpy 数组（不强制依赖 torch，CPU 沙箱友好）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class WindowSample:
    """单条自回归样本。"""

    x: np.ndarray  # (lookback, n_features)
    y: float  # 标签（未来收益或方向）
    ts: object  # 样本对应时间戳


class BarDataset:
    """表格数据集：封装按 symbol 切分的 BarFrame。"""

    def __init__(self, barframe) -> None:
        self.bf = barframe

    def features(self, symbol: str) -> pd.DataFrame:
        return self.bf.by_symbol(symbol)


def make_labels(
    close: np.ndarray,
    horizon: int = 5,
    kind: str = "return",
) -> np.ndarray:
    """生成标签。

    kind="return"：未来 ``horizon`` 步对数收益（回归）。
    kind="direction"：未来收益符号（分类，{-1, 0, 1}）。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    fut = np.full(n, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.log(close[horizon:] / close[:-horizon])
    fut[:-horizon] = ret
    if kind == "direction":
        # 仅对未来 horizon 步内有定义的收益取符号；尾部 NaN 保持 NaN（不伪造标签）
        fut = np.where(fut > 0, 1.0, np.where(fut < 0, -1.0, np.nan))
    return fut


class WindowDataset:
    """自回归滑窗数据集：给定特征矩阵 (T, F) 与标签 (T,)，按 lookback/horizon 切窗。"""

    def __init__(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
        lookback: int = 30,
        horizon: int = 5,
    ) -> None:
        if len(features) != len(labels):
            raise ValueError("features 与 labels 长度必须一致")
        self.features = np.asarray(features, dtype=float)
        self.labels = np.asarray(labels, dtype=float)
        self.timestamps = timestamps
        self.lookback = lookback
        self.horizon = horizon

    def __len__(self) -> int:
        # 最后一个有效标签位于 len-horizon-1；样本 i 的目标为 labels[i+lookback]，
        # 故 i 最大为 len-horizon-lookback-1 → 长度 = len-lookback-horizon（无 +1，避免尾部无效标签）
        return max(0, len(self.labels) - self.lookback - self.horizon)

    def __getitem__(self, i: int) -> WindowSample:
        if i < 0 or i >= len(self):
            raise IndexError(i)
        x = self.features[i : i + self.lookback]
        y = self.labels[i + self.lookback]  # 窗口末端时刻的标签
        ts = self.timestamps[i + self.lookback] if self.timestamps is not None else i
        return WindowSample(x=x, y=y, ts=ts)

    def to_numpy(self) -> tuple[np.ndarray, np.ndarray]:
        """返回 (X, y) 矩阵，便于直接喂给 sklearn/numpy 模型。"""
        X, Y = [], []
        for i in range(len(self)):
            s = self[i]
            X.append(s.x)
            Y.append(s.y)
        return np.stack(X), np.asarray(Y)
