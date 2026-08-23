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


def test_assert_no_leakage_honors_embargo():
    """embargo 间隙内的 fold 必须被 assert_no_leakage 捕获（L6 修复）。

    构造一个 purge 合法、但 test 落在 embargo 间隙内的 fold：
    train_max_pos=249, purge=5, embargo=2 → 合法 test_start 应为 256；
    此处 test_start=255 仅满足 purge（255 > 249+5），却仍在 embargo(+2) 间隙内。
    """
    within_embargo = [Fold(train_start=0, train_end=250, test_start=255, test_end=315)]
    sp = WalkForwardSplitter(train_len=250, test_len=60, purge=5, embargo=2)
    with pytest.raises(HexLeakageError):
        sp.assert_no_leakage(within_embargo)
    # 模块级便捷函数同样应透传 embargo
    with pytest.raises(HexLeakageError):
        assert_no_leakage(within_embargo, purge=5, embargo=2)


def test_split_is_idempotent():
    """L6 修复：同一实例重复调用 split() 必须返回完全相同的 fold（expanding 亦然）。

    旧实现会在 expanding 模式递增 ``self.train_len``，导致第二次调用训练窗被
    撑大、结果漂移，并在跨品种复用（refine_lightgbm_champion 等）时污染后续品种。
    """
    idx = _index(1000)
    for mode in ("rolling", "expanding"):
        sp = WalkForwardSplitter(train_len=200, test_len=60, purge=5, embargo=2, mode=mode)
        f1 = sp.split(idx)
        f2 = sp.split(idx)  # 模拟跨品种复用的中间状态：再调用一次
        assert len(f1) == len(f2)
        for a, b in zip(f1, f2):
            assert (a.train_start, a.train_end, a.test_start, a.test_end) == (
                b.train_start, b.train_end, b.test_start, b.test_end,
            )
        # 实例属性未被污染
        assert sp.train_len == 200

