"""P8-2 基本面（基差）特征模块测试：对齐因果性、缺失处理、管线注入。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.feature.fundamental import (
    DEFAULT_MIN_PERIODS,
    DEFAULT_WINDOW,
    _rolling_zscore,
    add_fundamental,
    fundamental_columns,
)


def _make_kline(n: int = 400, start: str = "2019-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"close": np.linspace(100.0, 150.0, n)}, index=idx)


def _make_basis(n: int = 300, start: str = "2019-04-01", seed: int = 1) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D")
    rng = np.random.RandomState(seed)
    br = rng.normal(0.0, 0.5, n)
    return pd.DataFrame({"basis_ratio": br, "basis": br * 100.0}, index=idx)


# ---------------------------------------------------------------------------
# 1. 特征生成
# ---------------------------------------------------------------------------
def test_add_fundamental_generates_all_features():
    kline = _make_kline()
    basis = _make_basis()
    out = add_fundamental(kline, basis, "au", {"window": 252, "min_periods": 60})
    assert fundamental_columns(out) == [
        "f_basis_ratio", "f_basis_ratio_rank", "f_basis_ratio_z", "f_basis",
    ]
    assert len(out) == len(kline)
    # rank 形态 = 0~1 分位（引擎 B 已验证形态）
    rank = out["f_basis_ratio_rank"].dropna()
    assert rank.min() >= 0.0 and rank.max() <= 1.0
    # 原始基差率对齐：早期（无基本面数据）为 NaN
    assert out["f_basis_ratio"].iloc[:60].isna().all()
    assert out["f_basis_ratio"].notna().sum() > 0


def test_add_fundamental_series_input():
    kline = _make_kline()
    basis = _make_basis()
    out = add_fundamental(kline, basis["basis_ratio"], "au")
    assert out["f_basis_ratio"].notna().sum() > 0
    assert out["f_basis"].isna().all()  # Series 输入无绝对基差


def test_add_fundamental_none_input_all_nan():
    kline = _make_kline()
    out = add_fundamental(kline, None, "au")
    for c in ["f_basis_ratio", "f_basis_ratio_rank", "f_basis_ratio_z", "f_basis"]:
        assert out[c].isna().all()


def test_include_basis_false():
    kline = _make_kline()
    basis = _make_basis()
    out = add_fundamental(kline, basis, "au", {"include_basis": False})
    assert "f_basis" not in out.columns
    assert "f_basis_ratio" in out.columns


def test_missing_basis_ratio_column_raises():
    kline = _make_kline()
    bad = pd.DataFrame({"spot": np.zeros(10)}, index=pd.date_range("2019-04-01", periods=10, freq="D"))
    with pytest.raises(ValueError, match="basis_ratio"):
        add_fundamental(kline, bad, "au")


# ---------------------------------------------------------------------------
# 2. 严格因果（QA 审查重点）
# ---------------------------------------------------------------------------
def test_no_lookahead_alignment():
    """ffill 对齐：只取基本面日期 <= t 的最新值；早于首个基本面日期 → NaN。"""
    kline = _make_kline()
    basis = _make_basis(start="2019-04-01")
    out = add_fundamental(kline, basis, "au")
    # 首个基本面日期前全部 NaN
    first = basis.index[0]
    assert out.loc[kline.index < first, "f_basis_ratio"].isna().all()
    # 首个基本面日期当天起有值（当日及以前最新值）
    assert out.loc[first, "f_basis_ratio"] == basis["basis_ratio"].iloc[0]
    # 对齐值等于该日及以前最后一个基本面观测
    d = basis.index[50]
    assert out.loc[d, "f_basis_ratio"] == basis["basis_ratio"].iloc[50]


def test_no_lookahead_rolling():
    """滚动分位/z 只依赖过去观测：改变 t 之后的基本面值不影响 t 及以前特征。"""
    kline = _make_kline()
    basis = _make_basis()
    out1 = add_fundamental(kline, basis, "au", {"window": 252, "min_periods": 60})
    basis2 = basis.copy()
    d_perturb = basis.index[100]
    basis2.loc[d_perturb, "basis_ratio"] = 99.9
    out2 = add_fundamental(kline, basis2, "au", {"window": 252, "min_periods": 60})
    past = kline.index < d_perturb
    for c in ["f_basis_ratio", "f_basis_ratio_rank", "f_basis_ratio_z", "f_basis"]:
        assert np.allclose(
            out1.loc[past, c].values, out2.loc[past, c].values, equal_nan=True
        ), f"{c} 在过去日应不受未来基本面影响"


def test_rolling_zscore_causal():
    s = pd.Series(np.random.RandomState(0).normal(0, 1, 500))
    z = _rolling_zscore(s, 100, 20)
    # 前 19 个观测：min_periods 未达到 → NaN
    assert z.iloc[:19].isna().all()
    assert z.iloc[20:].notna().all()


# ---------------------------------------------------------------------------
# 3. 管线注入（FeaturePipeline / build_features）
# ---------------------------------------------------------------------------
def test_pipeline_fundamental_injection():
    from _helpers import fast_cfg
    from hexbroker.data.sources.synthetic_source import SyntheticSource
    from hexbroker.feature import build_features

    cfg = fast_cfg(n_bars=400)
    cfg.data.symbols = ["SHFE.cu"]
    cfg.feature.transformers = [
        "technical", "microstructure", "iterative", "cross", "normalize", "fundamental",
    ]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": ["spx"]}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}

    bars = SyntheticSource(n_bars=400, seed=7).fetch_bars(["SHFE.cu"], freq="1d")
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    spx = pd.Series(
        np.linspace(3000, 4000, len(inner_dates) + 5),
        index=pd.date_range(inner_dates[0] - pd.Timedelta(days=5),
                            periods=len(inner_dates) + 5, freq="D"),
    )
    gc = {"spx": spx.shift(1).reindex(inner_dates).ffill()}
    fund = {
        "cu": _make_basis(start=str(inner_dates[0] + pd.Timedelta(days=30))),
    }
    ff = build_features(bars, cfg, global_close=gc, fundamental_data=fund)
    assert {"f_basis_ratio", "f_basis_ratio_rank", "f_basis_ratio_z", "f_basis"} <= set(ff.df.columns)
    # 基差特征不参与滚动 z-score 标准化（rank 保持 0~1 形态）
    au_rank = ff.df.loc["SHFE.cu", "f_basis_ratio_rank"].dropna()
    assert au_rank.min() >= 0.0 and au_rank.max() <= 1.0
    # 技术特征仍被标准化（z 尺度）
    assert abs(ff.df.loc["SHFE.cu", "f_ret_1"].std() - 1.0) < 0.2


def test_pipeline_fundamental_fail_fast():
    from _helpers import fast_cfg
    from hexbroker.feature import FeaturePipeline

    cfg = fast_cfg(n_bars=100)
    cfg.feature.transformers = ["fundamental"]
    with pytest.raises(ValueError, match="fundamental"):
        FeaturePipeline(cfg)


def test_pipeline_default_not_enabled():
    """默认 transformers 不含 fundamental：不注入数据也正常（向后兼容）。"""
    from _helpers import fast_cfg
    from hexbroker.data.sources.synthetic_source import SyntheticSource
    from hexbroker.feature import build_features

    cfg = fast_cfg(n_bars=200)
    cfg.data.symbols = ["SHFE.cu"]
    bars = SyntheticSource(n_bars=200, seed=7).fetch_bars(["SHFE.cu"], freq="1d")
    ff = build_features(bars, cfg)  # 默认 transformers: technical/microstructure/normalize
    assert not any(c.startswith("f_basis") for c in ff.df.columns)
