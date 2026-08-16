"""独立验证：真实数据 OOS 信号校验 + R7 裁决脚本（scripts/validate_oos_r7.py）。

验证重点（QA「严过关」fresh-eyes 复核）：
  1. 闸门1指标函数 compute_gate1 数值正确（方向/有效准确率/coverage/RankIC/IC），
     realized 含 NaN 时只剔 NaN 计入方向统计、coverage 用全量。
  2. gate1_pass 边界（≥ 阈值即 PASS）。
  3. SignalStore 按 model_id 分片正确。
  4. 框架缓存改动 _clear_inference_caches / clear_cache 只清缓存、
     不改变任何信号数值（两次 predict 完全一致；调用后 _cache 已空）。
  5. Kronos 代理路径优雅降级（无 torch 时降级 ARTransformer，不崩）。

全部离线、无网络依赖；仅 compute_gate1/gate1_pass/compute_realized_returns/
SignalStore/build_model 相关接口。
"""

import math
import tempfile

import numpy as np
import pandas as pd
import pytest
import scipy.stats as ss

from scripts.validate_oos_r7 import (
    GATE_COVERAGE,
    GATE_DIR_ACC,
    GATE_EFF_ACC,
    compute_gate1,
    compute_realized_returns,
    gate1_pass,
)
from hexbroker.config import default_demo_config
from hexbroker.data.sources.synthetic_source import SyntheticSource
from hexbroker.feature import build_features
from hexbroker.forecast.base import ForecastSignal
from hexbroker.forecast.signal_store import SignalStore
from hexbroker.forecast.trainer import ForecastTrainer
from hexbroker.utils.registry import build as build_model


# ---------------------------------------------------------------------------
# 测试数据构造工具
# ---------------------------------------------------------------------------
def _make_signal_frame(p_up, exp_ret, eff, realized, sym="SHFE.cu"):
    n = len(p_up)
    idx = pd.MultiIndex.from_arrays(
        [[sym] * n, pd.date_range("2021-01-01", periods=n)],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "p_up": np.asarray(p_up, dtype=float),
            "exp_ret": np.asarray(exp_ret, dtype=float),
            "is_effective": np.asarray(eff, dtype=bool),
            "realized": np.asarray(realized, dtype=float),
        },
        index=idx,
    )


# ===========================================================================
# 1. 闸门1指标函数 compute_gate1
# ===========================================================================
def test_compute_gate1_metrics_and_nan_handling():
    """方向/有效准确率/coverage/RankIC/IC 数值正确；NaN 只剔出方向统计。"""
    realized = np.array([0.1, -0.2, np.nan, 0.05, -0.1])
    p_up = np.array([0.7, 0.3, 0.8, 0.4, 0.6])
    exp_ret = np.array([0.02, -0.01, 0.03, 0.005, -0.02])
    eff = np.array([True, True, False, True, True])

    sig = _make_signal_frame(p_up, exp_ret, eff, realized)
    m = compute_gate1(sig)

    # coverage 用全量（含 NaN 行）
    assert m["n_oos"] == 5
    assert m["coverage"] == pytest.approx(4 / 5)  # 4/5 有效信号

    # 方向准确率只在 realized 非 NaN 的 4 行上计算
    mask = ~np.isnan(realized)
    pred_dir = np.sign(p_up[mask] - 0.5)
    real_dir = np.sign(realized[mask])
    expected_dir = float(np.mean(pred_dir == real_dir))
    assert m["direction_accuracy"] == pytest.approx(expected_dir)

    # 有效准确率只在 (mask & eff) 的 4 行上计算
    eff_mask = mask & eff
    pred_dir_e = np.sign(p_up[eff_mask] - 0.5)
    real_dir_e = np.sign(realized[eff_mask])
    expected_eff = float(np.mean(pred_dir_e == real_dir_e))
    assert m["effective_accuracy"] == pytest.approx(expected_eff)

    # RankIC / IC 与 scipy 参考一致（仅用非 NaN 行）
    expected_rank_ic = ss.spearmanr(p_up[mask], realized[mask]).statistic
    expected_ic = ss.pearsonr(exp_ret[mask], realized[mask]).statistic
    assert m["rank_ic"] == pytest.approx(expected_rank_ic)
    assert m["ic"] == pytest.approx(expected_ic)


def test_compute_gate1_all_realized_nan():
    """realized 全 NaN -> 方向/有效/RankIC/IC 为 nan，但 coverage 仍可用。"""
    sig = _make_signal_frame(
        p_up=[0.7, 0.3, 0.6],
        exp_ret=[0.01, -0.01, 0.0],
        eff=[True, True, False],
        realized=[np.nan, np.nan, np.nan],
    )
    m = compute_gate1(sig)
    assert math.isnan(m["direction_accuracy"])
    assert math.isnan(m["effective_accuracy"])
    assert math.isnan(m["rank_ic"])
    assert math.isnan(m["ic"])
    assert m["coverage"] == pytest.approx(2 / 3)  # coverage 基于全量
    assert gate1_pass(m) is False  # 含 nan -> FAIL


