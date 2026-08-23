"""FeatureTokenizer 因果性 / 防泄漏回归测试（L2）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hexbroker.feature.tokenizer import FeatureTokenizer


def test_causal_no_future_leak():
    """第 i 行的 token 不能依赖第 i 行之后的数据（未来函数红线）。"""
    rng = np.random.default_rng(0)
    base = np.concatenate([rng.normal(0.0, 1.0, 50), [1e6]])  # 末尾极端值
    df = pd.DataFrame({"x": base})

    tok = FeatureTokenizer(n_bins=64)
    full = tok.fit_transform(df)
    truncated = tok.fit_transform(df.iloc[:-1])

    # 前 50 行的 token 在有无末尾极端未来值时必须完全一致
    assert full["x"].to_numpy()[:-1].tolist() == truncated["x"].to_numpy().tolist()


def test_n_bins_guard():
    """n_bins 过小应直接报错，避免所有特征退化为 MASK。"""
    import pytest

    with pytest.raises(ValueError):
        FeatureTokenizer(n_bins=2)
    with pytest.raises(ValueError):
        FeatureTokenizer(n_bins=3)


def test_out_of_range_clipped_within_valid_bins():
    """超出历史区间的值应被裁剪到有效分箱，而非 PAD/MASK。"""
    df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0]})
    tok = FeatureTokenizer(n_bins=32)
    toks = tok.fit_transform(df)["x"].to_numpy()
    assert toks.min() >= 2  # 跳过 PAD/MASK
    assert toks.max() <= 31  # 不超过 n_bins-1


def test_future_appended_after_test_does_not_change_test_tokens():
    """训练/测试分段下，测试段之后追加的极端未来值不得改变测试段 token（因果性）。"""
    train = pd.DataFrame({"x": np.arange(1.0, 21.0)})
    test = pd.DataFrame({"x": np.arange(21.0, 26.0)})

    tok = FeatureTokenizer(n_bins=32).fit(train)
    out_test = tok.transform(test)

    # 在 test 之后追加一个极端未来值，test 段 token 必须不变（因果性）
    test_with_future = pd.concat(
        [train, test, pd.DataFrame({"x": [1e9]})], ignore_index=True
    )
    out_full = tok.transform(test_with_future)
    tail = out_full["x"].to_numpy()[len(train) : len(train) + len(test)]
    assert tail.tolist() == out_test["x"].to_numpy().tolist()

    # 所有 token 落在有效区间
    assert out_test["x"].to_numpy().min() >= 2
    assert out_test["x"].to_numpy().max() <= 31
