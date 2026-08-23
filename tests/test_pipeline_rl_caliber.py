"""E3-B 回归测试：RL 口径不可比修复。

根因（fresh-eyes 实证）：
- 旧 ``_run_rl`` 仅以 ``symbols[0]`` 单品种训练，而基线 ``baseline.py`` 逐品种遍历
  → 口径不可比；
- 阈值基线对比写死 ``scale=1``，而基线/RL 实盘用 ``_contract_scale(cfg, prices)``
  → 杠杆口径不可比。

修复：``_run_rl`` 改为跨**全品种** loop，并把 ``scale`` 透传至 ``_run_baselines`` 的
``signal_threshold`` 对比（不再写死 1）。

全部 mock RL 内部，不实际训练。
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

import hexbroker.pipeline as P
from tests._helpers import fast_cfg, make_prices, make_signals


def test_run_rl_loops_all_symbols_and_passes_scale():
    cfg = fast_cfg()
    prices = make_prices(n_bars=120, seed=21, symbols=["SHFE.cu", "DCE.m"])
    signals = make_signals(prices, seed=21)
    expected_symbols = list(signals.index.get_level_values(0).unique())
    assert len(expected_symbols) == 2

    captured = {"symbols": [], "baseline_scale": None}

    class FakeEnv:
        def __init__(self, signals, prices, cfg, symbol=None):
            captured["symbols"].append(symbol)
            self.symbol = symbol

    def fake_train(env, cfg, total_timesteps=0):
        return None, {"n": 1}, env

    def fake_rollout(policy, env):
        idx = pd.MultiIndex.from_tuples(
            [(env.symbol, pd.Timestamp("2020-01-02"))], names=["symbol", "datetime"]
        )
        return pd.DataFrame({"target": [1]}, index=idx)

    def fake_backtest(cfg, prices, targets):
        return {
            "calmar": 0.5, "max_drawdown": -0.03, "sharpe": 1.0,
            "win_rate": 0.55, "equity": pd.Series([1.0, 1.0]),
        }

    def fake_baselines(cfg, prices, signals, scale):
        captured["baseline_scale"] = scale
        return {"signal_threshold": {"metrics": {"calmar": 0.1, "max_drawdown": -0.05}}}

    with patch("hexbroker.rl.futures_env.FuturesTradingEnv", FakeEnv), \
         patch("hexbroker.rl.futures_env.check_env", lambda env: None), \
         patch("hexbroker.rl.futures_env.assert_env_has_no_model_dependency", lambda cls: None), \
         patch("hexbroker.rl.train.train", fake_train), \
         patch("hexbroker.rl.train.policy_rollout", fake_rollout), \
         patch.object(P, "_backtest", fake_backtest), \
         patch.object(P, "_run_baselines", fake_baselines):
        scale_arg = 7  # 显式传入，验证透传（旧实现写死 1）
        res = P._run_rl(cfg, prices, signals, total_steps=10, scale=scale_arg)

    # 1) 跨全品种 loop（不再只用 symbols[0]）
    assert sorted(captured["symbols"]) == sorted(expected_symbols)
    assert res["n_symbols"] == len(expected_symbols)

    # 2) scale 透传至阈值基线对比（不再写死 1）
    assert captured["baseline_scale"] == scale_arg

    # 3) 返回结构健全
    assert "metrics" in res and "better_than_threshold" in res
    assert res["better_than_threshold"] is True
