"""环境向风控透传行情上下文回归测试（P6）。"""

from __future__ import annotations

import numpy as np

from _helpers import fast_cfg, make_prices, make_signals
from hexbroker.rl.futures_env import FuturesTradingEnv
from hexbroker.risk.manager import RiskManager


class CapturingRM(RiskManager):
    """捕获 evaluate 入参的风险管理器（验证 env 是否透传行情上下文）。"""

    def evaluate(self, state, intent_position, p_up=0.5, recent_returns=None,
                 recent_volumes=None, ma_price=None):
        self.last_ctx = {
            "recent_returns": recent_returns,
            "recent_volumes": recent_volumes,
            "ma_price": ma_price,
        }
        return super().evaluate(
            state, intent_position, p_up, recent_returns, recent_volumes, ma_price
        )


def test_env_forwards_market_context():
    """P6 回归：env.step 必须向 RiskManager.evaluate 透传 recent_returns/volumes/ma_price。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=60, seed=9)
    signals = make_signals(prices, seed=10, strength=0.2)
    env = FuturesTradingEnv(signals, prices, cfg)
    env.risk = CapturingRM(cfg)  # 注入捕获型风控
    env.reset()
    for _ in range(25):
        env.step(3)  # 离散5 的 +0.5 多仓意图

    ctx = env.risk.last_ctx
    assert ctx["recent_returns"] is not None
    assert isinstance(ctx["recent_returns"], np.ndarray)
    assert ctx["recent_returns"].shape[0] >= 5  # S5 需要 >=5
    assert ctx["recent_volumes"] is not None
    assert isinstance(ctx["recent_volumes"], np.ndarray)
    assert ctx["recent_volumes"].shape[0] >= 2  # S2 需要 >=2
    assert ctx["ma_price"] is not None
    assert isinstance(ctx["ma_price"], float)

    # ma_price 必须与最近窗口收盘均值一致（上下文计算正确）
    i = env._i - 1
    lo = max(0, i - 20 + 1)
    expected_ma = float(np.mean(env._close[lo : i + 1]))
    assert abs(ctx["ma_price"] - expected_ma) < 1e-9
