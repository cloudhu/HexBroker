"""代码审核补覆盖：hexbroker/evaluation 绩效评估测试。

覆盖 metrics / stats / baseline，含边界（单样本、权益归零、空列表）与过拟合诊断。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.evaluation.baseline import buy_hold, dual_ma, signal_threshold
from hexbroker.evaluation.metrics import MetricsReport, compute_metrics
from hexbroker.evaluation.stats import (
    aggregate_metrics,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)


def _equity(n=100, drift=0.001, seed=0) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    eq = 100 * np.cumprod(1 + rets)
    return pd.Series(eq, index=pd.date_range("2024-01-01", periods=n, freq="B"))


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------
def test_compute_metrics_positive_equity():
    eq = _equity(drift=0.001)
    m = compute_metrics(eq, freq="1d")
    assert m.total_return > 0
    assert m.sharpe > 0
    assert m.sortino > 0
    assert m.max_drawdown <= 0  # 回撤为负（跌幅）
    assert m.final_equity == pytest.approx(eq.iloc[-1])
    assert m.n_bars == len(eq) - 1  # pct_change 后样本数


def test_compute_metrics_single_sample_guard():
    eq = pd.Series([100.0], index=[pd.Timestamp("2024-01-02")])
    m = compute_metrics(eq)
    assert m.final_equity == 100.0
    assert m.total_return == 0.0


def test_compute_metrics_loss_clamps_annual_return():
    # 权益归零（-100%）：annual_return 钳制为 -1，不产生复数
    eq = pd.Series([100.0, 0.0], index=pd.date_range("2024-01-01", periods=2))
    m = compute_metrics(eq, freq="1d")
    assert m.total_return == pytest.approx(-1.0)
    assert m.annual_return == pytest.approx(-1.0)


def test_metrics_report_to_dict_has_all_keys():
    d = MetricsReport().to_dict()
    for k in ["total_return", "annual_return", "sharpe", "max_drawdown", "n_bars", "final_equity"]:
        assert k in d


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def test_aggregate_metrics_empty():
    assert aggregate_metrics([]) == {}


def test_aggregate_metrics_mean_and_std():
    reports = [MetricsReport(sharpe=1.0, total_return=0.1), MetricsReport(sharpe=3.0, total_return=0.3)]
    out = aggregate_metrics(reports)
    assert out["sharpe"] == pytest.approx(2.0)
    assert out["sharpe_std"] == pytest.approx(1.0)  # np.std ddof=0
    assert out["total_return"] == pytest.approx(0.2)


def test_deflated_sharpe_ratio():
    assert deflated_sharpe_ratio(1.0, n_obs=1) == 0.0  # 样本不足
    p_high = deflated_sharpe_ratio(3.0, n_obs=100)
    p_low = deflated_sharpe_ratio(0.1, n_obs=100)
    assert p_high > 0.9 > p_low
    # 策略数越多惩罚越重
    assert deflated_sharpe_ratio(1.0, 100, n_strategies=50) < deflated_sharpe_ratio(1.0, 100, n_strategies=1)


def test_pbo_ratio():
    assert probability_of_backtest_overfitting([1.0, 1.0], [0.0, 0.0]) == 1.0
    assert probability_of_backtest_overfitting([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert probability_of_backtest_overfitting([], []) == 0.0


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------
def _prices(n=60) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({"close": close}, index=idx)
    df.index = pd.MultiIndex.from_arrays([np.repeat("SHFE.au", n), idx], names=["symbol", "datetime"])
    return df


def test_buy_hold_target_constant():
    out = buy_hold(_prices())
    assert (out["target"] == 1.0).all()
    assert out.index.names == ["symbol", "datetime"]


def test_dual_ma_warmup_zero_then_signed():
    out = dual_ma(_prices(), fast=3, slow=8)
    tgt = out["target"]
    # 慢线未就绪（前 7 根）目标为 0
    assert (tgt.iloc[:7] == 0).all()
    # 就绪后为 ±1
    assert set(tgt.iloc[7:].unique()) <= {1.0, -1.0}


def test_signal_threshold_direction():
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame({"p_up": [0.8, 0.5, 0.2]}, index=idx)
    df.index = pd.MultiIndex.from_arrays([np.repeat("SHFE.au", 3), idx], names=["symbol", "datetime"])
    out = signal_threshold(df, long_thr=0.55, short_thr=0.45)
    assert list(out["target"]) == [1.0, 0.0, -1.0]
