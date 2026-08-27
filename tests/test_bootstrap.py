"""P0-4 block bootstrap 测试（PRD A4.1–A4.5）。

覆盖：MC 覆盖率 ∈[0.90, 1.00]、固定 seed 可复现、点估计与 compute_metrics 一致、
性能 <60s、报告块结构、by_symbol 截面。
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from hexbroker.evaluation.bootstrap import (
    block_bootstrap,
    bootstrap_metrics_ci,
    bootstrap_report_block,
)
from hexbroker.evaluation.metrics import compute_metrics


def _equity(seed=1, n=300, mu=0.001, sigma=0.01):
    rng = np.random.default_rng(seed)
    rets = rng.normal(mu, sigma, size=n)
    eq = pd.Series(
        np.cumprod(1.0 + rets),
        index=pd.date_range("2020-01-01", periods=n, freq="D"),
    )
    return eq, rets


# ---------------------------------------------------------------------------
# block_bootstrap 基础
# ---------------------------------------------------------------------------
def test_block_bootstrap_shape_and_seed_reproducible():
    r = np.arange(100, dtype=float)
    b1 = block_bootstrap(r, block_len=20, n_boot=50, seed=1)
    b2 = block_bootstrap(r, block_len=20, n_boot=50, seed=1)
    assert b1.shape == (50, 100)
    assert np.array_equal(b1, b2)


def test_block_bootstrap_preserves_values():
    r = np.array([1.0, 2.0, 3.0])
    b = block_bootstrap(r, block_len=2, n_boot=10, seed=3)
    assert set(np.unique(b)) <= {1.0, 2.0, 3.0}


def test_block_bootstrap_empty_returns_empty():
    b = block_bootstrap(np.array([]), n_boot=10, seed=1)
    assert b.shape == (10, 0)


# ---------------------------------------------------------------------------
# MC 覆盖率验证（A4.1：≈95% 覆盖，容差 [0.90, 1.00]）
# ---------------------------------------------------------------------------
def test_bootstrap_coverage():
    rng = np.random.default_rng(123)
    mu_true = 0.0
    n, n_trials = 120, 100
    covered = 0
    for t in range(n_trials):
        rets = rng.normal(mu_true, 0.01, size=n)
        boots = block_bootstrap(rets, block_len=10, n_boot=200, seed=t)
        means = boots.mean(axis=1)
        lo, hi = np.percentile(means, [2.5, 97.5])
        if lo <= mu_true <= hi:
            covered += 1
    ratio = covered / n_trials
    assert 0.90 <= ratio <= 1.00, f"MC 覆盖率 {ratio} 不在 [0.90, 1.00]"


# ---------------------------------------------------------------------------
# bootstrap_metrics_ci（A4.2 可复现 / A4.3 点估计一致 / A4.4 性能）
# ---------------------------------------------------------------------------
def test_bootstrap_metrics_ci_reproducible():
    eq, _ = _equity(seed=1)
    r1 = bootstrap_metrics_ci(eq, freq="1d", block_len=20, n_boot=200, seed=42)
    r2 = bootstrap_metrics_ci(eq, freq="1d", block_len=20, n_boot=200, seed=42)
    assert r1.to_dict() == r2.to_dict()


def test_bootstrap_metrics_ci_point_matches_compute_metrics():
    eq, _ = _equity(seed=2, n=300)
    res = bootstrap_metrics_ci(eq, freq="1d", block_len=20, n_boot=200, seed=42)
    m = compute_metrics(eq, freq="1d")
    assert abs(res.sharpe.point - m.sharpe) < 1e-12
    assert abs(res.calmar.point - m.calmar) < 1e-12
    assert abs(res.max_drawdown.point - m.max_drawdown) < 1e-12
    # CI 界正确
    assert res.sharpe.ci_low <= res.sharpe.point <= res.sharpe.ci_high


def test_bootstrap_metrics_ci_short_series():
    eq = pd.Series([100.0, 101.0])
    res = bootstrap_metrics_ci(eq, freq="1d", n_boot=50, seed=1)
    assert res.sharpe.point == 0.0  # len<2 收益 → 0


def test_bootstrap_performance_under_60s():
    """默认参数（n_boot=1000）单次运行 <60s（A4.4）。"""
    eq, _ = _equity(seed=3, n=500)
    t0 = time.time()
    bootstrap_metrics_ci(eq, freq="1d", block_len=20, n_boot=1000, seed=7)
    elapsed = time.time() - t0
    assert elapsed < 60, f"bootstrap 耗时 {elapsed:.1f}s ≥60s"


def test_bootstrap_by_symbol():
    n = 100
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    rng = np.random.default_rng(9)
    eqs = []
    for sym in ("A", "B"):
        eqs.append(
            pd.Series(
                np.cumprod(1 + rng.normal(0.0005, 0.01, n)),
                index=pd.MultiIndex.from_product([[sym], dates], names=["symbol", "datetime"]),
            )
        )
    eq = pd.concat(eqs)
    res = bootstrap_metrics_ci(eq, freq="1d", block_len=10, n_boot=100, seed=5, by_symbol=True)
    assert res.by_symbol is not None
    assert set(res.by_symbol.keys()) == {"A", "B"}
    assert "sharpe" in res.by_symbol["A"]
    # 非 MultiIndex 时 by_symbol 被忽略
    eq2, _ = _equity(seed=4, n=100)
    res2 = bootstrap_metrics_ci(eq2, freq="1d", n_boot=50, seed=5, by_symbol=True)
    assert res2.by_symbol is None


def test_bootstrap_report_block():
    eq, _ = _equity(seed=5, n=200)
    res = bootstrap_metrics_ci(eq, freq="1d", block_len=20, n_boot=100, seed=42)
    block = bootstrap_report_block(res)
    assert set(block) == {"sharpe", "calmar", "max_drawdown", "params"}
    for k in ("sharpe", "calmar", "max_drawdown"):
        assert set(block[k]) == {"point", "ci_low", "ci_high"}
    assert set(block["params"]) == {"block_len", "n_boot", "seed", "by_symbol"}
