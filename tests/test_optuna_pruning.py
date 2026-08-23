"""V2 回归：Optuna 剪枝必须真正生效（原 HyperbandPruner 因 objective 从不 report 而失效）。"""

from __future__ import annotations

import optuna

from hexbroker.evolution.optuna_engine import default_forecast_objective


def test_forecast_objective_prunes_below_median():
    # 用真实 MedianPruner + 递减价值，证明 report/should_prune 已接入并实际剪枝
    pruner = optuna.pruners.MedianPruner(n_startup_trials=1, n_warmup_steps=0, interval_steps=1)
    study = optuna.create_study(pruner=pruner, direction="maximize")

    def eval_fn(params, n):
        # 价值随 trial 编号递减 → 后期 trial 必低于已完成中位数 → 被剪枝
        da = max(0.30, 0.99 - 0.05 * n)
        return {"dir_acc": da, "rank_ic": 0.1, "brier": 0.2}

    study.optimize(
        lambda t: default_forecast_objective(t, eval_fn, budget=3),
        n_trials=10,
        show_progress_bar=False,
    )
    pruned = [tr for tr in study.trials if tr.state == optuna.trial.TrialState.PRUNED]
    # V2 修复判据：至少剪掉 1 个 trial —— 证明剪枝链路已生效
    assert len(pruned) > 0, "剪枝未生效：objective 未接入 trial.report/should_prune"
    # 最好的 trial 仍被保留且价值最高（剪枝不得丢失最优解）
    assert study.best_value >= 0.8
