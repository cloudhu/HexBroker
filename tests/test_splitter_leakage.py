"""T01 WalkForwardSplitter 防泄漏测试。

红线：任一 fold 的 ``train.max_position + purge < test.min_position``。
故意构造泄漏场景时，``assert_no_leakage`` 必须抛出 ``HexLeakageError``（单测失败）。
"""

import pandas as pd
import pytest

from hexbroker.data.splitter import (
    Fold,
    WalkForwardSplitter,
    assert_no_leakage,
)
from hexbroker import HexLeakageError


def _index(n=1000):
    return pd.date_range("2018-01-01", periods=n, freq="D")


def test_no_leakage_passes():
    idx = _index(1000)
    sp = WalkForwardSplitter(train_len=250, test_len=60, purge=5, embargo=2)
    folds = sp.split(idx)
    assert len(folds) >= 3
    sp.assert_no_leakage(folds)  # 不抛异常


def test_train_test_non_overlapping_with_purge():
    idx = _index(1000)
    sp = WalkForwardSplitter(train_len=250, test_len=60, purge=5, embargo=2)
    folds = sp.split(idx)
    for f in folds:
        # 不变量：train.max_pos + purge < test.min_pos
        assert f.train_max_pos + sp.purge < f.test_min_pos


def test_purge_and_embargo_gap_enforced():
    idx = _index(1000)
    sp = WalkForwardSplitter(train_len=250, test_len=60, purge=5, embargo=2)
    folds = sp.split(idx)
    f = folds[0]
    gap = f.test_start - f.train_end
    assert gap == sp.purge + sp.embargo  # 5 + 2 = 7


def test_leakage_detected_should_fail_test():
    """故意构造泄漏 fold：test 落在 train 内 → assert_no_leakage 必须抛异常。"""
    leaky = [
        Fold(train_start=0, train_end=250, test_start=240, test_end=300),  # 重叠！
    ]
    with pytest.raises(HexLeakageError):
        assert_no_leakage(leaky, purge=5)


def test_static_has_leakage():
    leaky = Fold(train_start=0, train_end=250, test_start=240, test_end=300)
    clean = Fold(train_start=0, train_end=250, test_start=257, test_end=317)
    assert WalkForwardSplitter.has_leakage(leaky, purge=5) is True
    assert WalkForwardSplitter.has_leakage(clean, purge=5) is False


def test_expanding_mode_works():
    idx = _index(800)
    sp = WalkForwardSplitter(train_len=200, test_len=60, purge=5, embargo=2, mode="expanding")
    folds = sp.split(idx)
    sp.assert_no_leakage(folds)
    # 扩展模式：所有 fold 训练窗起点为 0
    assert all(f.train_start == 0 for f in folds)
