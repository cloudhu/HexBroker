"""数据清洗（§1.1 D1）：涨跌停标记、停牌/无成交 bar、极值 winsorize、缺失前向填充。

MVP 采用通用涨跌停判定：当日涨跌达到 ``limit_pct``（默认 4% 商品期货）且
close==high（涨停）/ close==low（跌停）视为封板。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..constants import LIMIT_DOWN, LIMIT_UP
from .schema import BarFrame


class Cleaner:
    """数据清洗器。"""

    def __init__(
        self,
        limit_pct: float = 0.04,
        winsor_quantile: float = 0.001,
        ffill_limit: int = 1,
    ) -> None:
        self.limit_pct = limit_pct
        self.winsor_quantile = winsor_quantile
        self.ffill_limit = ffill_limit

    def clean(self, bars: BarFrame) -> BarFrame:
        parts = []
        for sym in bars.symbols:
            grp = bars.by_symbol(sym).sort_index().copy()
            # 停牌/无成交：volume==0 且跳过
            vol = grp["volume"].to_numpy(dtype=float)
            valid = vol > 0
            grp["_no_trade"] = ~valid

            prev_close = grp["close"].shift(1)
            ret = (grp["close"] - prev_close) / prev_close.replace(0, np.nan)
            grp[LIMIT_UP] = (ret >= self.limit_pct * 0.99) & (
                grp["close"] >= grp["high"] - 1e-9
            )
            grp[LIMIT_DOWN] = (ret <= -self.limit_pct * 0.99) & (
                grp["close"] <= grp["low"] + 1e-9
            )

            # winsorize 极值（仅用截至 t 的历史分位裁剪：滚动 expanding，避免未来函数）
            # 注：用全样本分位会引用未来 bar，既泄漏又抹掉真实极值，判据失效。
            for col in ["open", "high", "low", "close", "adj_close"]:
                s = grp[col].astype(float)
                rolling_lo = s.expanding().quantile(self.winsor_quantile)
                rolling_hi = s.expanding().quantile(1 - self.winsor_quantile)
                grp[col] = s.clip(rolling_lo, rolling_hi)

            # 缺失前向填充 ≤ ffill_limit（契约 §8.4）
            fill_cols = ["open", "high", "low", "close", "adj_close", "raw_close",
                         "volume", "amount", "open_interest"]
            grp[fill_cols] = grp[fill_cols].ffill(limit=self.ffill_limit)
            parts.append(grp)
        out = pd.concat(parts)
        return BarFrame(df=out, freq=bars.freq, source=bars.source)