def test_compute_gate1_rankic_sign_does_not_depend_on_p_up_offset():
    """RankIC 用 spearman(p_up, realized)，平移不影响秩相关。"""
    realized = np.array([0.1, -0.2, 0.05, -0.1, 0.0])
    p_up = np.array([0.7, 0.3, 0.4, 0.6, 0.5])
    sig = _make_signal_frame(p_up, p_up - 0.5, [True] * 5, realized)
    m = compute_gate1(sig)
    expected = ss.spearmanr(p_up, realized).statistic
    assert m["rank_ic"] == pytest.approx(expected)


# ===========================================================================
# 2. gate1_pass 边界
# ===========================================================================
@pytest.mark.parametrize(
    "da,ea,cov,expect",
    [
        (0.54, 0.58, 0.30, True),    # 恰好等于边界 -> PASS（≥）
        (0.5401, 0.5801, 0.3001, True),
        (0.539, 0.58, 0.30, False),  # 方向略低
        (0.54, 0.579, 0.30, False),  # 有效略低
        (0.54, 0.58, 0.299, False),  # coverage 略低
        (0.60, 0.60, 0.50, True),    # 全高于
        (0.54, 0.58, 0.30, True),    # 重复边界确认
    ],
)
def test_gate1_pass_boundaries(da, ea, cov, expect):
    assert gate1_pass({"direction_accuracy": da, "effective_accuracy": ea, "coverage": cov}) is expect


def test_gate1_pass_rejects_nan_and_none():
    assert gate1_pass({"direction_accuracy": float("nan"), "effective_accuracy": 0.6, "coverage": 0.4}) is False
    assert gate1_pass({"direction_accuracy": 0.6, "effective_accuracy": None, "coverage": 0.4}) is False
    assert gate1_pass({"direction_accuracy": 0.6, "effective_accuracy": 0.6}) is False  # 缺 coverage


def test_gate_thresholds_constants_match_contract():
    """脚本内阈值常量须与契约一致（方向≥54% / 有效≥58% / coverage≥30%）。"""
    assert GATE_DIR_ACC == 0.54
    assert GATE_EFF_ACC == 0.58
    assert GATE_COVERAGE == 0.30


# ===========================================================================
# 3. SignalStore 分片
# ===========================================================================
def test_signal_store_sharding_by_model_id():
    store = SignalStore(tempfile.mkdtemp())
    a = ForecastSignal(
        symbol="SHFE.cu", ts=pd.Timestamp("2021-01-01"), horizon=5, p_up=0.7,
        exp_ret=0.01, quantiles={"q50": 0.01}, vol_hat=0.01, conf=0.6,
        model_id="modelA", train_end=pd.Timestamp("2020-12-31"), is_effective=True,
    )
    b = ForecastSignal(
        symbol="SHFE.rb", ts=pd.Timestamp("2021-01-01"), horizon=5, p_up=0.3,
        exp_ret=-0.01, quantiles={"q50": -0.01}, vol_hat=0.01, conf=0.6,
        model_id="modelB", train_end=pd.Timestamp("2020-12-31"), is_effective=True,
    )
    store.put([a, b])

    fa = store.get_frame(model_id="modelA")
    fb = store.get_frame(model_id="modelB")
    assert list(fa.index.get_level_values("symbol")) == ["SHFE.cu"]
    assert list(fb.index.get_level_values("symbol")) == ["SHFE.rb"]
    assert fa["model_id"].unique().tolist() == ["modelA"]
    assert fb["model_id"].unique().tolist() == ["modelB"]
    assert set(store.models()) == {"modelA", "modelB"}


# ===========================================================================
# 4. 缓存安全：clear_cache 不改变信号数值
# ===========================================================================
def _trained_model(model_name, n_bars=400, seed=3, sym="SHFE.cu"):
    cfg = default_demo_config()
    bars = SyntheticSource(n_bars=n_bars, seed=seed).fetch_bars([sym])
    ff = build_features(bars, cfg)
    store = SignalStore(tempfile.mkdtemp())
    res = ForecastTrainer(cfg, store, model_name=model_name).run(bars, ff)
    assert res.models, f"{model_name} 未产出已训练模型"
    return res.models[-1], ff, sym


