"""主力合约拼接与换月复权（§1.1 D1）。

``ContractStitcher`` 负责：
1. 主力合约选取（``main_rule``：持仓量/成交量）；
2. 换月检测（基于 ``is_rollover`` 标记或价格跳空）；
3. **价差后向复权**：保留未复权 ``raw_close``（用于成本与涨跌停判定），
   输出连续 ``adj_close``，使换月处收益无跳空（|jump| < 3σ）。

复权方法：比例后向复权（backward adjustment）。在换月边界 i，令
``f[i] = raw_close[i] / raw_close[i-1]``，对所有 rollover 之后的边界做连乘，
``adj_close[j] = raw_close[j] * prod(f[k] for rollover k > j)``，从而保持最新价不变、
历史价平移对齐，换月处收益连续。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .schema import BarFrame


class ContractStitcher:
    """主力合约拼接 + 换月后向复权。"""

    def __init__(self, main_rule: str = "open_interest", adjust_method: str = "backward") -> None:
        self.main_rule = main_rule
        self.adjust_method = adjust_method

    def stitch(self, raw_bars: BarFrame) -> BarFrame:
        """对 BarFrame 逐品种做后向复权，返回带 ``adj_close`` 的 BarFrame。"""
        out_parts = []
        rollover_index = {}
        for sym in raw_bars.symbols:
            grp = raw_bars.by_symbol(sym).sort_index()
            raw = grp["raw_close"].to_numpy(dtype=float)
            # 换月标记：优先数据自带；否则以价格跳空检测
            if "is_rollover" in grp.columns:
                is_roll = grp["is_rollover"].to_numpy(dtype=bool)
            else:
                ret = np.diff(raw) / raw[:-1]
                is_roll = np.zeros(len(raw), dtype=bool)
                thr = 5 * np.nanstd(ret)
                is_roll[1:][np.abs(ret) > max(thr, 1e-3)] = True

            adj = self._backward_adjust(raw, is_roll)
            new = grp.copy()
            new["adj_close"] = adj
            out_parts.append(new)
            dates = grp.index.get_level_values("datetime")[is_roll]
            rollover_index[sym] = pd.DatetimeIndex(dates, name="datetime")

        out = pd.concat(out_parts)
        result = BarFrame(df=out, freq=raw_bars.freq, source=raw_bars.source)
        result.metadata["rollover_dates"] = rollover_index
        return result.validate()

    @staticmethod
    def _backward_adjust(raw: np.ndarray, is_roll: np.ndarray) -> np.ndarray:
        """比例后向复权核心。"""
        n = len(raw)
        if n == 0:
            return raw.copy()
        factors = np.ones(n)
        # 对每个换月边界 i（i>=1），向后累积因子
        cum = 1.0
        # 从后往前：对于 j，factor = prod(f[k] for rollover k > j)
        # 等价于：从右向左累积，遇到 rollover i 时乘上 f[i]
        running = 1.0
        for j in range(n - 1, -1, -1):
            # 先记录当前累积因子（不含本位置自身的 rollover 因子），再更新
            factors[j] = running
            if j >= 1 and is_roll[j]:
                prev = raw[j - 1]
                if prev != 0:
                    running *= raw[j] / prev
        return raw * factors

    def rollover_dates(self, stitched: BarFrame) -> dict[str, pd.DatetimeIndex]:
        """返回各品种换月日期列表。"""
        return stitched.metadata.get("rollover_dates", {})
