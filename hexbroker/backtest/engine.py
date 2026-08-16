"""事件驱动回测引擎（§3.5 / §8.5）。

逐 bar、逐标的地根据目标仓位执行交易（经 ``SimBroker`` 精确记账），并记录权益曲线。
与 RL 环境共用同一个 ``SimBroker`` / ``CostModel``，从而保证训练-回测一致性。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from .broker import SimBroker
from .cost import CostModel
from .portfolio import Portfolio

_log = get_logger("BT")


class BacktestEngine:
    """bar 级事件回测引擎。"""

    def __init__(self, cfg: Any, cost: Any = None, initial_capital: float | None = None) -> None:
        self.cfg = cfg
        self.cost = cost if cost is not None else CostModel.from_config(cfg)
        ic = initial_capital if initial_capital is not None else getattr(
            getattr(cfg, "backtest", None), "initial_capital", 1_000_000.0
        )
        self.initial_capital = float(ic)
        self.broker = SimBroker(self.cost, self.initial_capital)

    def run(self, prices: pd.DataFrame, targets: pd.DataFrame) -> Portfolio:
        """运行回测。

        参数
        ----
        prices  : MultiIndex(symbol, datetime)，需含 ``close`` 列。
        targets : MultiIndex(symbol, datetime)，需含 ``target`` 列（目标合约数，可正可负）。

        返回
        ----
        Portfolio（含权益曲线与回撤）。
        """
        if "close" not in prices.columns:
            raise ValueError("prices 必须含 'close' 列")
        if "target" not in targets.columns:
            raise ValueError("targets 必须含 'target' 列")

        symbols = list(prices.index.get_level_values(0).unique())
        # 目标仓位按标的分别前向填充（缺失标的安全降级为空仓）
        fwd_targets: dict[str, pd.Series] = {}
        for sym in symbols:
            try:
                sub = targets.xs(sym, level=0)["target"]
            except (KeyError, TypeError):
                fwd_targets[sym] = pd.Series(dtype=float)
                continue
            if len(sub) == 0:
                fwd_targets[sym] = pd.Series(dtype=float)
            else:
                fwd_targets[sym] = sub.sort_index()

        all_ts = sorted(
            set(prices.index.get_level_values(1)) | set(targets.index.get_level_values(1))
        )
        portfolio = Portfolio(self.initial_capital)
        cur_target: dict[str, float] = {s: 0.0 for s in symbols}

        for ts in all_ts:
            marks: dict[str, float] = {}
            for sym in symbols:
                # 当前价格
                sub_p = prices.xs(sym, level=0)
                if ts in sub_p.index:
                    marks[sym] = float(sub_p.loc[ts, "close"])
                # 更新目标仓位（前向填充）
                tgt_series = fwd_targets[sym]
                if len(tgt_series) and ts >= tgt_series.index.min():
                    cur_target[sym] = float(tgt_series.loc[:ts].iloc[-1])
                if sym in marks:
                    self.broker.execute(sym, cur_target[sym], marks[sym], timestamp=ts)
            portfolio.record(ts, self.broker.equity(marks))

        _log.info("回测完成：最终权益 %.2f", portfolio.final_equity)
        return portfolio
