"""周线多尺度特征（第四轮特征工程，严格因果）。

在日线 bar 上捕捉**自然周**节奏（区别于 f_ret_acc_5 等跨周滚动窗口）：

1. ``f_week_ret_acc``  —— 周内累计收益 close_t / 本周首日 open - 1（周内动量进度）
2. ``f_week_pos``      —— 周内位置（本周已过交易日数 / 5，0~1）
3. ``f_week_vol``      —— 周内已实现波动率（本周截至 t 的日收益 std，expanding）
4. ``f_week_prev_ret`` —— 上一完整周收益（上周五 close / 上周一 open - 1，shift 一周）

因果性论证：
- 周内累计：week_open 是本周第一个交易日的 open（t 当日已知），close_t 当日已知 → 因果。
- 周内位置：已过天数由 t 自己决定 → 因果。
- 周内波动：expanding 窗口只含 ≤t 的收益 → 因果。
- 上周收益：上周完整周已结束，本周任何 bar 用上周值 → 因果（无任何未来信息）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _week_id(idx: pd.DatetimeIndex) -> pd.Series:
    """ISO 周标识：'2024-W32'（按周一为一周起点，与交易日自然周一致）。"""
    iso = idx.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


def add_weekly(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """在 df 上追加周线特征列（单标的、datetime 索引，含 open/close）。

    params 支持 ``{"include": [...]}`` 白名单。
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
    idx = out.index
    week = _week_id(idx)

    log_ret = np.log(close / close.shift(1)).fillna(0.0)

    # 1. 周内累计收益：close_t / 本周首日 open - 1
    if want("f_week_ret_acc"):
        week_open = open_.groupby(week).transform("first")
        out["f_week_ret_acc"] = (close / week_open.replace(0, np.nan) - 1.0).fillna(0.0)

    # 2. 周内位置：本周已过交易日数 / 5（clip 0~1）
    if want("f_week_pos"):
        cnt = out.groupby(week).cumcount() + 1
        out["f_week_pos"] = (cnt / 5.0).clip(0.0, 1.0)

    # 3. 周内已实现波动率（本周 expanding std，仅 ≤t）
    if want("f_week_vol"):
        wv = log_ret.groupby(week).expanding(min_periods=2).std()
        wv = wv.reset_index(level=0, drop=True)
        out["f_week_vol"] = wv.fillna(0.0)

    # 4. 上一完整周收益：每周五 close / 周一 open - 1，shift 一周后映射回日线
    if want("f_week_prev_ret"):
        week_last = close.groupby(week).last()
        week_first = open_.groupby(week).first()
        week_ret = (week_last / week_first.replace(0, np.nan) - 1.0).shift(1)
        week_map = week_ret.to_dict()
        out["f_week_prev_ret"] = week.map(week_map).fillna(0.0)

    return out


def weekly_columns(df: pd.DataFrame) -> list[str]:
    """返回 df 中以 ``f_week_`` 开头的周线特征列。"""
    return [c for c in df.columns if c.startswith("f_week_")]
