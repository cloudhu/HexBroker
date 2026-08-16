"""LightGBM 精进脚本的纯函数单测（不含重 walk-forward，确保快速）。

覆盖：gate1 指标计算、特征重要性反扁平化聚合、Optuna 参数边界。
完整 walk-forward 集成由 scripts/refine_lightgbm_champion.py 端到端完成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import refine_lightgbm_champion as R


def test_compute_gate1_perfect():
    n = 200
    rng = np.random.default_rng(0)
    p_up = rng.uniform(0.55, 0.9, n)
    realized = np.where(p_up > 0.5, 1.0, -1.0) + rng.normal(0, 0.01, n)
    sig = pd.DataFrame({
        "p_up": p_up,
        "exp_ret": p_up - 0.5,
        "is_effective": np.ones(n, dtype=bool),
        "realized": realized,
    })
    g = R.compute_gate1(sig)
    assert g["direction_accuracy"] > 0.95
    assert g["coverage"] == 1.0
    assert g["n_oos"] == n


def test_compute_gate1_random_is_chance():
    n = 2000
    rng = np.random.default_rng(1)
    p_up = rng.uniform(0.0, 1.0, n)
    realized = rng.choice([-1.0, 1.0], n).astype(float)
    sig = pd.DataFrame({
        "p_up": p_up,
        "exp_ret": rng.normal(0, 0.1, n),
        "is_effective": np.ones(n, dtype=bool),
        "realized": realized,
    })
    g = R.compute_gate1(sig)
    # 随机 p_up 对随机方向，方向准确率应接近 50%
    assert abs(g["direction_accuracy"] - 0.5) < 0.08


def test_aggregate_importances_unflatten():
    # 构造：2 个特征，lookback=3 -> 扁平化 6 维
    feat_names = ["f_a", "f_b"]
    lookback = 3
    n_feat = len(feat_names)
    R._lookback_cache["lb"] = lookback
    # 每折 importance：把全部权重放在 (f_a, lag=0) 与 (f_b, lag=2)
    # 扁平化：j = time_off*n_feat + feat_i；lag = (lookback-1)-time_off
    # (f_a, lag=0) -> time_off=2 -> j = 2*2+0 = 4
    # (f_b, lag=2) -> time_off=0 -> j = 0*2+1 = 1
    imp = np.zeros(lookback * n_feat)
    imp[4] = 1.0  # f_a lag0
    imp[1] = 1.0  # f_b lag2
    wf = R.WFResult(records=[], importances=[("ag0", 0, imp)], feat_names=feat_names)
    out = R.aggregate_importances(wf)
    ranked = {r["feature"]: r["importance"] for r in out["ranked_features"]}
    assert abs(ranked["f_a"] - 0.5) < 1e-9
    assert abs(ranked["f_b"] - 0.5) < 1e-9
    top = {(t["feature"], t["lag"]) for t in out["top_feature_lag"]}
    assert ("f_a", 0) in top
    assert ("f_b", 2) in top


def test_suggest_params_keys():
    import optuna

    captured = {}

    def fake_objective(trial):
        captured.update(R.suggest_params(trial))
        return 0.0

    study = optuna.create_study(direction="maximize")
    study.optimize(fake_objective, n_trials=1)
    expected = {
        "lgbm_n_estimators", "lgbm_lr", "lgbm_max_depth", "lgbm_num_leaves",
        "lgbm_min_child_samples", "lgbm_subsample", "lgbm_colsample_bytree",
        "lgbm_reg_lambda", "lgbm_reg_alpha",
    }
    assert set(captured.keys()) == expected
    assert 100 <= captured["lgbm_n_estimators"] <= 600
    assert 0.01 <= captured["lgbm_lr"] <= 0.1
