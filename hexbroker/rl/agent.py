"""纯 numpy PPO（CPU 沙箱默认 RL 算法，§3.3 / §8.6）。

不依赖 torch / stable-baselines3：actor-critic MLP + GAE + clipped 目标 + minibatch 更新。
解析反向传播（MLP 结构简单，梯度精确且 CPU 友好）。

约束：
- 观测来自 ``FuturesTradingEnv``（只读 OOS 信号，无预测层依赖）；
- 训练统计含 ``ep_rew_mean``（验收：5 万步后应上升）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Actor-Critic MLP（参数为 numpy 数组列表，便于进化层复用）
# ---------------------------------------------------------------------------
class MLPPolicy:
    """离散动作 actor-critic（PPO 用）。参数布局：trunk W0,b0,W1,b1,… Wa,ba, Wv,bv。"""

    def __init__(
        self,
        obs_dim: int,
        n_actions: int,
        hidden: int = 32,
        n_hidden: int = 2,
        seed: int = 42,
    ) -> None:
        self.obs_dim = obs_dim
        self.n_actions = n_actions
        rng = np.random.default_rng(seed)
        self.params: list[np.ndarray] = []
        dims = [obs_dim] + [hidden] * n_hidden
        for i in range(len(dims) - 1):
            self.params.append(rng.normal(0, 0.1, (dims[i], dims[i + 1])))  # W
            self.params.append(rng.normal(0, 0.1, (dims[i + 1],)))  # b
        self.params.append(rng.normal(0, 0.1, (dims[-1], n_actions)))  # Wa
        self.params.append(rng.normal(0, 0.1, (n_actions,)))  # ba
        self.params.append(rng.normal(0, 0.1, (dims[-1], 1)))  # Wv
        self.params.append(rng.normal(0, 0.1, (1,)))  # bv
        self.hidden = hidden
        self.n_hidden = n_hidden

    # ---- 前向 ----
    def forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
        """返回 (logits, value, pre_act 缓存)。x: (N, obs_dim)。"""
        h = x
        pre: list[np.ndarray] = []
        idx = 0
        for _ in range(self.n_hidden):
            W = self.params[idx]
            b = self.params[idx + 1]
            idx += 2
            z = h @ W + b
            pre.append(z)
            h = np.maximum(0.0, z)
        Wa = self.params[idx]
        ba = self.params[idx + 1]
        Wv = self.params[idx + 2]
        bv = self.params[idx + 3]
        logits = h @ Wa + ba
        value = h @ Wv + bv
        return logits, value, pre

    def act(self, x: np.ndarray, greedy: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """采样动作。返回 (actions, log_probs, values)。x: (N, obs_dim)。"""
        logits, value, _ = self.forward(x)
        logp = _log_softmax(logits)
        probs = np.exp(logp)
        if greedy:
            actions = np.argmax(probs, axis=1)
        else:
            cum = np.cumsum(probs, axis=1)
            r = np.random.random((len(probs), 1))
            actions = (r > cum).sum(axis=1)
            actions = np.clip(actions, 0, self.n_actions - 1)
        a_logp = np.array([logp[i, a] for i, a in enumerate(actions)])
        return actions, a_logp, value[:, 0]

    def value(self, x: np.ndarray) -> np.ndarray:
        _, value, _ = self.forward(x)
        return value[:, 0]

    def n_params(self) -> int:
        return int(sum(p.size for p in self.params))

    # ---- 反向传播（返回梯度列表，与 params 对齐） ----
    def grads(self, x: np.ndarray, d_logits: np.ndarray, d_value: np.ndarray) -> list[np.ndarray]:
        """d_logits/d_value 形状与 forward 输出一致。返回梯度（累加，未除以 N）。"""
        logits, _, pre = self.forward(x)
        N = x.shape[0]
        grads: list[np.ndarray] = [np.zeros_like(p) for p in self.params]
        dv = d_value if d_value.ndim == 2 else d_value[:, None]  # (N,1)

        # 输出头
        idx = 2 * self.n_hidden
        Wa, ba = self.params[idx], self.params[idx + 1]
        Wv, bv = self.params[idx + 2], self.params[idx + 3]
        grads[idx] = pre[-1].T @ d_logits
        grads[idx + 1] = d_logits.sum(axis=0)
        grads[idx + 2] = pre[-1].T @ dv
        grads[idx + 3] = dv.sum(axis=0)

        # 共享主干反向
        dh = d_logits @ Wa.T + dv @ Wv.T
        for l in reversed(range(self.n_hidden)):
            z = pre[l]
            dz = dh * (z > 0)
            W = self.params[2 * l]
            b = self.params[2 * l + 1]
            if l == 0:
                grads[2 * l] = x.T @ dz
            else:
                grads[2 * l] = pre[l - 1].T @ dz
            grads[2 * l + 1] = dz.sum(axis=0)
            dh = dz @ W.T
        return grads


def _log_softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return z - np.log(e.sum(axis=1, keepdims=True))


# ---------------------------------------------------------------------------
# PPO 训练
# ---------------------------------------------------------------------------
@dataclass
class TrainStats:
    """训练统计。"""

    total_timesteps: int = 0
    ep_rew_mean: float = 0.0
    ep_len_mean: float = 0.0
    mean_reward: float = 0.0
    policy_loss: float = 0.0
    value_loss: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_timesteps": self.total_timesteps,
            "ep_rew_mean": self.ep_rew_mean,
            "ep_len_mean": self.ep_len_mean,
            "mean_reward": self.mean_reward,
            "policy_loss": self.policy_loss,
            "value_loss": self.value_loss,
        }


def _ppo_policy_grad_logits(
    logits: np.ndarray,
    actions: np.ndarray,
    adv: np.ndarray,
    ratio: np.ndarray,
    clip_hi: np.ndarray,
    n: int,
    ent_coef: float = 0.01,
) -> np.ndarray:
    """计算 PPO clipped 目标对 logits 的梯度 dL/dlogits，形状 (N, A)。

    策略梯度（REINFORCE/PPO 标准式）：
        dL/dlogits_j = d_logp * (onehot(a)_j - p_j)
    其中 d_logp = dL/dlogp = (dL/dratio) * (dratio/dlogp)，而 dratio/dlogp = ratio，
    且 dL/dratio = -adv * take（take 表示未触发 clip 的那一项被取）。

    ⚠️ 历史 bug（P1+P2）：
      - P2：dL/dratio 曾被写成 ``-adv*take*ratio``（多乘一次 ratio），导致 actor
        梯度多出 ratio 因子；
      - P1：d_logp 是每样本标量却被 ``[:,None]`` 等值广播到全部 logit → softmax
        对常数平移不变 → actor 代理梯度近似空操作（策略不学习）。
    这里用 ``(a_onehot - probs) * d_logp[:,None]`` 给出正确的非均匀策略梯度。
    """
    logp = _log_softmax(logits)
    probs = np.exp(logp)
    take = (ratio * adv) <= (clip_hi * adv)
    d_r = -adv * take               # dL/dratio（P2 修复：不含多余 ratio）
    d_logp = d_r * ratio            # dL/dlogp = dL/dratio * dratio/dlogp，dratio/dlogp = ratio
    a_onehot = np.zeros_like(logits)
    a_onehot[np.arange(n), actions] = 1.0
    d_ent = probs * (logp + 1.0)    # 熵项 dL/dlogits（ent_coef 在调用处乘）
    d_logits = (a_onehot - probs) * d_logp[:, None] / n + ent_coef * d_ent / n
    return d_logits


def _rollout(env: Any, policy: MLPPolicy, gamma: float, lam: float) -> dict:
    """收集一条完整轨迹（单环境）。"""
    obs = np.asarray(env.reset(), dtype=np.float64)
    states: list[np.ndarray] = []
    actions: list[int] = []
    rewards: list[float] = []
    values: list[float] = []
    dones: list[bool] = []
    done = False
    while not done:
        act, a_logp, v = policy.act(obs[None, :])
        states.append(obs)
        actions.append(int(act[0]))
        values.append(float(v[0]))
        obs, rew, done, _ = env.step(actions[-1])
        obs = np.asarray(obs, dtype=np.float64)
        rewards.append(float(rew))
        dones.append(bool(done))
        if "old_logp" not in locals():
            old_logp: list[float] = []
        old_logp.append(float(a_logp[0]))
    # GAE
    n = len(rewards)
    advs = np.zeros(n)
    gae = 0.0
    v_next = 0.0
    for t in reversed(range(n)):
        delta = rewards[t] + gamma * v_next * (1 - int(dones[t])) - values[t]
        gae = delta + gamma * lam * (1 - int(dones[t])) * gae
        advs[t] = gae
        v_next = values[t]
    returns = advs + np.asarray(values)
    return {
        "states": np.stack(states),
        "actions": np.asarray(actions),
        "rewards": np.asarray(rewards),
        "values": np.asarray(values),
        "advs": advs,
        "returns": returns,
        "old_logp": np.asarray(old_logp),
    }


def train_ppo(
    env: Any,
    total_timesteps: int = 50_000,
    lr: float = 3e-3,
    gamma: float = 0.99,
    lam: float = 0.95,
    clip_range: float = 0.2,
    epochs: int = 4,
    minibatch: int = 64,
    hidden: int = 32,
    n_hidden: int = 2,
    seed: int = 42,
) -> tuple[MLPPolicy, TrainStats]:
    """训练 PPO。返回 (policy, stats)。"""
    np.random.seed(seed)
    policy = MLPPolicy(env.obs_dim, env.n_actions, hidden=hidden, n_hidden=n_hidden, seed=seed)
    adam = _Adam(lr)

    ep_rews: list[float] = []
    total_steps = 0
    n_updates = 0
    cur_ep_len = 0.0
    p_loss_sum = 0.0
    v_loss_sum = 0.0
    reward_track: list[tuple[int, float]] = []  # (step, reward) 用于学习进度诊断

    while total_steps < total_timesteps:
        roll = _rollout(env, policy, gamma, lam)
        steps = len(roll["rewards"])
        total_steps += steps
        ep_rews.append(float(np.sum(roll["rewards"])))
        cur_ep_len = float(steps)
        for k, r in enumerate(roll["rewards"]):
            reward_track.append((total_steps - steps + k, float(r)))

        adv = roll["advs"]
        adv = (adv - adv.mean()) / (adv.std() + 1e-8)

        idx = np.arange(steps)
        for _ in range(epochs):
            np.random.shuffle(idx)
            for s in range(0, steps, minibatch):
                mb = idx[s : s + minibatch]
                states = roll["states"][mb]
                actions = roll["actions"][mb]
                returns_mb = roll["returns"][mb]
                adv_mb = adv[mb]
                n = len(mb)

                logits, value, _ = policy.forward(states)
                logp = _log_softmax(logits)
                new_logp = np.array([logp[i, a] for i, a in enumerate(actions)])
                old_logp = np.array([roll["old_logp"][mb[i]] for i in range(n)])
                ratio = np.exp(new_logp - old_logp)
                clip_hi = np.clip(ratio, 1 - clip_range, 1 + clip_range)
                # PPO clipped 策略梯度（修正 P1 空操作 + P2 双乘 ratio）
                d_logits = _ppo_policy_grad_logits(
                    logits, actions, adv_mb, ratio, clip_hi, n, ent_coef=0.01
                )
                d_value = (value[:, 0] - returns_mb) / n  # 0.5*v_loss 的导数

                grads = policy.grads(states, d_logits, d_value)
                for gi, (g, p) in enumerate(zip(grads, policy.params)):
                    p -= lr * adam.step(gi, g)
                p_loss_sum += float(np.mean(-np.minimum(ratio * adv_mb, clip_hi * adv_mb)))
                v_loss_sum += float(np.mean((value[:, 0] - returns_mb) ** 2))
                n_updates += 1

    if reward_track:
        steps_arr = np.array([s for s, _ in reward_track])
        rew_arr = np.array([r for _, r in reward_track])
        q = int(max(1, len(rew_arr) // 4))
        extra_progress = {
            "first_quarter_reward": float(rew_arr[:q].mean()),
            "last_quarter_reward": float(rew_arr[-q:].mean()),
            "reward_progress": float(rew_arr[-q:].mean() - rew_arr[:q].mean()),
        }
    else:
        extra_progress = {"reward_progress": 0.0}
    stats = TrainStats(
        total_timesteps=total_steps,
        ep_rew_mean=float(np.mean(ep_rews[-20:])) if ep_rews else 0.0,
        ep_len_mean=cur_ep_len,
        mean_reward=float(np.mean(ep_rews[-20:]) / max(cur_ep_len, 1)) if ep_rews else 0.0,
        policy_loss=p_loss_sum / max(n_updates, 1),
        value_loss=v_loss_sum / max(n_updates, 1),
        extra={"n_episodes": len(ep_rews), "n_params": policy.n_params(), **extra_progress},
    )
    return policy, stats


class _Adam:
    """Adam 优化器（按参数索引维护一阶/二阶矩，避免 id() 地址复用冲突）。"""

    def __init__(self, lr: float = 3e-3, beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-8) -> None:
        self.beta1, self.beta2, self.eps = beta1, beta2, eps
        self.t = 0
        self.m: dict[int, np.ndarray] = {}
        self.v: dict[int, np.ndarray] = {}

    def step(self, key: int, g: np.ndarray) -> np.ndarray:
        self.t += 1
        m = self.m.get(key)
        if m is None:
            m = np.zeros_like(g)
            self.m[key] = m
            self.v[key] = np.zeros_like(g)
        v = self.v[key]
        m = self.beta1 * m + (1 - self.beta1) * g
        v = self.beta2 * v + (1 - self.beta2) * (g * g)
        self.m[key], self.v[key] = m, v
        mhat = m / (1 - self.beta1 ** self.t)
        vhat = v / (1 - self.beta2 ** self.t)
        return mhat / (np.sqrt(vhat) + self.eps)
