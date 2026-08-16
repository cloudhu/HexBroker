"""T03/T04 训练-回测一致性测试：同一策略经 env 与 BacktestEngine 权益曲线差 < 1e-6。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from _helpers import fast_cfg, make_prices, make_signals


def _build_env(cfg, prices, signals):
    from hexbroker.rl.futures_env import FuturesTradingEnv

    return FuturesTradingEnv(signals, prices, cfg)


def _scripted_actions(env, seq):
    """固定动作序列跑完环境。返回 (equity_series, target_frame)。"""
    obs = env.reset()
    i = 0
    done = False
    while not done:
        a = seq[i % len(seq)]
        obs, rew, done, info = env.step(a)
        i += 1
    return env.equity_curve, env.target_frame


def test_env_backtest_consistency():
    cfg = fast_cfg()
    prices = make_prices(n_bars=240, seed=7)
    signals = make_signals(prices, seed=11, strength=0.18)
    env = _build_env(cfg, prices, signals)

    # 含多空与空仓的固定动作序列，制造真实交易
    seq = [3, 3, 3, 2, 1, 1, 2, 4, 4, 2, 3, 3, 2, 1, 1, 2]  # 0.5多→平→0.5空→平→满多→平→满空…
    eq_env, targets = _scripted_actions(env, seq)
    assert len(eq_env) > 0

    # BacktestEngine 重放
    from hexbroker.backtest.engine import BacktestEngine

    eng = BacktestEngine(cfg)
    pf = eng.run(prices, targets)
    eq_bt = pf.equity_curve

    # 只对比信号 bar 上的权益（env 只在这些 bar 上估值）
    common = eq_env.index.intersection(eq_bt.index)
    diff = (eq_env.loc[common] - eq_bt.loc[common]).abs().max()
    assert diff < 1e-6, f"训练-回测一致性失败：权益差 {diff}"


def test_env_target_frame_contract_scale_sane():
    cfg = fast_cfg()
    prices = make_prices(n_bars=240, seed=7)
    signals = make_signals(prices, seed=11)
    env = _build_env(cfg, prices, signals)
    obs = env.reset()
    done = False
    while not done:
        obs, _, done, _ = env.step(3)  # 0.5 多
    t = env.target_frame["target"]
    assert (t.abs() > 0).any()  # 有实际成交
