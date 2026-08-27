"""事件驱动回测引擎（§3.5 / §8.5）。

逐 bar、逐标的地根据目标仓位执行交易（经 ``SimBroker`` 精确记账），并记录权益曲线。
与 RL 环境共用同一个 ``SimBroker`` / ``CostModel``，从而保证训练-回测一致性。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..utils.logging import get_logger
from .broker import SimBroker
from .cost import CostModel
from .execution import ExecutionConfig, cap_order_qty, next_bar_ref_price
from .portfolio import Portfolio

_log = get_logger("BT")


class BacktestEngine:
    """bar 级事件回测引擎。"""

    def __init__(self, cfg: Any, cost: Any = None, initial_capital: float | None = None,
                 execution: ExecutionConfig | None = None) -> None:
        self.cfg = cfg
        self.cost = cost if cost is not None else CostModel.from_config(cfg)
        ic = initial_capital if initial_capital is not None else getattr(
            getattr(cfg, "backtest", None), "initial_capital", 1_000_000.0
        )
        self.initial_capital = float(ic)
        self.broker = SimBroker(self.cost, self.initial_capital)
        # P0-1：撮合假设开关（execution=None → 从 cfg.backtest 读取；
        # 默认 next_bar_execution=False / volume_cap=None 时与原代码逐语句等价）
        self.execution = execution if execution is not None else ExecutionConfig.from_cfg(cfg)
        # P1-9：分品种中国市场规则表（交割月禁开仓 / 分品种涨跌停幅度）。
        # 默认 CostModel.market_rules=None → 不启用，回退现状口径。
        self.market_rules = getattr(self.cost, "market_rules", None)

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

        # P0-1：撮合假设开关（默认关闭 → 以下分支均不进入，与原代码等价）
        ex = self.execution
        if ex.next_bar_execution and "open" not in prices.columns:
            raise ValueError("next_bar_execution=True 但 prices 缺少 'open' 列（成交价需下一 bar 开盘价）")
        if ex.volume_cap is not None and "volume" not in prices.columns:
            raise ValueError("volume_cap 启用但 prices 缺少 'volume' 列（成交量约束需 bar 成交量）")
        # 预计算 per-symbol 下一 bar open（shift(-1)，末根为 NaN）
        next_open: dict[str, pd.Series] = {}
        if ex.next_bar_execution:
            for sym in symbols:
                next_open[sym] = prices.xs(sym, level=0)["open"].shift(-1)

        all_ts = sorted(
            set(prices.index.get_level_values(1)) | set(targets.index.get_level_values(1))
        )
        portfolio = Portfolio(self.initial_capital)
        cur_target: dict[str, float] = {s: 0.0 for s in symbols}

        # P8 修复：涨跌停拦截开关（config.backtest.limit_trade_allowed）
        allow_limit = bool(getattr(getattr(self.cfg, "backtest", None), "limit_trade_allowed", True))
        has_limit_cols = "limit_up" in prices.columns or "limit_down" in prices.columns
        # P1-9：分品种市场规则表（默认 None → 不启用，回退现状涨跌停/开仓口径）
        market_table = self.market_rules
        # 维护每个品种上一 bar 收盘价（仅在规则表配置了涨跌停幅度时使用）
        prev_close: dict[str, float] = {}

        for ts in all_ts:
            marks: dict[str, float] = {}
            limit_flags: dict[str, bool] = {}
            for sym in symbols:
                # 当前价格
                sub_p = prices.xs(sym, level=0)
                if ts in sub_p.index:
                    row = sub_p.loc[ts]
                    marks[sym] = float(row["close"])
                    # P8 修复：判定本 bar 是否涨跌停（缺流动性，禁止以该价成交）
                    limit_hit = False
                    if has_limit_cols:
                        lu = bool(row.get("limit_up", False))
                        ld = bool(row.get("limit_down", False))
                        limit_hit = lu or ld
                    # P1-9：规则表覆盖的涨跌停幅度（无覆盖 → 不变）
                    if market_table is not None:
                        ru, rd = market_table.limit(sym)
                        if ru is not None or rd is not None:
                            prev = prev_close.get(sym)
                            if prev is not None:
                                close = float(row["close"])
                                if ru is not None and close >= prev * (1.0 + ru):
                                    limit_hit = True
                                if rd is not None and close <= prev * (1.0 - rd):
                                    limit_hit = True
                    limit_flags[sym] = limit_hit
                    prev_close[sym] = float(row["close"])
                # 更新目标仓位（前向填充）
                tgt_series = fwd_targets[sym]
                if len(tgt_series) and ts >= tgt_series.index.min():
                    cur_target[sym] = float(tgt_series.loc[:ts].iloc[-1])
                if sym in marks:
                    # P8 修复：涨跌停且未允许 → 跳过成交（不再以 close 乐观成交）
                    if limit_flags.get(sym, False) and not allow_limit:
                        _log.debug("bar %s @ %s 触发涨跌停，limit_trade_allowed=False 跳过成交", sym, ts)
                        continue
                    # P1-9：交割月禁开仓（规则表 allows_open；默认无规则 → 允许，行为不变）
                    if market_table is not None and not market_table.allows_open(sym, ts):
                        _log.debug("bar %s @ %s 交割月禁开仓，跳过成交", sym, ts)
                        continue
                    ref_price = marks[sym]
                    target_qty = cur_target[sym]
                    # P0-1：next_bar_execution=True → 成交参考价取下一 bar open（无下一 bar 跳过成交）
                    if ex.next_bar_execution:
                        nxt = next_bar_ref_price(next_open[sym], ts)
                        if nxt is None:
                            _log.debug("bar %s @ %s 无下一 bar，next_bar_execution 跳过成交", sym, ts)
                            continue
                        ref_price = nxt
                    # P0-1：volume_cap 非 None → 目标调整量受 bar.volume × cap 约束
                    if ex.volume_cap is not None:
                        cap = ex.resolve_cap(sym)
                        if cap is not None:
                            bar_volume = float(row["volume"])
                            target_qty = cap_order_qty(
                                target_qty, self.broker.position(sym),
                                bar_volume, cap, ex.volume_cap_mode,
                            )
                    self.broker.execute(sym, target_qty, ref_price, timestamp=ts)
            portfolio.record(ts, self.broker.equity(marks))

        _log.info("回测完成：最终权益 %.2f", portfolio.final_equity)
        return portfolio
