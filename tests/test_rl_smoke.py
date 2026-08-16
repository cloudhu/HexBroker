"""T04 RL 冒烟测试：环境自检 + PPO 训练奖励上升。"""

from __future__ import annotations

import numpy as np
import pytest

from _helpers import fast_cfg, make_prices, make_signals
from hexbroker.rl.agent import MLPPolicy, train_ppo
from hexbroker.rl.futures_env import FuturesTradingEnv, check_env
from hexbroker.rl.train import policy_rollout


def _env(cfg, n_bars=200, seed=9):
    prices = make_prices(n_bars=n_bars, seed=seed)
    signals = make_signals(prices, seed=seed + 1, strength=0.2)
    return FuturesTradingEnv(signals, prices, cfg)


def test_env_check_passes():
    cfg = fast_cfg()
    env = _env(cfg)
    assert check_env(env) is True
    assert env.obs_dim == cfg.rl.obs_window * 6 + 4
    assert env.action_space.n == 5


def test_env_obs_contains_risk_state():
    cfg = fast_cfg()
    env = _env(cfg)
    obs = env.reset()
    # 末尾 4 维 = [position_frac, drawdown, stage_scalar, veto]
    assert obs[-4:].shape == (4,)
    assert np.isfinite(obs).all()


def test_ppo_training_reward_improves():
    cfg = fast_cfg()
    cfg.rl.total_timesteps = 0  # 由显式参数控制
    env = _env(cfg, n_bars=160)
    policy, stats = train_ppo(env, total_timesteps=2400, hidden=16, n_hidden=1, seed=42)
    assert stats.total_timesteps >= 2400
    progress = stats.extra.get("reward_progress", 0.0)
    # PPO 应在训练后半段获得不低于前半段的平均奖励
    assert progress > -0.05, f"PPO 奖励未改善：progress={progress}"


def test_ppo_policy_rollout_returns_target_frame():
    cfg = fast_cfg()
    env = _env(cfg, n_bars=160)
    policy, _ = train_ppo(env, total_timesteps=1200, hidden=16, n_hidden=1, seed=1)
    tf = policy_rollout(policy, env)
    assert "target" in tf.columns
    assert len(tf) > 0


def test_mlp_policy_param_count_small():
    p = MLPPolicy(obs_dim=184, n_actions=5, hidden=16, n_hidden=1, seed=0)
    assert p.n_params() < 50_000
    logits, value, _ = p.forward(np.zeros((3, 184)))
    assert logits.shape == (3, 5)
    assert value.shape == (3, 1)
