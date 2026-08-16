"""迭代式特征（第二轮特征工程，严格因果）。

第一轮 18 特征中量价/波动率类主导（f_vol_5/f_vol_ratio/f_bar_dir/f_boll_width/f_vol_20
占特征重要性 top5），本模块补充以下正交信息维度（全部只用 [t-w+1, t] 历史）：

1. f_range_pos_20      —— 20 日高低区间内的收盘位置（0~1），趋势阶段定位
2. f_intraday_ret      —— 日内收益 close/open-1（与 f_gap 的隔夜收益互补，分解收益来源）
3. f_autocorr_20       —— 收益 lag-1 自相关（动量 vs 均值回复结构）
4. f_skew_20           —— 20 日收益偏度（分布形态）
5. f_kurt_20           —— 20 日收益峰度（肥尾程度）
6. f_streak_dir        —— 带方向的连续同向天数（趋势持续性，±计数）
7. f_vol_ratio_5_20    —— 短期/长期波动率结构比（vol_5 / vol_20，波动率锥）
8. f_ret_vol_corr_20   —— 20 日收益-成交量相关性（量价配合/背离）

实现约定（与 technical.py / microstructure.py 保持一致）：
- 输入：单标的、datetime 索引 DataFrame，含 open/high/low/close/volume（部分可选）
- 输出：追加 f_* 特征列；NaN/除零按现有风格填充（0.0 或中性值）
- 全部 rolling/shift 操作窗口含当前 bar，绝不引入未来
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_iterative(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """在 df 上追加迭代特征列，返回新的 DataFrame（含原始列 + 特征列）。

    params 支持 ``{"include": [特征名...]}`` 白名单（只计算列出的特征），
    ``include=None`` 表示计算全部。用于特征消融实验。
    """
    params = params or {}
    include = params.get("include")
    if include is not None:
        include = set(include)

    def want(name: str) -> bool:
        return include is None or name in include

    out = df.copy()
    close = out["close"].astype(float)
    open_ = out.get("open", close).astype(float)
    high = out.get("high", close).astype(float)
    low = out.get("low", close).astype(float)
    vol = out.get("volume", pd.Series(0.0, index=close.index)).astype(float)

    log_ret = np.log(close / close.shift(1)).fillna(0.0)

    # 1. 20 日高低区间内收盘位置（0~1），除零保护
    if want("f_range_pos_20"):
        hi20 = high.rolling(20, min_periods=5).max()
        lo20 = low.rolling(20, min_periods=5).min()
        rng20 = (hi20 - lo20).replace(0, np.nan)
        out["f_range_pos_20"] = ((close - lo20) / rng20).clip(0.0, 1.0).fillna(0.5)

    # 2. 日内收益（close/open-1），与隔夜收益（f_gap）互补
    if want("f_intraday_ret"):
        out["f_intraday_ret"] = (close / open_.replace(0, np.nan) - 1.0).fillna(0.0)

    # 3. 收益 lag-1 自相关（20 窗）：>0 动量延续，<0 均值回复
    if want("f_autocorr_20"):
        out["f_autocorr_20"] = (
            log_ret.rolling(20, min_periods=8).corr(log_ret.shift(1))
        ).fillna(0.0)

    # 4/5. 20 日收益偏度与峰度（分布形态）
    if want("f_skew_20"):
        out["f_skew_20"] = log_ret.rolling(20, min_periods=8).skew().fillna(0.0)
    if want("f_kurt_20"):
        out["f_kurt_20"] = log_ret.rolling(20, min_periods=8).kurt().fillna(0.0)

    # 6. 带方向连续同向天数：涨 +1 累计，跌 -1 累计，翻转归 0（因果）
    if want("f_streak_dir"):
        s = np.sign(close - open_).fillna(0.0).astype(int)
        grp = (s != s.shift(1)).cumsum()
        cnt = s.groupby(grp).cumcount() + 1
        out["f_streak_dir"] = (cnt * s).astype(float).fillna(0.0)

    # 7. 波动率结构比：短期/长期波动率（vol_5 / vol_20）
    if want("f_vol_ratio_5_20"):
        v5 = log_ret.rolling(5, min_periods=2).std().fillna(0.0)
        v20 = log_ret.rolling(20, min_periods=5).std().replace(0, np.nan).fillna(np.nan)
        out["f_vol_ratio_5_20"] = (v5 / v20).fillna(1.0)

    # 8. 收益-成交量滚动相关（20 窗）：量价配合度
    if want("f_ret_vol_corr_20"):
        out["f_ret_vol_corr_20"] = (
            log_ret.rolling(20, min_periods=8).corr(vol.rolling(20, min_periods=8).mean())
        ).fillna(0.0)

    return out


def iterative_columns(df: pd.DataFrame) -> list[str]:
    """返回 df 中本模块新增的迭代特征列。"""
    return [c for c in df.columns if c.startswith("f_")]
