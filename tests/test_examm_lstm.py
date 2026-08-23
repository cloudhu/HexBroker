"""V1 回归：LSTM 必须跨时间步携带单元状态；进化按验证集适应度选种。"""

from __future__ import annotations

import numpy as np
import pytest

from hexbroker.evolution.examm_engine import (
    RNNGenome,
    _cell_forward,
    _fitness_fn,
    _init_rnn_params,
    evolve,
)


def test_lstm_carries_cell_state_across_timesteps():
    """修复后 LSTM 跨步携带 c；输出必须不同于'每步重置 c'（单元记忆已生效）。"""
    rng = np.random.default_rng(0)
    p = _init_rnn_params("lstm", 3, 4, rng)
    T = 6
    x_seq = rng.normal(0, 1, (T, 3))
    h0 = np.zeros(4)
    outs, _ = _cell_forward("lstm", p, x_seq, h0)
    # 参考：每步重置 c（旧缺陷实现）
    h = h0
    c = np.zeros_like(h)
    ref = []
    for t in range(T):
        x = x_seq[t]
        c = np.zeros_like(h)
        f = 1 / (1 + np.exp(-(x @ p["Wf"] + h @ p["Uf"] + p["bf"])))
        i = 1 / (1 + np.exp(-(x @ p["Wi"] + h @ p["Ui"] + p["bi"])))
        o = 1 / (1 + np.exp(-(x @ p["Wo"] + h @ p["Uo"] + p["bo"])))
        cc = np.tanh(x @ p["Wc"] + h @ p["Uc"] + p["bc"])
        c = f * c + i * cc
        h = o * np.tanh(c)
        ref.append(h)
    ref = np.stack(ref)
    assert not np.allclose(outs, ref, atol=1e-6)
    # 隐藏态应随步演进（非恒等）
    assert not np.allclose(outs[0], outs[-1], atol=1e-6)


def test_evolve_selects_by_validation_fitness(monkeypatch):
    """全局最优必须以验证集适应度裁决（防训练集过拟合选种）。

    monkeypatch 使训练适应度 = s、验证适应度 = -s（取反，便于区分选种口径）。
    修复后 best = 验证最优精英 = s 最小者；旧实现（按训练选）会选 s 最大者 → 本断言失败。
    """
    import hexbroker.evolution.examm_engine as E

    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (8, 5, 3))
    y = np.zeros(8)
    X_val = rng.normal(0, 1, (8, 5, 3))
    y_val = np.zeros(8)
    elite_records = []  # (s, val)

    def _fit(g, Xa, ya):
        s = float(sum(float(p.sum()) for p in g.params.values()))
        if Xa is X:
            return s  # 训练适应度
        v = -s  # 验证适应度 = 训练取反
        elite_records.append((s, v))
        return v

    monkeypatch.setattr(E, "_fitness_fn", _fit)
    res = evolve(
        X, y, X_val, y_val, n_generations=4, n_islands=3, pop_size=4,
        in_dim=3, out_dim=1, seed=0,
    )
    assert res.best is not None
    max_v = max(v for _, v in elite_records)
    expected_s = -max_v
    best_s = float(sum(float(p.sum()) for p in res.best.params.values()))
    assert best_s == pytest.approx(expected_s, abs=1e-6)
    # best_fitness 应为其验证适应度（= -best_s，因 val=-s）
    assert res.best_fitness == pytest.approx(-best_s, abs=1e-6)
    # history 非空且长度=代数
    assert len(res.history) == 4
    assert all(np.isfinite(h) for h in res.history)
