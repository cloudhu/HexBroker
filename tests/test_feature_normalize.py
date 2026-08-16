"""特征归一化零泄漏测试（§1.1 防泄漏红线 / §3.2）。

核心断言：任意 bar t 的归一化结果只依赖其历史，修改「未来」bar 不会改变历史归一化值。
"""

import numpy as np
import pandas as pd
import pytest

from hexbroker.feature.normalize import RollingNormalizer, rolling_zscore


def _rand_series(n: int, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.Series(rng.normal(size=n), index=idx, name="x")


def test_rolling_zscore_is_causal():
    """修改未来 bar 不应改变历史 bar 的归一化结果。"""
    n = 100
    s = _rand_series(n)
    base = rolling_zscore(s, window=20)
    # 篡改“未来”bar（位置 60，naive 不会用到 0..49 的窗口）
    perturbed = s.copy()
    perturbed.iloc[60] = perturbed.iloc[60] * 3 + 100.0
    pert_norm = rolling_zscore(perturbed, window=20)
    # 前 50 个 bar 完全不受未来篡改影响
    assert np.allclose(base.iloc[:50].values, pert_norm.iloc[:50].values, atol=1e-9)


def test_rolling_zscore_mean_zero_within_window():
    """归一化后，窗口内均值≈0、标准差≈1（统计性质正确）。"""
    s = _rand_series(300, seed=3)
    z = rolling_zscore(s, window=30, min_periods=30)
    z = z.dropna()
    assert abs(z.mean()) < 0.05
    assert abs(z.std() - 1.0) < 0.1


def test_normalizer_assert_no_leakage_helper():
    """RollingNormalizer.assert_no_leakage 必须通过。"""
    s = _rand_series(200, seed=5)
    df = s.to_frame()
    assert RollingNormalizer.assert_no_leakage(df, window=30) is True


def test_fit_transform_preserves_index():
    """fit_transform 不破坏索引与形状。"""
    s = _rand_series(120, seed=7)
    df = s.to_frame()
    out = RollingNormalizer(window=20).fit_transform(df)
    assert out.shape == df.shape
    assert list(out.index) == list(df.index)
    assert out.isna().sum().sum() == 0
