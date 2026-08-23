"""Optuna 进化引擎（§3.7 / §8.7 内层 A/B）。

- 内层 A（预测超参）：TPE + Hyperband 剪枝，目标 = 0.5·有效信号方向准确率 + 0.3·RankIC + 0.2·(1-Brier)，
  可选 ``sharpe_aware`` 变体（R8，把 Sharpe 纳入训练目标）。
- 内层 B（RL 奖励权重）：NSGA-II 多目标，max(Sharpe, 交易胜率) s.t. MaxDD ≤ 阈值。
- sqlite storage 支持 Ctrl-C 断点续跑。

为保持 CPU 沙箱可运行，所有 objective 均轻量化（小样本快速评估）。
"""

from __future__ import annotations

import os
from typing import Callable

import numpy as np
import optuna


def _ensure_storage_dir(storage: str) -> str:
    """sqlite 存储的父目录必须存在，否则 sqlite 无法建库。"""
    if storage.startswith("sqlite:///"):
        rel = storage[len("sqlite:///"):]
        d = os.path.dirname(rel)
        if d:
            os.makedirs(d, exist_ok=True)
    return storage


def default_forecast_objective(
    trial: optuna.Trial,
    eval_fn: Callable[[dict, int], dict],
    budget: int = 3,
) -> float:
    """内层 A：预测超参目标。

    eval_fn(params, trial_number) 返回 {"dir_acc", "rank_ic", "brier", "effective_acc", "coverage"}。
    """
    params = {
        "temperature": trial.suggest_float("temperature", 0.5, 1.5),
        "top_p": trial.suggest_float("top_p", 0.7, 1.0),
        "n_mc_samples": trial.suggest_int("n_mc_samples", 8, 40),
        "effective_threshold": trial.suggest_float("effective_threshold", 0.03, 0.10),
    }
    res = eval_fn(params, trial.number)
    da = float(res.get("dir_acc", 0.5))
    ric = float(res.get("rank_ic", 0.0))
    brier = float(res.get("brier", 0.25))
    value = 0.5 * da + 0.3 * (ric * 0.5 + 0.5) + 0.2 * (1.0 - brier)
    # V2 修复：report 单步终值并令剪枝器可实际裁掉低于中位数的 trial
    trial.report(value, step=0)
    if trial.should_prune():
        raise optuna.TrialPruned()
    return value


def forecast_sharpe_aware_objective(
    trial: optuna.Trial,
    eval_fn: Callable[[dict, int], dict],
    budget: int = 3,
) -> float:
    """内层 A 变体（R8）：把回测 Sharpe 纳入目标。"""
    params = {
        "temperature": trial.suggest_float("temperature", 0.5, 1.5),
        "top_p": trial.suggest_float("top_p", 0.7, 1.0),
        "n_mc_samples": trial.suggest_int("n_mc_samples", 8, 40),
        "effective_threshold": trial.suggest_float("effective_threshold", 0.03, 0.10),
    }
    res = eval_fn(params, trial.number)
    da = float(res.get("dir_acc", 0.5))
    sharpe = float(res.get("sharpe", 0.0))
    brier = float(res.get("brier", 0.25))
    value = 0.4 * da + 0.4 * float(np.tanh(sharpe)) + 0.2 * (1.0 - brier)
    # V2 修复：report 单步终值并令剪枝器可实际裁掉低于中位数的 trial
    trial.report(value, step=0)
    if trial.should_prune():
        raise optuna.TrialPruned()
    return value


def run_forecast_optimization(
    eval_fn: Callable[[dict, int], dict],
    n_trials: int = 20,
    storage: str = "sqlite:///artifacts/optuna/forecast_study.db",
    study_name: str = "forecast_hyper",
    seed: int = 42,
    sharpe_aware: bool = False,
) -> optuna.Study:
    """运行内层 A 优化（可断点续跑：storage 已存在则续跑）。"""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    storage = _ensure_storage_dir(storage)
    objective = forecast_sharpe_aware_objective if sharpe_aware else default_forecast_objective
    sampler = optuna.samplers.TPESampler(seed=seed)
    # V2 修复：原 HyperbandPruner 依赖多步 intermediate report，但 forecast objective
    # 为单步单次评估，从不 report/should_prune → 剪枝恒失效。改用 MedianPruner 并对
    # 单步终值 report + should_prune，使低于已完成 trial 中位数的 trial 被实际剪枝。
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=0, interval_steps=1)
    study = optuna.create_study(
        direction="maximize",
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        sampler=sampler,
        pruner=pruner,
    )
    study.optimize(lambda t: objective(t, eval_fn, budget=3), n_trials=n_trials, show_progress_bar=False)
    return study


def run_rl_optimization(
    eval_fn: Callable[[dict, int], dict],
    n_trials: int = 20,
    storage: str = "sqlite:///artifacts/optuna/rl_study.db",
    study_name: str = "rl_reward",
    seed: int = 42,
    max_dd: float = -0.25,
) -> optuna.Study:
    """内层 B：NSGA-II 多目标，max(Sharpe, 交易胜率) s.t. MaxDD ≥ 阈值。"""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    storage = _ensure_storage_dir(storage)
    sampler = optuna.samplers.NSGAIISampler(seed=seed)
    study = optuna.create_study(
        directions=["maximize", "maximize"],
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        sampler=sampler,
    )

    def _obj(trial: optuna.Trial) -> tuple[float, float]:
        params = {
            "w_pnl": trial.suggest_float("w_pnl", 0.5, 3.0),
            "w_dd": trial.suggest_float("w_dd", 0.5, 5.0),
            "w_cost": trial.suggest_float("w_cost", 0.0, 3.0),
            "w_turn": trial.suggest_float("w_turn", 0.0, 0.5),
        }
        res = eval_fn(params, trial.number)
        dd = float(res.get("max_drawdown", 0.0))
        if dd < max_dd:
            return -1.0, 0.0  # 强约束：超出回撤阈值则判负
        return float(res.get("sharpe", 0.0)), float(res.get("win_rate", 0.0))

    study.optimize(_obj, n_trials=n_trials, show_progress_bar=False)
    return study
