"""Kronos 数据 IO 适配（§3.2）。

把 ``FeatureFrame`` / ``BarFrame`` 转换为 Kronos 期望的分词序列（依赖 ``feature.tokenizer``）。
本模块不引入第三方依赖：当 Kronos 未安装时，仅提供序列化/反序列化工具，
真正的模型权重加载在 ``kronos_adapter`` 中做受控导入与降级。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..feature.tokenizer import FeatureTokenizer


class KronosDataIO:
    """为 Kronos 准备 token 序列。"""

    def __init__(self, n_bins: int = 256, columns: list[str] | None = None) -> None:
        self.tokenizer = FeatureTokenizer(n_bins=n_bins, columns=columns)

    def to_token_sequences(self, feature_frame: Any) -> dict[str, np.ndarray]:
        """逐标的把特征离散为 token 矩阵，返回 {symbol: (T, n_features) int}。"""
        out: dict[str, np.ndarray] = {}
        for sym in feature_frame.symbols:
            grp = feature_frame.by_symbol(sym)
            cols = self.tokenizer.columns or list(grp.columns)
            toks = self.tokenizer.fit_transform(grp[cols]).to_numpy(dtype=int)
            out[sym] = toks
        return out

    def context_window(self, tokens: np.ndarray, pos: int, max_context: int = 512) -> np.ndarray:
        """取 [pos-max_context, pos] 的上下文（因果截断）。"""
        lo = max(0, pos - max_context + 1)
        return tokens[lo : pos + 1]
