"""技术指标算子（严格因果，单标的、datetime 索引 DataFrame）。

所有滚动窗口均使用 ``shift(0)`` 式的 [t-w+1, t] 区间，绝不引入未来信息。
输入 df 含列 ``open/high/low/close/volume/adj_close``（部分可选）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd



def _safe_ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    return a.divide(b.replace(0, np.nan)).fillna(0.0)


def add_technical(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """在 df 上追加技术指标特征列，返回新的 DataFrame（含原始列 + 特征列）。"""
    params = params or {}
    out = df.copy()
    close = out["close"].astype(float)
    high = out.get("high", close).astype(float)
    low = out.get("low", close).astype(float)
    vol = out.get("volume", pd.Series(0.0, index=close.index)).astype(float)

    # 对数收益（使用 shift(1) 保证因果：t 时刻只知道 t-1 收盘）
    log_ret = np.log(close / close.shift(1)).fillna(0.0)
    out["f_ret_1"] = log_ret
    out["f_ret_acc_5"] = log_ret.rolling(5, min_periods=2).sum().fillna(0.0)
    out["f_ret_acc_20"] = log_ret.rolling(20, min_periods=5).sum().fillna(0.0)

    # 已实现波动率（滚动 std）
    out["f_vol_5"] = log_ret.rolling(5, min_periods=2).std().fillna(0.0)
    out["f_vol_20"] = log_ret.rolling(20, min_periods=5).std().fillna(0.0)

    # 动量：过去 N 根收益和（已含于 acc，这里补一个相对均线差）
    ma5 = close.rolling(5, min_periods=2).mean()
    ma20 = close.rolling(20, min_periods=5).mean()
    out["f_ma_spread"] = _safe_ratio(ma5 - ma20, ma20).fillna(0.0)

    # RSI（Wilder 近似）
    delta = close.diff().fillna(0.0)
    gain = delta.clip(lower=0.0).rolling(14, min_periods=5).mean()
    loss = (-delta.clip(upper=0.0)).rolling(14, min_periods=5).mean()
    rs = _safe_ratio(gain, loss)
    # loss==0（全涨）时 rs 退化 0 → RSI 应为 100，而非 0
    f_rsi = 100.0 - 100.0 / (1.0 + rs)
    out["f_rsi"] = f_rsi.mask(loss == 0, 100.0).fillna(50.0)

    # MACD（因果：使用截止 t 的 EWM）
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["f_macd"] = macd.fillna(0.0)
    out["f_macd_hist"] = (macd - signal).fillna(0.0)

    # 布林带宽度（相对）
    mid = close.rolling(20, min_periods=5).mean()
    sd = close.rolling(20, min_periods=5).std().fillna(0.0)
    out["f_boll_width"] = _safe_ratio(2.0 * sd, mid).fillna(0.0)

    # 量比（当前量 / 过去 20 均量）
    vma20 = vol.rolling(20, min_periods=5).mean()
    out["f_vol_ratio"] = _safe_ratio(vol, vma20).fillna(0.0)

    return out


def technical_columns(df: pd.DataFrame) -> list[str]:
    """返回 df 中以 ``f_`` 开头的技术特征列名。"""
    return [c for c in df.columns if c.startswith("f_")]
