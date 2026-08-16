"""T05 进化层冒烟测试：Optuna（可续跑）+ EXAMM 神经进化（<1M 参数）。"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

from hexbroker.evolution.drift import DriftDetector, psi
from hexbroker.evolution.examm_engine import evolve
from hexbroker.evolution.optuna_engine import (
    run_forecast_optimization,
    run_rl_optimization,
)


def _tmp_storage(name: str) -> str:
    d = tempfile.mkdtemp(prefix="optuna_test_")
    return f"sqlite:///{os.path.join(d, name)}"


def _eval_fn(params, trial_number):
    return {
        "dir_acc": 0.5 + 0.05 * np.sin(trial_number),
        "rank_ic": 0.05,
        "brier": 0.24,
    }


def _rl_eval_fn(params, trial_number):
    return {"sharpe": 0.1 + trial_number * 0.01, "win_rate": 0.52, "max_drawdown": -0.05}


def test_optuna_forecast_optimization_runs_and_resumable():
    storage = _tmp_storage("forecast.db")
    s1 = run_forecast_optimization(_eval_fn, n_trials=2, storage=storage, study_name="f1", seed=1)
    assert len(s1.trials) >= 2
    assert s1.best_value > 0.5
    # 续跑（load_if_exists）：trial 数继续增长而非重置
    s2 = run_forecast_optimization(_eval_fn, n_trials=2, storage=storage, study_name="f1", seed=1)
    assert len(s2.trials) >= 4


def test_optuna_rl_nsga2_multiobjective():
    storage = _tmp_storage("rl.db")
    s = run_rl_optimization(_rl_eval_fn, n_trials=3, storage=storage, study_name="r1", seed=1)
    assert len(s.directions) == 2  # max(Sharpe, win_rate)
    assert len(s.trials) >= 3


def test_examm_evolves_small_network_under_limit():
    rng = np.random.default_rng(0)
    n = 60
    X = rng.normal(0, 1, (n, 6, 3))
    y = np.sign(X[:, -1, 0]) + rng.normal(0, 0.1, n)
    tr = slice(0, 42)
    va = slice(42, 60)
    res = evolve(
        X[tr], y[tr], X[va], y[va],
        n_generations=3, n_islands=2, pop_size=4, in_dim=3, out_dim=1, seed=2,
        max_params=1_000_000,
    )
    assert res.best is not None
    assert res.n_generations == 3
    assert 0 < res.n_params < 1_000_000
    assert len(res.history) == 3
    assert res.best.cell in ("delta_rnn", "gru", "lstm", "mgu", "ugrnn")


def test_examm_covers_multiple_cell_types():
    from hexbroker.evolution.examm_engine import CELLS

    assert len(set(CELLS)) >= 3  # R6：记忆单元覆盖 ≥3 种


def test_psi_detects_distribution_shift():
    base = np.random.default_rng(1).normal(0, 1, 1000)
    shifted = base + 3.0
    assert psi(shifted, base) > 0.25
    same = np.random.default_rng(2).normal(0, 1, 1000)
    assert psi(same, base) < 0.10


def test_drift_detector_triggers_on_shift():
    import pandas as pd

    rng = np.random.default_rng(3)
    n = 400
    ts = pd.date_range("2020-01-01", periods=n, freq="D")
    v = np.concatenate([rng.normal(0, 1, 200), rng.normal(2.5, 1, 200)])
    df = pd.DataFrame({"f_signal": v, "vol_hat": np.abs(v)}, index=ts)
    det = DriftDetector(threshold=0.2, baseline_len=100)
    events = det.detect(df)
    assert det.any_triggered(events) is True

    v2 = rng.normal(0, 1, n)
    df2 = pd.DataFrame({"f_signal": v2, "vol_hat": np.abs(v2)}, index=ts)
    events2 = det.detect(df2)
    assert det.any_triggered(events2) is False
