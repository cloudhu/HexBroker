"""P0-3 四层指纹测试（PRD A3.2–A3.5）。

覆盖：可复现性（同配置一致）、敏感性（改任一配置/数据/参数必变）、
SignalStore sidecar 写读/校验、信号语义零改动。
"""

from __future__ import annotations

import pandas as pd

from _helpers import fast_cfg, make_prices

from hexbroker.data.schema import as_barframe
from hexbroker.forecast.base import ForecastSignal
from hexbroker.forecast.signal_store import SignalStore
from hexbroker.utils.fingerprint import (
    FourLayerFingerprint,
    compute_four_layer,
    four_layer_report_block,
)


def _barframe(seed=1, n_bars=60):
    prices = make_prices(n_bars=n_bars, seed=seed)
    prices = prices.assign(adj_close=prices["close"], raw_close=prices["close"])
    return as_barframe(prices, freq="1d")


def _sig(sym="SHFE.cu", ts="2024-01-02", mid="m1"):
    return ForecastSignal(
        symbol=sym, ts=pd.Timestamp(ts), horizon=5, p_up=0.6, exp_ret=0.01,
        quantiles={"q50": 0.01}, vol_hat=0.01, conf=0.8, model_id=mid,
        train_end=pd.Timestamp("2024-01-01"), is_effective=True,
    )


# ---------------------------------------------------------------------------
# 可复现性（A3.3）
# ---------------------------------------------------------------------------
def test_compute_four_layer_reproducible():
    cfg = fast_cfg()
    fp1 = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    fp2 = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    assert fp1 == fp2
    assert fp1.to_dict() == fp2.to_dict()
    assert hash(fp1) == hash(fp2)


# ---------------------------------------------------------------------------
# 敏感性（A3.4）：任一配置/数据/参数变化 → 指纹必变
# ---------------------------------------------------------------------------
def test_compute_four_layer_sensitive_to_data():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(seed=1), "m1", "2024-01-01", {"lr": 1e-3})
    other = compute_four_layer(cfg, _barframe(seed=2), "m1", "2024-01-01", {"lr": 1e-3})
    assert base != other
    assert base.data_version != other.data_version


def test_compute_four_layer_sensitive_to_feature_cfg():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    cfg2 = fast_cfg()
    cfg2.feature.normalize_window = 99
    other = compute_four_layer(cfg2, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    assert base != other
    assert base.feature_version != other.feature_version


def test_compute_four_layer_sensitive_to_model_id():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    other = compute_four_layer(cfg, _barframe(), "m2", "2024-01-01", {"lr": 1e-3})
    assert base != other
    assert base.model_version != other.model_version


def test_compute_four_layer_sensitive_to_params():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    other = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 2e-3})
    assert base != other
    assert base.param_hash != other.param_hash


def test_compute_four_layer_sensitive_to_train_end():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    other = compute_four_layer(cfg, _barframe(), "m1", "2024-06-01", {"lr": 1e-3})
    assert base != other


def test_compute_four_layer_sensitive_to_global_cfg():
    cfg = fast_cfg()
    base = compute_four_layer(cfg, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    cfg2 = fast_cfg()
    cfg2.seed = 7
    other = compute_four_layer(cfg2, _barframe(), "m1", "2024-01-01", {"lr": 1e-3})
    assert base != other
    assert base.config_version != other.config_version


# ---------------------------------------------------------------------------
# SignalStore sidecar（A3.2/A3.5）
# ---------------------------------------------------------------------------
def test_signal_store_sidecar_roundtrip(tmp_path):
    store = SignalStore(str(tmp_path))
    sigs = [_sig("SHFE.cu", "2024-01-02", "m1"), _sig("DCE.m", "2024-01-03", "m1")]
    fp = compute_four_layer(fast_cfg(), _barframe(), "m1", "2024-01-01", {"lr": 1e-3})

    n = store.put(sigs, fingerprint=fp)
    assert n == 2

    sidecars = list((tmp_path / "m1").glob("*.manifest.json"))
    assert len(sidecars) == 1

    fp_list = store.fingerprints(model_id="m1")
    assert len(fp_list) == 1
    assert fp_list[0]["data_version"] == fp.data_version
    assert fp_list[0]["feature_version"] == fp.feature_version
    assert fp_list[0]["model_version"] == fp.model_version
    assert fp_list[0]["param_hash"] == fp.param_hash
    assert fp_list[0]["config_version"] == fp.config_version

    v = store.verify(model_id="m1")
    assert v["checked"] == 1
    assert v["mismatched"] == []
    assert v["missing"] == []

    # 信号语义不变：get_frame 读回两条 OOS 信号
    df = store.get_frame(model_id="m1")
    assert len(df) == 2


def test_signal_store_put_without_fingerprint_backward_compat(tmp_path):
    """fingerprint=None 时行为与改动前完全一致（无 sidecar）。"""
    store = SignalStore(str(tmp_path))
    n = store.put([_sig(), _sig("DCE.m", "2024-01-03")])
    assert n == 2
    fps = store.fingerprints()
    assert len(fps) == 1
    assert fps[0]["model_id"] == "m1"
    assert "data_version" not in fps[0], "无 sidecar 时不应有四层指纹字段"
    v = store.verify()
    assert v["checked"] == 1
    assert v["missing"], "有 parquet 但无 manifest → 应列入 missing"
    assert v["mismatched"] == []


def test_signal_store_inconsistent_fingerprint_warns(tmp_path):
    store = SignalStore(str(tmp_path))
    fp1 = FourLayerFingerprint("d1", "f1", "m1:te:mc1", "p1", "c1")
    fp2 = FourLayerFingerprint("d2", "f2", "m1:te:mc2", "p2", "c2")
    store.put([_sig()], fingerprint=fp1)
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        store.put([_sig("DCE.m", "2024-01-03")], fingerprint=fp2)
        assert any("指纹不一致" in str(x.message) for x in w)


def test_signal_store_put_rejects_bad_fingerprint_type(tmp_path):
    store = SignalStore(str(tmp_path))
    import pytest

    with pytest.raises(TypeError):
        store.put([_sig()], fingerprint=123)


def test_four_layer_report_block():
    fp = FourLayerFingerprint("d", "f", "m", "p", "c")
    block = four_layer_report_block(fp)
    assert block["fingerprint"]["data_version"] == "d"
    assert block["fingerprint"]["config_version"] == "c"
    assert "note" in block
