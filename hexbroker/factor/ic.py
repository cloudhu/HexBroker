"""因子 IC 档案（§3 / P1-6）。

``FactorICArchive`` 计算滚动 OOS RankIC（因子值与远期收益的 Spearman 秩相关），
缓存到 sidecar（原子写 CSV，复用 data/manifest 的 tmp+replace 模式）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


class FactorICArchive:
    """滚动 OOS RankIC 档案（sidecar 缓存）。"""

    def __init__(self, cache_dir: str = "artifacts/factor_ic") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _column_for(factor_name: str) -> str:
        return factor_name if factor_name.startswith("f_") else f"f_{factor_name}"

    def _path(self, symbol: str, factor_name: str) -> Path:
        safe = f"{factor_name}__{symbol}".replace("/", "_")
        return self.cache_dir / f"{safe}.csv"

    def _factor_series(self, factor_name: str, feat: Any) -> pd.Series:
        if isinstance(feat, pd.Series):
            return feat.astype(float)
        col = self._column_for(factor_name)
        if col in feat.columns:
            return feat[col].astype(float)
        if factor_name in feat.columns:
            return feat[factor_name].astype(float)
        raise KeyError(f"feat 中找不到因子列：{col} / {factor_name}")

    def _save(self, symbol: str, factor_name: str, series: pd.Series) -> None:
        """原子写：tmp + os.replace（避免半写文件）。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(symbol, factor_name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        series.to_frame().to_csv(tmp)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # 主接口
    # ------------------------------------------------------------------
    def rolling_ic(
        self,
        factor_name: str,
        symbol: str,
        feat: Any,
        fwd_ret: pd.Series,
        window: int = 63,
    ) -> pd.Series:
        """滚动 OOS RankIC：逐窗口 [t-w+1, t] 计算因子值与 fwd_ret 的 Spearman 秩相关。

        参数
        ----
        feat     : 含 ``f_{factor_name}`` 列的单标的 DataFrame，或该因子的 Series。
        fwd_ret  : 远期收益 Series（datetime-indexed，与 feat 对齐）。
        window   : 滚动窗口长度。

        返回
        ----
        以 datetime 为索引的 RankIC Series（前 ``window-1`` 个点为 NaN）。
        """
        from scipy.stats import spearmanr  # 懒加载：仅在计算 IC 时引入 scipy

        fac = self._factor_series(factor_name, feat)
        common = fac.index.intersection(fwd_ret.index)
        fac = fac.reindex(common).astype(float)
        ret = fwd_ret.reindex(common).astype(float)
        idx = list(common)
        n = len(idx)
        vals: dict[Any, float] = {}
        for i in range(n):
            if i < window:
                vals[idx[i]] = np.nan
                continue
            f = fac.iloc[i - window : i]
            r = ret.iloc[i - window : i]
            if f.notna().sum() < 2 or r.notna().sum() < 2:
                vals[idx[i]] = np.nan
                continue
            ic, _ = spearmanr(f.values, r.values)
            vals[idx[i]] = float(ic)
        series = pd.Series(vals, name=f"ic_{factor_name}")
        self._save(symbol, factor_name, series)
        return series

    def cached(self, factor_name: str, symbol: str) -> Optional[pd.Series]:
        """读取已缓存的 RankIC；无缓存返回 None。"""
        path = self._path(symbol, factor_name)
        if path.exists():
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df.iloc[:, 0].rename(f"ic_{factor_name}")
        return None
