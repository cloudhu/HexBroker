"""P12-2：S4 影子监控脚本触发/漂移逻辑 + CLI 参数透传测试。

覆盖：
  - ``evaluate_trigger``：尾部连续优势计数（升级触发 / 不触发 / consec 阈值生效）
  - ``evaluate_trigger``：ic_threshold 门槛生效
  - ``evaluate_drift``：绝对告警 / 相对回落预警 / 正常 / 未知
  - ``run_monitor``：CLI 覆盖参数（consec/ic_threshold/corr_alert/corr_watch_delta）
    显式透传（D1 缺陷回归：默认参数在函数定义期绑定导致 globals() 覆盖静默失效）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p12_s4_shadow_monitor import (  # noqa: E402
    evaluate_drift,
    evaluate_trigger,
    run_monitor,
)


def _idx20():
    return pd.date_range("2026-06-01", periods=20, freq="B")


def test_trigger_upgrade_when_tail_beats():
    idx = _idx20()
    ic_rt = pd.Series([0.02] * 13 + [0.06] * 7, index=idx)
    ic_v2 = pd.Series([0.03] * 13 + [0.02] * 7, index=idx)
    hit_rt = pd.Series([0.45] * 13 + [0.55] * 7, index=idx)
    hit_v2 = pd.Series([0.40] * 20, index=idx)
    res = evaluate_trigger(ic_rt, ic_v2, hit_rt, hit_v2,
                           consec=5, ic_threshold=0.03, hit_ref=0.50)
    assert res["verdict"] == "UPGRADE_TRIGGER"
    assert res["ic_consec_wins"] == 7
    assert res["hit_consec_wins"] == 7


def test_trigger_no_when_short_beat():
    idx = _idx20()
    ic_rt = pd.Series([0.02] * 17 + [0.06] * 3, index=idx)
    ic_v2 = pd.Series([0.03] * 13 + [0.02] * 7, index=idx)
    hit_rt = pd.Series([0.40] * 20, index=idx)
    hit_v2 = pd.Series([0.40] * 20, index=idx)
    res = evaluate_trigger(ic_rt, ic_v2, hit_rt, hit_v2,
                           consec=5, ic_threshold=0.03, hit_ref=0.50)
    assert res["verdict"] == "NO_TRIGGER"
    assert res["ic_consec_wins"] == 3
    assert res["hit_consec_wins"] == 0


def test_trigger_consec_param_respected():
    """D1 回归：consec 显式传入 8 时，7 窗优势不得触发升级。"""
    idx = _idx20()
    ic_rt = pd.Series([0.02] * 13 + [0.06] * 7, index=idx)
    ic_v2 = pd.Series([0.03] * 13 + [0.02] * 7, index=idx)
    hit_rt = pd.Series([0.45] * 13 + [0.55] * 7, index=idx)
    hit_v2 = pd.Series([0.40] * 20, index=idx)
    res = evaluate_trigger(ic_rt, ic_v2, hit_rt, hit_v2,
                           consec=8, ic_threshold=0.03, hit_ref=0.50)
    assert res["verdict"] == "NO_TRIGGER"
    assert res["ic_consec_wins"] == 7


def test_trigger_ic_threshold_param_respected():
    """ic_threshold 门槛生效：|IC| <= 门槛不算胜窗。"""
    idx = _idx20()
    ic_rt = pd.Series([0.02] * 13 + [0.04] * 7, index=idx)
    ic_v2 = pd.Series([0.03] * 13 + [0.02] * 7, index=idx)
    hit_rt = pd.Series([0.45] * 13 + [0.55] * 7, index=idx)
    hit_v2 = pd.Series([0.40] * 20, index=idx)
    res = evaluate_trigger(ic_rt, ic_v2, hit_rt, hit_v2,
                           consec=5, ic_threshold=0.05, hit_ref=0.50)
    # |0.04| < 0.05 → IC 不算胜；命中率胜 7 窗仍触发
    assert res["ic_consec_wins"] == 0
    assert res["hit_consec_wins"] == 7


def test_drift_thresholds():
    assert evaluate_drift(0.38, 0.60, alert=0.40, watch_delta=0.15) == "DRIFT_ALERT"
    assert evaluate_drift(0.42, 0.60, alert=0.40, watch_delta=0.15) == "DRIFT_WATCH"
    assert evaluate_drift(0.55, 0.60, alert=0.40, watch_delta=0.15) == "DRIFT_OK"
    assert evaluate_drift(float("nan"), 0.60) == "DRIFT_UNKNOWN"


def test_drift_alert_param_respected():
    """D1 回归：corr_alert 显式传入 0.99 时 0.60 相关必告警。"""
    assert evaluate_drift(0.60, 0.60, alert=0.99, watch_delta=0.15) == "DRIFT_ALERT"


def _fake_metrics(label: str) -> dict:
    ts = pd.Timestamp("2026-01-05")
    return {
        "label": label,
        "n_rows": 1, "n_dates": 1, "coverage_start": ts.date(), "coverage_end": ts.date(),
        "signal_max_date": ts.date(), "avg_symbols_per_day": 1.0, "n_effective": 1,
        "effective_ratio": 1.0, "oos_n_dates": 1, "oos_ic_days": 1,
        "ic_last_date": ts.date(), "rolling_ic_63d": 0.0,
        "hit_last_date": ts.date(), "rolling_hit_63d": 0.5,
        "ic_series": pd.Series(dtype=float), "hit_series": pd.Series(dtype=float),
    }


def test_run_monitor_threads_cli_params():
    """D1 回归：run_monitor 把 CLI 覆盖参数显式透传给 evaluate_trigger/evaluate_drift。"""
    from scripts import p12_s4_shadow_monitor as mod

    ts = pd.Timestamp("2026-01-05")
    fake_sig = pd.DataFrame({
        "symbol": ["au0"], "ts": [ts], "p_up": [0.5], "exp_ret": [0.01], "is_effective": [True],
    })
    fake_panel = pd.DataFrame({"symbol": ["au0"], "date": [ts], "close": [100.0]})
    fake_base = pd.DataFrame({
        "kline_max_date": ["2026-01-01", "2026-01-01"],
        "signal_max_date": ["2026-01-01", "2026-01-01"],
        "cache": ["v2", "rt30"],
        "corr_pooled_63d": [0.60, 0.60],
    })
    fake_corr = {
        "corr_n_days": 0, "corr_last_date": None, "corr_daily_rolling_63d": float("nan"),
        "corr_pooled_63d": 0.60, "corr_pooled_n_pairs": 0,
    }
    with mock.patch.object(mod, "load_signals", return_value=fake_sig), \
         mock.patch.object(mod, "load_close_panel", return_value=fake_panel), \
         mock.patch.object(mod, "load_baseline", return_value=fake_base), \
         mock.patch.object(mod, "cache_metrics",
                           side_effect=[_fake_metrics("v2"), _fake_metrics("rt30")]), \
         mock.patch.object(mod, "corr_metrics", return_value=fake_corr), \
         mock.patch.object(mod, "evaluate_trigger") as m_trig, \
         mock.patch.object(mod, "evaluate_drift") as m_drift:
        m_trig.return_value = {"ic_consec_wins": 0, "hit_consec_wins": 0,
                               "n_common_dates": 0, "verdict": "NO_TRIGGER"}
        m_drift.return_value = "DRIFT_OK"
        run_monitor(fake_panel, baseline_csv=Path("unused.csv"), verbose=False,
                    window=42, consec=8, ic_threshold=0.05,
                    corr_alert=0.99, corr_watch_delta=0.20)
        assert m_trig.call_args.kwargs["consec"] == 8
        assert m_trig.call_args.kwargs["ic_threshold"] == 0.05
        assert m_drift.call_args.kwargs["alert"] == 0.99
        assert m_drift.call_args.kwargs["watch_delta"] == 0.20
        for call in mod.cache_metrics.call_args_list:
            assert call.kwargs["window"] == 42
