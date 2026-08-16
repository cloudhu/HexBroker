"""微观结构代理特征（严格因果）。

日线只有 OHLCV，但仍可构造不窥探未来的微观结构代理：
- 日内高-低区间、实体（收盘-开盘）占比
- 上下影线长度
- 量能相对强度（量在涨/跌日中的分布）
- 跳空（当前开盘相对前收）
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_microstructure(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上追加微观结构特征列。"""
    out = df.copy()
    open_ = out.get("open", out["close"]).astype(float)
    high = out.get("high", out["close"]).astype(float)
    low = out.get("low", out["close"]).astype(float)
    close = out["close"].astype(float)
    vol = out.get("volume", pd.Series(0.0, index=close.index)).astype(float)

    rng = (high - low).replace(0, np.nan)
    body = (close - open_).abs()
    out["f_intraday_range"] = (rng / close).fillna(0.0)
    out["f_body_ratio"] = (body / rng).fillna(0.0)
    out["f_upper_shadow"] = ((high - np.maximum(close, open_)) / rng).fillna(0.0)
    out["f_lower_shadow"] = ((np.minimum(close, open_) - low) / rng).fillna(0.0)

    # 当日方向（收盘相对开盘）
    out["f_bar_dir"] = (np.sign(close - open_)).fillna(0.0)

    # 跳空：开盘相对前收
    gap = (open_ - close.shift(1)) / close.shift(1)
    out["f_gap"] = gap.fillna(0.0)

    # 量在上涨日占比（滚动 20）
    up_day = (close > close.shift(1)).astype(float)
    up_vol = (up_day * vol).rolling(20, min_periods=5).sum()
    tot_vol = vol.rolling(20, min_periods=5).sum().replace(0, np.nan)
    out["f_up_vol_share"] = (up_vol / tot_vol).fillna(0.5)

    return out
