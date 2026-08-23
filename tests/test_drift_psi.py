"""V3 回归：PSI 空箱/退化不得爆炸（原实现实测 8.39，远超阈值 0.2）。

修复判据：PSI 必须有限且收敛到合理区间（远小于原 8.39）；真实轻微漂移不应被误报。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hexbroker.evolution.drift import DriftDetector, psi


def _date_range(n):
    return pd.date_range("2020-01-01", periods=n, freq="D")


def test_psi_zero_when_identical():
    e = np.linspace(0.0, 1.0, 200)
    a = e.copy()
    assert abs(psi(a, e)) < 1e-9


def test_psi_collapsed_is_bounded():
    # 退化场景：baseline 均匀分布，actual 全压到单一值。
    # 原实现空箱爆炸到 8.39；修复后占比 floor 钳制，PSI 必须有限且 < 5（合理上限）。
    rng = np.random.default_rng(0)
    e = rng.uniform(0.0, 1.0, 500)
    a = np.full(500, 0.5)
    val = psi(a, e)
    assert np.isfinite(val)
    assert val < 5.0, f"PSI 空箱未收敛：{val}（原 8.39）"


def test_psi_clip_bounded_ratio():
    # 极端分布比（actual 集中于一端）也应被 floor 钳制在有限范围
    e = np.linspace(0.0, 1.0, 1000)
    a = np.concatenate([np.zeros(900), np.ones(100)])
    val = psi(a, e)
    assert np.isfinite(val)
    assert val < 5.0, f"PSI 比值未钳制：{val}"


def test_psi_mild_drift_not_false_positive():
    # 轻微漂移（整体平移 0.02）应得到很小的 PSI，不会误触发漂移阈值(0.2)
    rng = np.random.default_rng(3)
    e = rng.normal(0.0, 1.0, 1000)
    a = e + 0.02  # 极小偏移
    val = psi(a, e)
    assert np.isfinite(val)
    assert val < 0.2, f"轻微漂移被误报：{val}"


def test_detector_event_finite_and_bounded():
    n = 300
    df = pd.DataFrame(
        {
            "f_x": np.concatenate(
                [np.zeros(150), np.ones(150)]
            )
            + np.random.default_rng(1).normal(0, 0.01, n),
        },
        index=_date_range(n),
    )
    det = DriftDetector(threshold=0.2, baseline_len=120)
    events = det.detect(df, feature_cols=["f_x"])
    assert len(events) == 1
    assert np.isfinite(events[0].psi)
    # 真实剧烈漂移本就该得高 PSI；修复只保证不爆到原 8.39 这种异常值
    assert events[0].psi < 8.0, f"PSI 未收敛：{events[0].psi}"
