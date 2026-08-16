"""回测层（§2.2 L7 / §3.5）。

``CostModel`` 是交易成本的唯一真理源——回测引擎与 RL 环境（T04）共用同一实例，
从而保证 **训练-回测一致性**（权益曲线差异 < 1e-6）。
"""

from .cost import CostModel
from .broker import SimBroker, Trade
from .portfolio import Portfolio
from .engine import BacktestEngine
from .walkforward import WalkForwardBacktester

__all__ = ["CostModel", "SimBroker", "Trade", "Portfolio", "BacktestEngine", "WalkForwardBacktester"]
