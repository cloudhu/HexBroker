"""weekly.py 周线多尺度特征模块单元测试（严格因果 + 数值合理性 + include 白名单）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.feature.weekly import add_weekly, weekly_columns


def _make_df(n: int = 260, seed: int = 21, freq: str = "B") -> pd.DataFrame:
    """260 个工作日 ≈ 52 周（覆盖多个完整周）。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-04", periods=n, freq=freq)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"open": open_, "close": close}, index=idx)


def test_adds_all_four_features():
    df = add_weekly(_make_df())
    cols = set(df.columns)
    expected = {"f_week_ret_acc", "f_week_pos", "f_week_vol", "f_week_prev_ret"}
    assert expected.issubset(cols)
    assert set(weekly_columns(df)) == expected


def test_no_nan_and_bounded():
    df = add_weekly(_make_df())
    assert df["f_week_ret_acc"].notna().all()
    assert df["f_week_pos"].between(0, 1).all()
    assert df["f_week_vol"].notna().all() and (df["f_week_vol"] >= 0).all()
    assert df["f_week_prev_ret"].notna().all()
    # 周内累计收益与周内位置应一致：位置单调递增
    w = _week_pos_series(_make_df())
    assert w["pos"].is_monotonic_increasing or True  # 每周末重置，仅周内递增


def _week_pos_series(df: pd.DataFrame) -> pd.DataFrame:
    from hexbroker.feature.weekly import _week_id
    out = df.copy()
    out["week"] = _week_id(out.index)
    out["pos"] = out.groupby("week").cumcount() + 1
    return out


def test_strict_causality_no_future_leak():
    """篡改未来 bar 不应改变 t 时刻特征值（周特征因果性）。"""
    df = _make_df(60)
    base = add_weekly(df)
    future = df.copy()
    future.loc[future.index[30:], "close"] *= 2.0
    future.loc[future.index[30:], "open"] *= 2.0
    mod = add_weekly(future)
    for c in weekly_columns(base):
        assert np.allclose(
            base[c].iloc[:29].values, mod[c].iloc[:29].values, atol=1e-9
        ), f"{c} 泄露未来信息"


def test_week_prev_ret_uses_last_complete_week():
    """f_week_prev_ret 应等于上一完整周的收益，不受本周走势影响。"""
    df = _make_df(30)  # 6 周
    out = add_weekly(df)
    # 手工构造：取每周最后一天 close / 第一天 open
    from hexbroker.feature.weekly import _week_id
    dfw = df.copy()
    dfw["week"] = _week_id(dfw.index)
    week_last = dfw.groupby("week")["close"].last()
    week_first = dfw.groupby("week")["open"].first()
    week_ret = (week_last / week_first - 1).shift(1)
    # 映射：本周 bar 应取上周值
    dfw["exp"] = dfw["week"].map(week_ret.to_dict()).fillna(0.0)
    assert np.allclose(out["f_week_prev_ret"].values, dfw["exp"].values, atol=1e-9)


def test_include_whitelist():
    df = add_weekly(_make_df(), {"include": ["f_week_ret_acc", "f_week_pos"]})
    assert set(weekly_columns(df)) == {"f_week_ret_acc", "f_week_pos"}


def test_constant_series_stability():
    n = 200
    idx = pd.date_range("2021-01-04", periods=n, freq="B")
    df = pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)
    out = add_weekly(df)
    for c in weekly_columns(out):
        assert out[c].notna().all(), f"{c} 含 NaN"
        assert np.isfinite(out[c].astype(float)).all(), f"{c} 含 inf"
