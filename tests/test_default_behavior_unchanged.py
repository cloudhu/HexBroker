"""T05 红线测试：默认配置下绩效与改动前基线 <1e-12 数值一致（PRD A1.2）。

实现方式：由于 engine.py 已在原地修改，本测试用「未改动模块（SimBroker /
CostModel / Portfolio）」复刻改动前 ``BacktestEngine.run`` 的逐语句逻辑作参考基线，
断言默认配置下引擎输出与参考基线完全一致。另验证默认配置字段与
``ExecutionConfig`` 默认值 = 生产基线口径。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from _helpers import fast_cfg, make_prices, make_signals

from hexbroker.backtest.engine import BacktestEngine
from hexbroker.backtest.execution import ExecutionConfig
from hexbroker.config import load_config
from hexbroker.evaluation.baseline import BaselineStrategy
from hexbroker.evaluation.metrics import compute_metrics


def _reference_close_run(prices, targets, cfg):
    """复刻改动前 engine.run 逻辑（直接调用未改动的 broker/cost/portfolio）。"""
    from hexbroker.backtest.broker import SimBroker
    from hexbroker.backtest.cost import CostModel
    from hexbroker.backtest.portfolio import Portfolio

    initial = float(getattr(getattr(cfg, "backtest", None), "initial_capital", 1_000_000.0))
    cost = CostModel.from_config(cfg)
    broker = SimBroker(cost, initial)
    symbols = list(prices.index.get_level_values(0).unique())
    fwd_targets: dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            sub = targets.xs(sym, level=0)["target"]
        except (KeyError, TypeError):
            fwd_targets[sym] = pd.Series(dtype=float)
            continue
        fwd_targets[sym] = sub.sort_index() if len(sub) else pd.Series(dtype=float)
    all_ts = sorted(set(prices.index.get_level_values(1)) | set(targets.index.get_level_values(1)))
    portfolio = Portfolio(initial)
    cur_target = {s: 0.0 for s in symbols}
    allow_limit = bool(getattr(getattr(cfg, "backtest", None), "limit_trade_allowed", True))
    has_limit = "limit_up" in prices.columns or "limit_down" in prices.columns
    for ts in all_ts:
        marks: dict[str, float] = {}
        limit_flags: dict[str, bool] = {}
        for sym in symbols:
            sub_p = prices.xs(sym, level=0)
            if ts in sub_p.index:
                row = sub_p.loc[ts]
                marks[sym] = float(row["close"])
                if has_limit:
                    limit_flags[sym] = bool(row.get("limit_up", False)) or bool(row.get("limit_down", False))
            tgt_series = fwd_targets[sym]
            if len(tgt_series) and ts >= tgt_series.index.min():
                cur_target[sym] = float(tgt_series.loc[:ts].iloc[-1])
            if sym in marks:
                if limit_flags.get(sym, False) and not allow_limit:
                    continue
                broker.execute(sym, cur_target[sym], marks[sym], timestamp=ts)
        portfolio.record(ts, broker.equity(marks))
    return portfolio


def test_default_engine_equals_pre_change_reference():
    """多品种 + 阈值策略 + 含涨跌停标记：默认口径与改动前基线 <1e-12。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=240, seed=7, symbols=["SHFE.cu", "DCE.m"])
    signals = make_signals(prices, seed=11, strength=0.18)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    eng = BacktestEngine(cfg)  # execution=None → ExecutionConfig.from_cfg → 默认口径
    pf = eng.run(prices, targets)
    ref = _reference_close_run(prices, targets, cfg)

    eq = pf.equity_curve
    eq_ref = ref.equity_curve
    common = eq.index.intersection(eq_ref.index)
    assert len(common) > 0
    diff = (eq.loc[common] - eq_ref.loc[common]).abs().max()
    assert diff < 1e-12, f"默认配置权益曲线偏离改动前基线 {diff}"

    m = compute_metrics(eq, freq="1d")
    m_ref = compute_metrics(eq_ref, freq="1d")
    for k in ("sharpe", "calmar", "max_drawdown", "total_return", "final_equity", "win_rate"):
        assert abs(getattr(m, k) - getattr(m_ref, k)) < 1e-12, f"{k} 偏离改动前基线"


def test_default_engine_with_limit_columns_matches_reference():
    """引擎默认路径含 limit_up/down 列时与改动前 P8 行为一致。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=120, seed=13)
    prices["limit_up"] = False
    prices["limit_down"] = False
    signals = make_signals(prices, seed=5)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    eng = BacktestEngine(cfg)
    pf = eng.run(prices, targets)
    ref = _reference_close_run(prices, targets, cfg)
    diff = (pf.equity_curve - ref.equity_curve).abs().max()
    assert diff < 1e-12


def test_explicit_default_execution_equals_implicit():
    """显式 ExecutionConfig()（默认值）与隐式（None）完全等价。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=200, seed=3, symbols=["SHFE.cu", "DCE.m"])
    signals = make_signals(prices, seed=4)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    a = BacktestEngine(cfg).run(prices, targets).equity_curve
    b = BacktestEngine(cfg, execution=ExecutionConfig()).run(prices, targets).equity_curve
    assert (a - b).abs().max() < 1e-12


def test_default_config_fields_are_noop_values():
    """默认配置字段 = 生产基线口径（next_bar_execution=False / volume_cap=None / bootstrap 默认）。"""
    cfg = load_config()
    assert cfg.backtest.next_bar_execution is False
    assert cfg.backtest.volume_cap is None
    assert cfg.backtest.volume_cap_mode == "partial"
    b = cfg.backtest.bootstrap
    assert b.block_len == 20
    assert b.n_boot == 1000
    assert b.seed is None
    assert b.by_symbol is False


def test_execution_from_cfg_matches_defaults():
    ex = ExecutionConfig.from_cfg(load_config())
    assert ex == ExecutionConfig()
    assert ex.next_bar_execution is False
    assert ex.volume_cap is None
    assert ex.volume_cap_mode == "partial"


def test_walkforward_default_unchanged():
    """WalkForwardBacktester 默认（execution=None）聚合结果与参考逐段回测一致。"""
    from hexbroker.backtest.walkforward import WalkForwardBacktester

    cfg = fast_cfg()
    prices = make_prices(n_bars=90, seed=7)
    signals = make_signals(prices, seed=8)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    wf = WalkForwardBacktester(cfg)
    res = wf.run(prices, targets, n_folds=3)
    agg = res["aggregate"]
    assert "profit_factor" in agg and "sharpe" in agg
    # 逐段参考对比
    ts = sorted(prices.index.get_level_values(1).unique())
    import numpy as np

    edges = np.array_split(np.arange(len(ts)), 3)
    refs = []
    for seg in edges:
        lo = ts[int(seg[0])]
        hi = ts[int(seg[-1])]
        p_sub = prices[(prices.index.get_level_values(1) >= lo) & (prices.index.get_level_values(1) <= hi)]
        t_sub = targets[(targets.index.get_level_values(1) >= lo) & (targets.index.get_level_values(1) <= hi)]
        refs.append(_reference_close_run(p_sub, t_sub, cfg).final_equity)
    eng_equities = [r.final_equity for r in res["reports"]]
    assert len(refs) == len(eng_equities)
    for ref_eq, eng_eq in zip(refs, eng_equities):
        assert abs(ref_eq - eng_eq) < 1e-12