def _predict_twice_equal(model, ff, sym):
    feat = ff.df.loc[[sym]].sort_index()
    sigs1 = model.predict(feat)
    # 中间不手动 clear，直接第二次 predict
    sigs2 = model.predict(feat)
    assert sigs1 and sigs2, "predict 应返回非空信号"
    p1 = np.array([s.p_up for s in sigs1])
    p2 = np.array([s.p_up for s in sigs2])
    e1 = np.array([s.exp_ret for s in sigs1])
    e2 = np.array([s.exp_ret for s in sigs2])
    assert np.allclose(p1, p2, atol=1e-12), "两次 predict 的 p_up 必须完全一致"
    assert np.allclose(e1, e2, atol=1e-12), "两次 predict 的 exp_ret 必须完全一致"
    return sigs1


def test_cache_safety_gru_signal_values_unchanged():
    """GRU predict 两次完全一致（clear_cache 只回收缓存、不改信号值）。"""
    model, ff, sym = _trained_model("gru")
    _predict_twice_equal(model, ff, sym)
    # 调用末尾已 _clear_inference_caches：GRU 序列缓存应已清空
    assert len(model.gru._samples) == 0
    assert len(model.encoder._cache) == 0
    assert len(model.head._cache) == 0


def test_cache_safety_tcn_signal_values_unchanged():
    """TCN predict 两次完全一致；conv 前向缓存已清空。"""
    model, ff, sym = _trained_model("tcn")
    _predict_twice_equal(model, ff, sym)
    # TCN 的 conv 为 Sequential[CausalConv1D, ReLU]，_cache 应已清空
    for conv in model.convs:
        assert len(conv.layers[0]._cache) == 0
    assert len(model.encoder._cache) == 0
    assert len(model.head._cache) == 0


def test_clear_inference_caches_no_exception_on_lightgbm():
    """LightGBM 无网络层缓存，_clear_inference_caches 仍应无异常。"""
    model, ff, sym = _trained_model("lightgbm")
    model._clear_inference_caches()  # 不应抛异常


def test_ar_attention_cache_not_part_of_signal_computation():
    """AR predict 两次一致：信号值源于 forward 输出（paths），与注意力缓存无关。"""
    model, ff, sym = _trained_model("ar_transformer")
    _predict_twice_equal(model, ff, sym)


# ===========================================================================
# 5. Kronos 代理路径优雅降级
# ===========================================================================
def test_build_ar_transformer_returns_ar_instance():
    from hexbroker.forecast.autoregressive import ARTransformer

    cfg = default_demo_config()
    m = build_model("ar_transformer", cfg)
    assert isinstance(m, ARTransformer)


def test_kronos_adapter_degrades_without_torch():
    """无 torch/transformers 时 KronosAdapter 必须降级 ARTransformer 且不崩。"""
    from hexbroker.forecast import kronos_adapter

    # 沙箱环境 HAS_KRONOS 应为 False（无 torch/transformers）
    assert kronos_adapter.HAS_KRONOS is False

    cfg = default_demo_config()
    adapter = build_model("kronos", cfg)
    # 默认 enable_kronos=False -> 走 ARTransformer fallback
    assert adapter.enable_kronos is False

    bars = SyntheticSource(n_bars=400, seed=5).fetch_bars(["SHFE.cu"])
    ff = build_features(bars, cfg)
    # fit + predict 不应抛异常，且产出真实 ForecastSignal（即降级成功）
    res = ForecastTrainer(cfg, SignalStore(tempfile.mkdtemp()), model_name="kronos").run(bars, ff)
    assert res.model_id, "Kronos 降级后仍应产出 model_id"
    assert res.n_oos_signals > 0, "Kronos 降级后应收敛 OOS 信号"


# ===========================================================================
# 6. compute_realized_returns 与契约一致
# ===========================================================================
def test_compute_realized_returns_matches_manual_formula():
    """realized = close.shift(-horizon)/close - 1，按 (symbol,datetime) 对齐。"""
    cfg = default_demo_config()
    horizon = int(cfg.forecast.horizon)
    bars = SyntheticSource(n_bars=300, seed=7).fetch_bars(["SHFE.cu"])
    realized = compute_realized_returns(bars, horizon)

    close = bars.by_symbol("SHFE.cu")["close"].astype(float).reset_index(level=0, drop=True).sort_index()
    manual = close.shift(-horizon) / close - 1.0
    manual.index = pd.MultiIndex.from_arrays(
        [np.array(["SHFE.cu"] * len(manual)), manual.index.to_numpy()],
        names=["symbol", "datetime"],
    )
    manual.name = "realized"

    common = realized.index.intersection(manual.index)
    r = realized.loc[common].to_numpy()
    man = manual.loc[common].to_numpy()
    # NaN 位置必须一致（同标的同 bar），非 NaN 数值须相等
    assert np.array_equal(np.isnan(r), np.isnan(man)), "NaN 位置应与手动公式一致"
    nz = ~np.isnan(r)
    assert np.allclose(r[nz], man[nz], atol=1e-12), "非 NaN 的已实现收益必须与手动公式一致"
    # 末段 horizon 根应为 NaN（无未来收益）
    assert realized.isna().sum() >= horizon
