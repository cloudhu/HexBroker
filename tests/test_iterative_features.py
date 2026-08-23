"""iterative.py 特征模块单元测试（严格因果 + 数值合理性 + include 白名单）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hexbroker.feature.iterative import add_iterative, iterative_columns


def _make_df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    volume = rng.integers(1000, 100000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_adds_all_eight_features():
    df = add_iterative(_make_df())
    cols = [c for c in df.columns if c.startswith("f_")]
    expected = {
        "f_range_pos_20", "f_intraday_ret", "f_autocorr_20",
        "f_skew_20", "f_kurt_20", "f_streak_dir",
        "f_vol_ratio_5_20", "f_ret_vol_corr_20",
    }
    assert expected.issubset(set(cols))
    # 与 technical_columns 一致性（iterative_columns 返回 f_* 列）
    assert set(iterative_columns(df)) == set(cols)


def test_no_nan_and_bounded():
    df = add_iterative(_make_df())
    feats = [c for c in df.columns if c.startswith("f_")]
    for c in feats:
        assert df[c].notna().all(), f"{c} 含 NaN"
    # 区间位置在 [0,1]
    assert df["f_range_pos_20"].between(0, 1).all()
    # 日内收益/自相关/偏度/峰度/量价相关不应有极端值
    assert df["f_intraday_ret"].abs().max() < 0.5
    assert df["f_autocorr_20"].abs().max() <= 1.0
    assert df["f_ret_vol_corr_20"].abs().max() <= 1.0
    # 波动率结构比为正
    assert (df["f_vol_ratio_5_20"] > 0).all()


def test_strict_causality_no_future_leak():
    """修改 bar t+1 之后的未来价格，不应改变 t 时刻的特征值。"""
    df = _make_df(60)
    base = add_iterative(df)
    future = df.copy()
    future.loc[future.index[30:], "close"] *= 2.0  # 篡改 t>=30 的收盘
    future.loc[future.index[30:], "open"] *= 2.0
    future.loc[future.index[30:], "high"] *= 2.0
    future.loc[future.index[30:], "low"] *= 2.0
    future.loc[future.index[30:], "volume"] *= 2.0
    mod = add_iterative(future)
    feats = [c for c in base.columns if c.startswith("f_")]
    for c in feats:
        # 前 29 根不应受未来影响
        assert np.allclose(
            base[c].iloc[:29].values, mod[c].iloc[:29].values, atol=1e-9
        ), f"{c} 泄露未来信息"


def test_include_whitelist():
    df = add_iterative(_make_df(), {"include": ["f_range_pos_20", "f_skew_20"]})
    cols = [c for c in df.columns if c.startswith("f_")]
    assert set(cols) == {"f_range_pos_20", "f_skew_20"}


def test_empty_include_means_all():
    df_all = add_iterative(_make_df(), {})
    df_none = add_iterative(_make_df(), {"include": []})
    assert "f_range_pos_20" in df_all.columns
    assert "f_range_pos_20" not in df_none.columns
    # 空 include 不应抛错且不添加任何特征
    assert not any(c.startswith("f_") for c in df_none.columns)


def test_constant_series_stability():
    """极端输入（价格恒定、量恒定）不产生 NaN/除零。"""
    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 5000.0,
        },
        index=idx,
    )
    out = add_iterative(df)
    for c in out.columns:
        if c.startswith("f_"):
            assert out[c].notna().all(), f"{c} 含 NaN"
            assert np.isfinite(out[c].astype(float)).all(), f"{c} 含 inf"
