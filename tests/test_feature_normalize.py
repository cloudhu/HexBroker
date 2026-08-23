"""L3 回归：每品种独立归一，后续品种新增特征必须被归一（不泄漏未归一/被丢弃列）。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from hexbroker.feature.normalize import RollingNormalizer
from hexbroker.feature.pipeline import FeaturePipeline


def _cfg():
    fc = SimpleNamespace(
        transformers=["normalize"],
        normalize_window=120,
        technical_params={},
        iterative_params={},
        cross_params={},
        weekly_params={},
        fundamental_params={},
        keep_features=None,
    )
    return SimpleNamespace(feature=fc)


def test_per_symbol_normalizer_normalizes_later_symbol_extra_feature():
    """品种2 比品种1 多一列 f_c；修复后 f_c 必须存在且被归一（std≈1）。"""
    pipe = FeaturePipeline(_cfg())
    idx = pd.date_range("2020-01-01", periods=6, freq="D")
    s1 = pd.DataFrame({"f_a": [1, 2, 3, 4, 5, 6], "f_b": [6, 5, 4, 3, 2, 1]}, index=idx)
    s2 = pd.DataFrame(
        {"f_a": [1, 2, 3, 4, 5, 6], "f_b": [6, 5, 4, 3, 2, 1], "f_c": [100, 200, 300, 400, 500, 600]},
        index=idx,
    )
    out1 = pipe._per_symbol(s1)
    out2 = pipe._per_symbol(s2)
    # f_c 必须存在且被滚动 z-score 归一（修复前共享归一器会把 f_c 锁死在首品种列集 → 不归一/被丢弃）
    assert "f_c" in out2.columns
    # 末点处于完整窗口内：z = (600 - 350)/std_6 ≈ 1.464（而非原始 600）
    # 旧实现会留下未归一的原始值（iloc[-1] == 600）。
    assert out2["f_c"].iloc[-1] == pytest.approx(1.464, abs=1e-2)
    assert out2["f_a"].iloc[-1] == pytest.approx(1.464, abs=1e-2)
    assert out2["f_b"].iloc[-1] == pytest.approx(-1.464, abs=1e-2)
    # 整体已缩放到单位量级（原始 std≈170 → 归一后 ~0.74）
    assert out2["f_c"].std() < 2.0
    assert out2["f_c"].std() > 0.5
    # 首品种不受影响
    assert out1["f_a"].iloc[-1] == pytest.approx(1.464, abs=1e-2)


def test_rolling_normalizer_fit_locks_columns_on_reuse():
    """直接复现机制：单实例复用 + fit 仅首次生效 → 新增列 C 不被归一（旧缺陷）。"""
    rn = RollingNormalizer(window=5, min_periods=2)
    df1 = pd.DataFrame({"A": [1, 2, 3, 4, 5, 6], "B": [6, 5, 4, 3, 2, 1]})
    out1 = rn.fit_transform(df1)
    assert set(out1.columns) == {"A", "B"}
    df2 = pd.DataFrame(
        {"A": [1, 2, 3, 4, 5, 6], "B": [6, 5, 4, 3, 2, 1], "C": [100, 200, 300, 400, 500, 600]}
    )
    out2 = rn.fit_transform(df2)
    # 缺陷表现：C 列存在但量纲未动（fit 见 columns 已非 None 不再重置 → 仅归一 A/B）
    assert "C" in out2.columns
    assert out2["C"].iloc[-1] == pytest.approx(600.0, abs=1e-6)
    # 对照：全新实例会正确归一 C（末点用最后 5 点窗 → z≈1.414，单位量级而非原始 600）
    rn2 = RollingNormalizer(window=5, min_periods=2)
    out2_fixed = rn2.fit_transform(df2)
    assert out2_fixed["C"].iloc[-1] == pytest.approx(1.414, abs=1e-2)
