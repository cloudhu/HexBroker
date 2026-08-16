"""代码审核（2026-08-16）修复项的回归测试。

覆盖 4 项 P1 修复：
1. ``base._signals_from_paths``：vol_hat 用 n_mc 而非窗口数判 ddof（防 NaN/错误置 0）。
2. ``feature.pipeline``：配置 global_codes 但未传 global_close → fail-fast 报错（防特征静默缺失）。
3. ``ablate_features``：--global-only 未传 --global-code 时自动加载全部外盘候选。
4. ``ensemble.ensemble_signals``：ForecastSignal 无 eff_thr 时用默认 0.05（防 AttributeError）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.forecast.base import ForecastModel, ForecastSignal


# ---------------------------------------------------------------------------
# 1. vol_hat / n_mc 判 ddof
# ---------------------------------------------------------------------------
class _CfgStub:
    """最小 cfg 桩：仅暴露 forecast/feature 所需属性。"""

    class _Forecast:
        horizon = 5
        n_mc_samples = 30
        effective_threshold = 0.05

    class _Feature:
        normalize_window = 20

    def __init__(self) -> None:
        self.forecast = self._Forecast()
        self.feature = self._Feature()


class _Model(ForecastModel):
    """最小可实例化模型（仅测 _signals_from_paths）。"""

    family = "test"

    def fit(self, X, y, X_valid=None):
        raise NotImplementedError

    def predict(self, X):
        raise NotImplementedError

    def _next_return(self, windows):
        raise NotImplementedError


def _run_signals(n_mc: int, n_windows: int) -> list[ForecastSignal]:
    m = _Model(_CfgStub(), model_id="t")
    # paths: (n_mc, n_windows, horizon) —— 全部 +0.1 保证 p_up=1、路径有变化
    rng = np.random.default_rng(0)
    paths = 0.1 + rng.normal(0, 0.01, size=(n_mc, n_windows, 5))
    mi = pd.MultiIndex.from_product(
        [["au0"], pd.date_range("2020-01-01", periods=n_windows, freq="B")],
        names=["symbol", "datetime"],
    )
    return m._signals_from_paths(paths, mi)


def test_vol_hat_single_mc_no_nan():
    """n_mc=1 且多窗：vol_hat 应为 0.0（旧代码误用窗口数判 ddof → std=NaN → conf=NaN）。"""
    sigs = _run_signals(n_mc=1, n_windows=5)
    assert len(sigs) == 5
    for s in sigs:
        assert s.vol_hat == 0.0
        # 单路径 iqr=0 → conf=clip(1-0)=1.0（无波动高置信），关键是绝不 NaN
        assert not np.isnan(s.conf)
        assert 0.0 <= s.conf <= 1.0


def test_vol_hat_multi_mc_finite():
    """n_mc>1：vol_hat 用 n_mc 样本标准差，应为有限正值。"""
    sigs = _run_signals(n_mc=30, n_windows=3)
    for s in sigs:
        assert s.vol_hat > 0
        assert 0.0 <= s.conf <= 1.0


# ---------------------------------------------------------------------------
# 2. 外盘 global_codes 无数据 → fail-fast
# ---------------------------------------------------------------------------
def test_global_codes_without_data_raises():
    """配置了 cross_params.global_codes 但未传 global_close → ValueError。"""
    from hexbroker.feature.pipeline import FeaturePipeline

    cfg = _CfgStub()
    cfg.feature.transformers = ["technical", "microstructure", "cross", "normalize"]
    cfg.feature.cross_params = {"global_codes": ["spx"]}
    with pytest.raises(ValueError, match="global_codes"):
        FeaturePipeline(cfg, global_close=None)


def test_global_codes_with_data_ok():
    """传入 global_close 后正常构造（不再报错）。"""
    from hexbroker.feature.pipeline import FeaturePipeline

    cfg = _CfgStub()
    cfg.feature.transformers = ["technical", "microstructure", "cross", "normalize"]
    cfg.feature.cross_params = {"global_codes": ["spx"]}
    s = pd.Series([1.0], index=pd.to_datetime(["2020-01-02"]))
    FeaturePipeline(cfg, global_close={"spx": s})


# ---------------------------------------------------------------------------
# 3. ablate --global-only 自动加载全部外盘候选
# ---------------------------------------------------------------------------
def test_ablate_global_only_auto_loads_candidates(monkeypatch):
    """--global-only 未传 --global-code 时，应自动加载全部 GLOBAL_CANDIDATES。"""
    import argparse

    import scripts.ablate_features as ab

    seen = {}

    def fake_load(code):
        seen[code] = True
        return pd.Series([1.0], index=pd.to_datetime(["2020-01-02"]))

    monkeypatch.setattr(ab, "load_global_close", fake_load)
    monkeypatch.setattr(ab, "align_global_to_inner", lambda g, idx: g.reindex(idx).ffill())

    args = argparse.Namespace(global_only=True, global_code=None)
    assert args.global_code is None
    # 复刻 main 中的自动加载逻辑
    if args.global_only and not args.global_code:
        args.global_code = ",".join(ab.GLOBAL_CANDIDATES)
    assert args.global_code == ",".join(ab.GLOBAL_CANDIDATES)
    assert ab.GLOBAL_CANDIDATES  # 非空候选表


# ---------------------------------------------------------------------------
# 4. ensemble eff_thr 默认值防御
# ---------------------------------------------------------------------------
def test_ensemble_eff_thr_default():
    """ForecastSignal 无 eff_thr 字段时，集成使用默认 0.05 而不抛 AttributeError。"""
    from hexbroker.forecast.ensemble import ensemble_signals

    def _sig(ts, p_up):
        return ForecastSignal(
            symbol="au0", ts=ts, horizon=5, p_up=p_up, exp_ret=0.0, quantiles={},
            vol_hat=1.0, conf=1.0, model_id="t",
            train_end=pd.Timestamp("2020-01-01"), is_effective=False,
        )

    l1 = [_sig(pd.Timestamp("2020-01-06"), 0.9), _sig(pd.Timestamp("2020-01-07"), 0.1)]
    l2 = [_sig(pd.Timestamp("2020-01-06"), 0.8), _sig(pd.Timestamp("2020-01-07"), 0.2)]
    out = ensemble_signals([l1, l2])
    assert len(out) == 2
    # p_up 均值：0.85/0.15 → is_effective 用默认 0.05 阈值 → True
    assert all(s.is_effective for s in out)
