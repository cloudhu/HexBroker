"""E4 回归：walk-forward 聚合须含 profit_factor；_std 用 ddof=1；短折不年化。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hexbroker.backtest.walkforward import WalkForwardBacktester
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics


def _make_frame(n_bars=60, n_symbols=1, seed=0):
    rng = np.random.default_rng(seed)
    symbols = [f"S{i}" for i in range(n_symbols)]
    dates = pd.date_range("2020-01-01", periods=n_bars, freq="D")
    idx = pd.MultiIndex.from_product([symbols, dates])
    px = rng.uniform(90, 110, size=len(idx))
    prices = pd.DataFrame({"close": px}, index=idx)
    # 目标仓位：随机 ±1
    tgt = rng.choice([-1.0, 0.0, 1.0], size=len(idx))
    targets = pd.DataFrame({"target": tgt}, index=idx)
    return prices, targets


def test_aggregate_includes_profit_factor_and_ddof1():
    cfg = load_config()
    prices, targets = _make_frame(n_bars=60, n_symbols=1)
    wf = WalkForwardBacktester(cfg)
    res = wf.run(prices, targets, n_folds=3)
    agg = res["aggregate"]
    assert "profit_factor" in agg, "聚合缺 profit_factor（E1 已加字段，E4 须聚合）"
    assert "profit_factor_std" in agg

    # 验证 _std 使用 ddof=1（而非 ddof=0）—— 用始终有变化的 total_return 验证
    reports = res["reports"]
    total_returns = np.array([r.total_return for r in reports])
    if total_returns.size >= 2:
        expected_std = float(total_returns.std(ddof=1))
        assert abs(agg["total_return_std"] - expected_std) < 1e-9, "total_return_std 未用 ddof=1"
    # 年化类指标仅对足够长(>=20bar)的折聚合
    assert "annual_return" in agg
    assert "sharpe_std" in agg


def test_short_fold_not_annualized():
    # 3 bar 短序列：annualize=False 时年化指标为 0，且不触发大数
    eq = pd.Series([100.0, 101.0, 99.0])
    rep = compute_metrics(eq, freq="1d", annualize=False)
    assert rep.annual_return == 0.0
    assert rep.sharpe == 0.0
    assert np.isfinite(rep.total_return)
    assert np.isfinite(rep.max_drawdown)
