"""P8 回归：涨跌停 bar 且 limit_trade_allowed=False 时必须跳过成交（禁止以 close 乐观成交）。"""

from __future__ import annotations

import pandas as pd

from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config


def _frame():
    dates = pd.date_range("2020-01-01", periods=3, freq="D")
    # 价格序列：t0=100, t1=110(涨停), t2=120
    prices = pd.DataFrame(
        {
            "close": [100.0, 110.0, 120.0],
            "limit_up": [False, True, False],
            "limit_down": [False, False, False],
        },
        index=pd.MultiIndex.from_product([["X"], dates]),
    )
    # 目标仓位：t0=0（空仓），t1 起=1（本应在涨停 bar 建仓）
    targets = pd.DataFrame(
        {"target": [0.0, 1.0, 1.0]},
        index=pd.MultiIndex.from_product([["X"], dates]),
    )
    return prices, targets, dates


def test_limit_up_blocks_fill_when_disallowed():
    cfg = load_config()
    cfg.backtest.limit_trade_allowed = False  # 默认即 False，显式确认
    prices, targets, dates = _frame()
    eng = BacktestEngine(cfg)
    eng.run(prices, targets)
    # 涨停 bar(t1) 的成交必须被跳过；建仓应推迟到 t2（非涨停 bar）
    fill_ts = {t.timestamp for t in eng.broker.trades}
    assert dates[1] not in fill_ts, "涨停 bar 不应发生成交（limit_trade_allowed=False）"
    # t2 才建仓（非涨停 bar）
    assert dates[2] in fill_ts


def test_limit_up_allows_fill_when_enabled():
    cfg = load_config()
    cfg.backtest.limit_trade_allowed = True
    prices, targets, dates = _frame()
    eng = BacktestEngine(cfg)
    eng.run(prices, targets)
    fill_ts = {t.timestamp for t in eng.broker.trades}
    # 允许时：涨停 bar(t1) 也发生成交（以该 bar 价格成交，含滑点）
    assert dates[1] in fill_ts
