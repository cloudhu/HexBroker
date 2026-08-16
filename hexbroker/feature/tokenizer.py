"""特征离散化 / Kronos 风格 tokenizer（无第三方依赖）。

把连续特征量化到固定区间的整数 token，供可选的 Kronos 分词器接口使用。
默认采用按列分位数分箱（训练时 fit，transform 时严格因果——只用历史分位数）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class FeatureTokenizer:
    """把特征矩阵离散成整数 token id（0..n_bins-1），并保留特殊 <PAD>/<MASK>。"""

    PAD_ID = 0
    MASK_ID = 1

    def __init__(self, n_bins: int = 256, columns: list[str] | None = None) -> None:
        self.n_bins = int(n_bins)
        self.columns = columns
        self._quantiles: dict[str, np.ndarray] = {}

    def fit(self, df: pd.DataFrame) -> "FeatureTokenizer":
        cols = self.columns or list(df.columns)
        for c in cols:
            if c not in df.columns:
                continue
            q = np.linspace(0, 100, self.n_bins - 2)  # 去掉首尾，留出 PAD/MASK
            self._quantiles[c] = np.nanpercentile(df[c].astype(float).values, q)
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        cols = self.columns or list(df.columns)
        out = pd.DataFrame(index=df.index)
        for c in cols:
            if c not in df.columns or c not in self._quantiles:
                out[c] = self.PAD_ID
                continue
            edges = self._quantiles[c]
            tokens = np.digitize(df[c].astype(float).values, edges) + 2  # +2 跳过 PAD/MASK
            tokens = np.clip(tokens, 2, self.n_bins - 1)
            out[c] = tokens
        return out

    def fit_transform(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.fit(df).transform(df)
