"""RL 训练入口（§3.3 / §8.6）：根据配置选择内置 numpy PPO 或 SB3。

``train`` 返回 ``(policy_or_model, stats, env)``。
训练后可通过 ``policy_rollout`` 用最终策略在环境里回放，产出目标合约数帧供回测一致性校验。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .agent import train_ppo
from .sb3_wrap import sb3_available, train_sb3


def train(env: Any, cfg: Any, total_timesteps: Optional[int] = None) -> tuple[Any, dict, Any]:
    """训练 RL 智能体。返回 (policy_or_model, stats_dict, env)。"""
    rlc = cfg.rl
    use_sb3 = bool(getattr(rlc, "use_sb3", False))
    n = total_timesteps or int(getattr(rlc, "total_timesteps", 50_000))

    if use_sb3 and sb3_available():
        model, stats = train_sb3(env, cfg, total_timesteps=n)
        stats["engine"] = "sb3"
        return model, stats, env

    policy, tstats = train_ppo(
        env,
        total_timesteps=n,
        lr=float(getattr(rlc, "learning_rate", 3e-3)),
        gamma=float(getattr(rlc, "gamma", 0.99)),
        clip_range=float(getattr(rlc, "clip_range", 0.2)),
        hidden=int(getattr(rlc, "hidden_size", 32)),
        n_hidden=int(getattr(rlc, "n_hidden", 2)),
        seed=int(getattr(cfg, "seed", 42)),
    )
    stats = tstats.to_dict()
    stats["engine"] = "numpy_ppo"
    return policy, stats, env


def policy_rollout(policy: Any, env: Any, greedy: bool = True) -> pd.DataFrame:
    """用训练好的策略在环境中贪婪回放，返回目标合约数帧（供回测引擎重放）。"""
    obs = np.asarray(env.reset(), dtype=np.float64)
    done = False
    while not done:
        if hasattr(policy, "act"):  # numpy PPO
            act, _, _ = policy.act(obs[None, :], greedy=greedy)
            a = int(act[0])
        else:  # SB3 model
            a = int(policy.predict(obs, deterministic=greedy)[0])
        obs, _, done, _ = env.step(a)
        obs = np.asarray(obs, dtype=np.float64)
    return env.target_frame
