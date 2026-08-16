"""概率校准测试（§3.2）。

核心验收：``calibration_error`` 在校准后必须 < 0.05。
"""

import numpy as np
import pytest

from hexbroker.forecast.calibration import (
    PlattScaler,
    calibrate_signals,
    calibration_error,
    reliability_curve,
)


def test_platt_calibration_error_below_threshold():
    """构造「真实概率 + 轻微扰动」场景，Platt 缩放后校准误差应 < 0.05。"""
    rng = np.random.default_rng(0)
    n = 8000
    score = rng.normal(size=n)
    true_p = 1.0 / (1.0 + np.exp(-score))
    y = (rng.random(n) < true_p).astype(float)
    raw_p = np.clip(true_p + rng.normal(0.0, 0.03, n), 1e-3, 1 - 1e-3)

    scaler = PlattScaler().fit(raw_p, y, n_iter=200, lr=0.05)
    cal_p = scaler.transform(raw_p)
    err = calibration_error(cal_p, y)
    assert err < 0.05, f"校准误差 {err:.4f} 超过 0.05 阈值"


def test_calibration_error_decreases_after_scaling():
    """校准后误差不应高于校准前（至少不退化）。"""
    rng = np.random.default_rng(1)
    n = 6000
    score = rng.normal(size=n)
    true_p = 1.0 / (1.0 + np.exp(-score))
    y = (rng.random(n) < true_p).astype(float)
    # 系统性偏差的 raw p
    raw_p = np.clip(true_p * 0.7 + 0.15, 1e-3, 1 - 1e-3)
    before = calibration_error(raw_p, y)
    scaler = PlattScaler().fit(raw_p, y, n_iter=150, lr=0.05)
    after = calibration_error(scaler.transform(raw_p), y)
    assert after <= before + 1e-6


def test_reliability_curve_monotonicish():
    """reliability_curve 返回与分箱对应的列表且长度一致。"""
    rng = np.random.default_rng(2)
    n = 4000
    score = rng.normal(size=n)
    true_p = 1.0 / (1.0 + np.exp(-score))
    y = (rng.random(n) < true_p).astype(float)
    raw_p = np.clip(true_p + rng.normal(0, 0.02, n), 1e-3, 1 - 1e-3)
    rc = reliability_curve(raw_p, y, n_bins=10)
    assert len(rc["pred"]) == len(rc["obs"]) == len(rc["count"])
    assert all(c > 0 for c in rc["count"])


def _make_signals(p_ups, n=1):
    """构造最小可用的 ForecastSignal 列表。"""
    from hexbroker.forecast.base import ForecastSignal
    import pandas as pd
    sigs = []
    for i, p in enumerate(p_ups):
        sigs.append(ForecastSignal(
            symbol="au0", ts=pd.Timestamp("2020-01-01") + pd.Timedelta(days=i),
            horizon=5, p_up=float(p), exp_ret=0.0, quantiles={},
            vol_hat=1.0, conf=1.0, model_id="t",
            train_end=pd.Timestamp("2020-01-01"), is_effective=False,
        ))
    return sigs


def test_isotonic_monotonic_and_bounded():
    """isotonic 校准保持单调性且输出在 (0,1)。"""
    rng = np.random.default_rng(3)
    p = np.sort(rng.uniform(0.3, 0.7, 200))
    y = (rng.random(200) < p).astype(float)
    sigs = _make_signals(p)
    calibrate_signals(sigs, y, method="isotonic")
    cal = np.array([s.p_up for s in sigs])
    assert (cal >= 1e-6).all() and (cal <= 1 - 1e-6).all()
    assert (np.diff(cal) >= -1e-9).all(), "isotonic 输出必须单调不减"
    # 校准质量优于未校准
    before = calibration_error(p, y)
    after = calibration_error(cal, y)
    assert after <= before + 1e-6


def test_calibrate_none_keeps_p_up():
    """method='none' 不改 p_up，只重算 is_effective。"""
    sigs = _make_signals([0.4, 0.6])
    calibrate_signals(sigs, np.array([0, 1]), method="none")
    assert sigs[0].p_up == 0.4 and sigs[1].p_up == 0.6
    assert sigs[0].is_effective is True and sigs[1].is_effective is True


def test_calibrate_unknown_method_raises():
    sigs = _make_signals([0.5])
    with pytest.raises(ValueError):
        calibrate_signals(sigs, np.array([1]), method="bogus")
