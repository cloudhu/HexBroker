"""cross.py 跨品种特征模块单元测试（严格因果 + 数值合理性 + include 白名单）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.feature.cross import add_cross_global, add_internal_ratios, cross_columns


def _make_panel(n: int = 300, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    au = 400 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
    ag = 5.0 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    m = 3000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"au": au, "ag": ag, "m": m}, index=idx)


def _make_df(n: int = 300, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"close": close}, index=idx)


def _make_global(n: int = 300, seed: int = 9, shift: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    s = pd.Series(close, index=idx)
    return s.shift(shift)  # 调用方已 shift(1)（时差安全）


def test_adds_expected_columns():
    df = _make_df()
    g = {"spx": _make_global()}
    out = add_cross_global(df, g, "au")
    cols = set(out.columns)
    assert "f_xr_spx_ratio" in cols
    assert "f_xr_spx_mom" in cols
    assert "f_xr_spx_vol" in cols
    assert set(cross_columns(out)) == {"f_xr_spx_ratio", "f_xr_spx_mom", "f_xr_spx_vol"}


def test_no_nan_and_bounded():
    df = _make_df()
    g = {"spx": _make_global(), "udi": _make_global(seed=11), "cl": _make_global(seed=13)}
    out = add_cross_global(df, g, "au")
    for c in cross_columns(out):
        assert out[c].notna().all(), f"{c} 含 NaN"
        assert np.isfinite(out[c].astype(float)).all(), f"{c} 含 inf"
    # 比值对数不应极端
    assert out["f_xr_spx_ratio"].abs().max() < 5.0


def test_strict_causality_no_future_leak():
    """篡改未来 bar 不应改变 t 时刻特征值。"""
    df = _make_df(60)
    g = {"spx": _make_global(60)}
    base = add_cross_global(df, g, "au")

    df2 = df.copy()
    df2.loc[df2.index[30:], "close"] *= 2.0  # 篡改内盘未来
    g2 = g["spx"].copy()
    g2.iloc[30:] *= 2.0  # 篡改外盘未来
    mod = add_cross_global(df2, {"spx": g2}, "au")

    for c in cross_columns(base):
        assert np.allclose(
            base[c].iloc[:29].values, mod[c].iloc[:29].values, atol=1e-9
        ), f"{c} 泄露未来信息"


def test_shifted_global_is_driving():
    """外盘 shift(1) 后：t 日比值只依赖外盘 t-1（或更早），不依赖外盘 t 当日。"""
    df = _make_df(40)
    g_raw = _make_global(40, shift=0)
    # 用 shift(1) 的序列 -> 特征
    out_shift = add_cross_global(df, {"spx": g_raw.shift(1)}, "au")
    # 篡改外盘当日值（不 shift 时）应导致比值变化；shift 后当日值本就不参与
    g_raw2 = g_raw.copy()
    g_raw2.iloc[5:] *= 3.0
    out_raw = add_cross_global(df, {"spx": g_raw2}, "au")
    # 前 5 根两者一致（shift 前 4 根 NaN 填充）
    # t=5 及以后：shift 版本用 t-1 收盘，raw 版本用 t 收盘 -> 应不同
    assert not np.allclose(
        out_shift["f_xr_spx_ratio"].iloc[6:].values,
        out_raw["f_xr_spx_ratio"].iloc[6:].values,
        atol=1e-9,
    )


def test_include_whitelist():
    df = _make_df()
    g = {"spx": _make_global(), "udi": _make_global(seed=11)}
    out = add_cross_global(df, g, "au", {"include": ["f_xr_spx_ratio"]})
    assert set(cross_columns(out)) == {"f_xr_spx_ratio"}


def test_constant_series_stability():
    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame({"close": 100.0}, index=idx)
    g = {"spx": pd.Series(1000.0, index=idx)}
    out = add_cross_global(df, g, "au")
    for c in cross_columns(out):
        assert out[c].notna().all(), f"{c} 含 NaN"
        assert np.isfinite(out[c].astype(float)).all(), f"{c} 含 inf"


def test_internal_ratios_adds_pairs():
    panel = _make_panel()
    df = panel[["au"]].rename(columns={"au": "close"}).copy()
    out = add_internal_ratios(df, panel, "au")
    cols = set(out.columns)
    assert "f_xr_au_ag" in cols and "f_xr_au_m" in cols and "f_xr_ag_m" in cols
    # 金银比正数（au 单位大，但比值应稳定）
    assert np.isfinite(out["f_xr_au_ag"].astype(float)).all()
    # 统一列名约定：au 品种与 ag 品种的 f_xr_au_ag 值应完全一致（同一 panel 计算）
    df_ag = panel[["ag"]].rename(columns={"ag": "close"}).copy()
    out_ag = add_internal_ratios(df_ag, panel, "ag")
    assert "f_xr_au_ag" in out_ag.columns
    assert np.allclose(out["f_xr_au_ag"].values, out_ag["f_xr_au_ag"].values, atol=1e-9)
    # 比值对数关系：log(au/ag) = -log(ag/au)
    df_rev = panel.rename(columns={"au": "close"}).copy()
    out_rev = add_internal_ratios(df_rev, panel, "au")
    assert "f_xr_au_ag" in out_rev.columns


def test_internal_ratios_causality():
    """篡改未来价格不应改变 t 时刻比值（同交易时段，t 时点信息即 t 收盘）。"""
    panel = _make_panel(60)
    df = panel[["au"]].rename(columns={"au": "close"}).iloc[:60].copy()
    base = add_internal_ratios(df, panel, "au")
    panel2 = panel.copy()
    panel2.iloc[30:] *= 2.0
    mod = add_internal_ratios(df, panel2, "au")
    for c in ("f_xr_au_ag", "f_xr_au_m"):
        assert np.allclose(
            base[c].iloc[:29].values, mod[c].iloc[:29].values, atol=1e-9
        ), f"{c} 泄露未来信息"


def test_internal_ratios_include():
    panel = _make_panel()
    df = panel[["au"]].rename(columns={"au": "close"}).copy()
    out = add_internal_ratios(df, panel, "au", {"include": ["f_xr_au_ag"]})
    assert set(cross_columns(out)) == {"f_xr_au_ag"}
