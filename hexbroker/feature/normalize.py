"""滚动标准化（严格因果，零泄漏）。

核心承诺：**任意 bar t 的归一化值只依赖 [t-window+1, t] 的已知历史**，
既不偷看未来，也不跨训练/测试共享全局统计量（避免分布泄漏）。
这是回测防泄漏红线的关键一环。

提供 ``RollingNormalizer``（fit/transform 风格，便于流水线组合）与
纯函数 ``rolling_zscore``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_zscore(series: pd.Series, window: int, min_periods: int = 5, eps: float = 1e-8) -> pd.Series:
    """对单列做严格因果滚动 z-score。

    返回 (x - rolling_mean) / (rolling_std + eps)，长度与输入一致。
    """
    s = series.astype(float)
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0)
    std = std.fillna(0.0)
    out = (s - mean) / (std + eps)
    return out.fillna(0.0)


class RollingNormalizer:
    """按列滚动标准化（fit 仅记录窗口与待处理列，transform 全程因果）。"""

    def __init__(self, window: int = 120, columns: list[str] | None = None, min_periods: int = 5) -> None:
        self.window = int(window)
        self.min_periods = int(min_periods)
        self.columns = columns  # None 表示所有数值列

    def fit(self, df: pd.DataFrame) -> "RollingNormalizer":
        if self.columns is None:
            self.columns = [c for c in df.columns if df[c].dtype.kind in "biufc"]
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        cols = [c for c in self.columns if c in out.columns] if self.columns else list(out.columns)
        for c in cols:
            out[c] = rolling_zscore(out[c], self.window, self.min_periods)
        return out

    def fit_transform(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.fit(df).transform(df)

    # ---- 零泄漏自检：修改 bar t+1 不应改变 bar t 的归一化结果 ----
    @staticmethod
    def assert_no_leakage(df: pd.DataFrame, window: int = 120) -> bool:
        """静态校验 rolled-zscore 不依赖未来。返回 True 表示通过。"""
        col = df.columns[0]
        s = df[col].astype(float)
        base = rolling_zscore(s, window)
        perturbed = s.copy()
        # 改变最后一个 bar 之后的“未来”值（这里指序列末尾追加一个未来点）
        future = s.tolist() + [s.iloc[-1] * 2 + 1.0]
        future_series = pd.Series(future, index=list(s.index) + [s.index[-1]])
        future_norm = rolling_zscore(future_series, window).iloc[: len(s)]
        return bool(np.allclose(base.values, future_norm.values, atol=1e-9))
