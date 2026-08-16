"""可选 Stable-Baselines3 适配（§3.3 / §8.6）。

仅当 ``cfg.rl.use_sb3=True`` 且已安装 ``stable-baselines3`` 时启用；
否则使用纯 numpy PPO（``agent.train_ppo``）。两者接口对齐：
``train`` 均返回 ``(policy, stats)``。
"""

from __future__ import annotations

from typing import Any, Optional


def sb3_available() -> bool:
    try:
        import stable_baselines3  # noqa: F401

        return True
    except Exception:
        return False


def train_sb3(env: Any, cfg: Any, total_timesteps: Optional[int] = None) -> tuple[Any, Any]:
    """用 SB3 PPO 训练。返回 (model, stats)。"""
    if not sb3_available():
        raise ImportError("stable-baselines3 未安装，请使用 use_sb3=false（内置 numpy PPO）")
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    rlc = cfg.rl
    n = total_timesteps or int(getattr(rlc, "total_timesteps", 50_000))

    def _make():
        return env

    vec = DummyVecEnv([_make])
    model = PPO(
        "MlpPolicy",
        vec,
        learning_rate=float(getattr(rlc, "learning_rate", 3e-3)),
        gamma=float(getattr(rlc, "gamma", 0.99)),
        clip_range=float(getattr(rlc, "clip_range", 0.2)),
        n_steps=min(max(int(n / 8), 64), 2048),
        seed=int(getattr(cfg, "seed", 42)),
        verbose=0,
    )
    model.learn(total_timesteps=n)
    stats = {"total_timesteps": n, "engine": "sb3", "algo": str(getattr(rlc, "algo", "PPO"))}
    return model, stats
