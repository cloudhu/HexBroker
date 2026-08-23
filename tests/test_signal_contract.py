"""预测信号契约测试（§3.2 / §8.4）。

验证 ForecastSignal 的数据契约：序列化往返、取值约束、is_effective 阈值、
sample_paths 形状与确定性，以及 SignalStore 只落盘 OOS 信号。
"""

import numpy as np
import pandas as pd
import pytest

from hexbroker.config import default_demo_config
from hexbroker.data.sources.synthetic_source import SyntheticSource
from hexbroker.feature import build_features
from hexbroker.forecast.base import ForecastSignal
from hexbroker.forecast.signal_store import SignalStore
from hexbroker.forecast.trainer import ForecastTrainer


def _make_signal(p_up: float, eff_thr: float = 0.05) -> ForecastSignal:
    ts = pd.Timestamp("2021-01-01")
    return ForecastSignal(
        symbol="SHFE.cu",
        ts=ts,
        horizon=5,
        p_up=p_up,
        exp_ret=0.001,
        quantiles={"q10": -0.01, "q25": 0.0, "q50": 0.001, "q75": 0.01, "q90": 0.02},
        vol_hat=0.01,
        conf=0.6,
        model_id="test123",
        train_end=pd.Timestamp("2020-12-31"),
        is_effective=abs(p_up - 0.5) > eff_thr,
    )


def test_signal_roundtrip_record():
    """to_record / from_record 往返保持字段一致。"""
    s = _make_signal(0.62)
    rec = s.to_record()
    assert isinstance(rec["ts"], str)  # 时间戳已序列化
    s2 = ForecastSignal.from_record(rec)
    assert s2.symbol == s.symbol
    assert s2.ts == s.ts
    assert s2.p_up == s.p_up
    assert s2.model_id == s.model_id
    assert s2.train_end == s.train_end


def test_p_up_clipped_in_model_output():
    """模型输出的 p_up 必须落在 (0,1) 开区间内。"""
    cfg = default_demo_config()
    bars = SyntheticSource(n_bars=300, seed=1).fetch_bars(["SHFE.cu"])
    ff = build_features(bars, cfg)
    import tempfile

    store = SignalStore(tempfile.mkdtemp())
    trainer = ForecastTrainer(cfg, store, model_name="ar_transformer")
    res = trainer.run(bars, ff)
    frame = store.get_frame(model_id=res.model_id)
    assert ((frame["p_up"] > 0) & (frame["p_up"] < 1)).all()


def test_is_effective_threshold_logic():
    """is_effective 仅在偏离 0.5 超过阈值时为 True。"""
    assert _make_signal(0.5).is_effective is False
    assert _make_signal(0.5 + 0.04).is_effective is False  # 0.54 距 0.5 为 0.04 < 0.05
    assert _make_signal(0.56).is_effective is True
    assert _make_signal(0.44).is_effective is True


def test_sample_paths_shape_and_determinism():
    """sample_paths 形状正确且同种子可复现。"""
    rng = np.random.default_rng(0)
    windows = rng.normal(size=(7, 30 * 4))
    # 用一个已训练的小模型接口：这里直接验证 ARTransformer 的采样接口
    from hexbroker.forecast.autoregressive import ARTransformer

    cfg = default_demo_config()
    m = ARTransformer(cfg)
    m.lookback = 30
    m._build(4)
    m.n_mc = 20
    paths1 = m.sample_paths(windows)
    paths2 = m.sample_paths(windows)
    assert paths1.shape == (20, 7, m.horizon)
    assert np.allclose(paths1, paths2)  # 确定性（固定 seed）


def test_signal_store_contains_oos_only():
    """SignalStore 落盘的信号都是 OOS（ts > train_end）。"""
    import tempfile

    cfg = default_demo_config()
    bars = SyntheticSource(n_bars=400, seed=2).fetch_bars(["SHFE.cu"])
    ff = build_features(bars, cfg)
    store = SignalStore(tempfile.mkdtemp())
    res = ForecastTrainer(cfg, store, model_name="ar_transformer").run(bars, ff)
    frame = store.get_frame(model_id=res.model_id)
    # 每条信号：ts 必须严格晚于其 train_end（OOS 红线）
    df = frame.reset_index()
    assert (df["datetime"] > pd.to_datetime(df["train_end"])).all()


def test_put_drops_in_sample_signals():
    """L9 修复：put() 必须丢弃样本内(ts<=train_end)信号并告警，绝不落盘。"""
    import tempfile
    import warnings

    store = SignalStore(tempfile.mkdtemp())
    oos = _make_signal(0.62)  # ts=2021-01-01 > train_end=2020-12-31
    insample = ForecastSignal(
        symbol="SHFE.cu",
        ts=pd.Timestamp("2020-12-30"),  # ts < train_end → 泄漏
        horizon=5,
        p_up=0.62,
        exp_ret=0.001,
        quantiles={"q10": -0.01, "q25": 0.0, "q50": 0.001, "q75": 0.01, "q90": 0.02},
        vol_hat=0.01,
        conf=0.6,
        model_id="test123",
        train_end=pd.Timestamp("2020-12-31"),
        is_effective=True,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        n = store.put([oos, insample])
        assert any(issubclass(c.category, Warning) for c in caught), "应产生丢弃告警"
    assert n == 1  # 仅 OOS 落盘
    frame = store.get_frame(model_id="test123").reset_index()
    assert len(frame) == 1
    assert frame.iloc[0]["datetime"] == pd.Timestamp("2021-01-01")
