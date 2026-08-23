"""PPO 策略梯度回归测试（P1 actor 空操作 + P2 双乘 ratio）。"""

from __future__ import annotations

import numpy as np

from hexbroker.rl.agent import _log_softmax, _ppo_policy_grad_logits


def _pg_loss(logits_flat, n, nA, actions, adv, old_logp, clip_range, ent_coef=0.0):
    """PPO clipped 策略损失（不含 value），用于有限差分对照。"""
    logits = logits_flat.reshape(n, nA)
    logp = _log_softmax(logits)
    ratio = np.exp(logp[np.arange(n), actions] - old_logp)
    clip_hi = np.clip(ratio, 1 - clip_range, 1 + clip_range)
    pg = -np.minimum(ratio * adv, clip_hi * adv)
    probs = np.exp(logp)
    ent = -np.sum(probs * logp, axis=1)
    return float((pg - ent_coef * ent).sum() / n)


def test_policy_grad_matches_finite_diff():
    """解析梯度（ent_coef=0）须与有限差分一致 → 验证 P1/P2 修正正确。"""
    rng = np.random.default_rng(0)
    n, nA = 8, 4
    logits0 = rng.normal(0.0, 0.5, (n, nA))
    actions = rng.integers(0, nA, n)
    adv = rng.normal(0.0, 1.0, n)
    old_logp = rng.normal(0.0, 0.5, n)
    clip_range = 0.2

    logp0 = _log_softmax(logits0)
    ratio = np.exp(logp0[np.arange(n), actions] - old_logp)
    clip_hi = np.clip(ratio, 1 - clip_range, 1 + clip_range)

    analytic = _ppo_policy_grad_logits(
        logits0, actions, adv, ratio, clip_hi, n, ent_coef=0.0
    )

    eps = 1e-6
    fd = np.zeros_like(logits0)
    for i in range(n):
        for j in range(nA):
            lp = logits0.copy()
            lp[i, j] += eps
            lm = logits0.copy()
            lm[i, j] -= eps
            fd[i, j] = (
                _pg_loss(lp, n, nA, actions, adv, old_logp, clip_range)
                - _pg_loss(lm, n, nA, actions, adv, old_logp, clip_range)
            ) / (2 * eps)

    assert np.allclose(analytic, fd, atol=1e-4)


def test_policy_grad_not_uniform_across_actions():
    """P1 回归：策略梯度在动作维度必须非均匀，否则 softmax 平移不变 → actor 不学习。"""
    rng = np.random.default_rng(1)
    n, nA = 6, 3
    logits0 = rng.normal(0.0, 0.5, (n, nA))
    actions = np.array([0, 1, 2, 0, 1, 2])
    adv = np.array([0.5, -0.3, 0.8, -0.1, 0.2, -0.4])

    logp0 = _log_softmax(logits0)
    # 令 old_logp == 真实旧 logp → ratio 恒为 1.0，全部落在 clip 带内（未触发 clip）
    old_logp = logp0[np.arange(n), actions].copy()
    ratio = np.ones(n)
    clip_hi = np.clip(ratio, 0.8, 1.2)  # = 1.0

    g = _ppo_policy_grad_logits(logits0, actions, adv, ratio, clip_hi, n, ent_coef=0.0)
    for i in range(n):
        # 未触发 clip 时 d_logp = -adv != 0，梯度应为 (onehot-probs)*d_logp → 动作维度非恒定
        assert np.std(g[i]) > 1e-9, f"样本 {i} 策略梯度在动作维度全相等（空操作）"
