"""特征离散化 / Kronos 风格 tokenizer（无第三方依赖）。

把连续特征量化到固定区间的整数 token，供可选的 Kronos 分词器接口使用。
**严格因果**：``transform`` 对每一行只使用其自身及之前的历史数据计算分位数边界
（pandas ``expanding`` 窗口），因此第 i 行的 token 绝不依赖第 i 行之后的任何信息，
从根本上消除全样本 ``nanpercentile`` 带来的"未来函数"泄漏（防泄漏红线 L2）。
``fit`` 仅记录列名，不做任何全局统计量估计；真正的离散化在 ``transform`` 内完成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class FeatureTokenizer:
    """把特征矩阵离散成整数 token id（0..n_bins-1），并保留特殊 <PAD>/<MASK>。"""

    PAD_ID = 0
    MASK_ID = 1

    def __init__(self, n_bins: int = 256, columns: list[str] | None = None) -> None:
        if n_bins < 4:
            # 至少要有 PAD(0)/MASK(1) + 1 个有效分箱区间，否则所有特征都会退化为 MASK。
            raise ValueError("n_bins 必须 >= 4（需为 PAD/MASK 与至少 1 个有效分箱留出空间）")
        self.n_bins = int(n_bins)
        self.columns = columns
        self._cols: list[str] = []

    def fit(self, df: pd.DataFrame) -> "FeatureTokenizer":
        # 因果 tokenizer：不在此处估计任何跨样本统计量（避免未来函数）。
        # 仅记录列顺序，真正的分位边界在 transform 内按行 expanding 计算。
        self._cols = list(self.columns or df.columns)
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        cols = self.columns or list(df.columns)
        out = pd.DataFrame(index=df.index)
        # 去掉首尾，留出 PAD/MASK 的量化点
        q = np.linspace(0.0, 100.0, self.n_bins - 2)

        for c in cols:
            if c not in df.columns:
                out[c] = self.PAD_ID
                continue
            s = df[c].astype(float)
            T = len(s)
            # 每一行 i 的分位边界只由 [0..i] 历史估计 -> 严格因果
            edges_per_row = np.column_stack(
                [s.expanding().quantile(qq / 100.0).to_numpy() for qq in q]
            )  # shape (T, n_bins-2)
            vals = s.to_numpy()
            toks = np.full(T, self.PAD_ID, dtype=int)
            for i in range(T):
                e = edges_per_row[i]
                if np.isnan(e).any():
                    # 历史不足（例如首个窗口全 NaN）时保持 PAD
                    continue
                t = int(np.digitize(vals[i], e) + 2)  # +2 跳过 PAD/MASK
                toks[i] = int(min(max(t, 2), self.n_bins - 1))
            out[c] = toks
        return out

    def fit_transform(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.fit(df).transform(df)
