"""P0-1 撮合口径测试（PRD A1.2–A1.4）。

覆盖：同 bar vs next_bar 对照、volume_cap partial/reject 语义、
开关缺列 fail-fast、默认行为 <1e-12 兜底。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from _helpers import fast_cfg, make_prices, make_signals

from hexbroker.backtest.execution import ExecutionConfig, cap_order_qty, run_dual_caliber
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.baseline import BaselineStrategy
from hexbroker.evaluation.metrics import compute_metrics


# ---------------------------------------------------------------------------
# cap_order_qty 纯函数语义（PRD A1.4）
# ---------------------------------------------------------------------------
def test_cap_order_qty_partial():
    # delta=100 超量 → 截断到 ±(1000×0.05)=±50
    assert cap_order_qty(100.0, 0.0, 1000.0, 0.05, "partial") == 50.0
    assert cap_order_qty(-100.0, 0.0, 1000.0, 0.05, "partial") == -50.0
    # 未超量原样
    assert cap_order_qty(10.0, 0.0, 1000.0, 0.05, "partial") == 10.0
    # 从已有仓位按比例调整（delta=80 超量 → 截断到 50）
    assert cap_order_qty(100.0, 20.0, 1000.0, 0.05, "partial") == 20.0 + 50.0


def test_cap_order_qty_reject():
    # 超量整单拒绝（delta=0 → 返回 current）
    assert cap_order_qty(100.0, 0.0, 1000.0, 0.05, "reject") == 0.0
    # 未超量成交
    assert cap_order_qty(30.0, 0.0, 1000.0, 0.05, "reject") == 30.0


def test_cap_order_qty_zero_volume_or_cap_returns_current():
    assert cap_order_qty(100.0, 7.0, 0.0, 0.05, "partial") == 7.0
    assert cap_order_qty(100.0, 7.0, 1000.0, 0.0, "partial") == 7.0


def test_next_bar_ref_price_semantics():
    from hexbroker.backtest.execution import next_bar_ref_price

    idx = pd.date_range("2020-01-01", periods=4, freq="D")
    opens = pd.Series([10.0, 11.0, 12.0, 13.0], index=idx)
    shifted = opens.shift(-1)
    assert next_bar_ref_price(shifted, idx[0]) == 11.0
    assert next_bar_ref_price(shifted, idx[2]) == 13.0
    # 末根无下一 bar → None
    assert next_bar_ref_price(shifted, idx[3]) is None
    # 时间戳不在索引 → None
    assert next_bar_ref_price(shifted, pd.Timestamp("2020-02-01")) is None


# ---------------------------------------------------------------------------
# 引擎级：next_bar_execution
# ---------------------------------------------------------------------------
def test_next_bar_execution_fills_next_open():
    cfg = fast_cfg()
    prices = make_prices(n_bars=30, seed=1)
    targets = pd.DataFrame({"target": 10.0}, index=prices.index)

    eng = BacktestEngine(cfg, execution=ExecutionConfig(next_bar_execution=True))
    eng.run(prices, targets)
    trades = eng.broker.trades
    assert len(trades) == 1, "仅在首个决策 bar 发生一次建仓成交"
    t = trades[0]
    sub = prices.xs("SHFE.cu", level=0)
    next_open = float(sub["open"].iloc[1])
    expected = next_open + cfg.backtest.min_tick  # 买入 + 1 tick 滑点
    assert abs(t.fill_price - expected) < 1e-9, f"fill {t.fill_price} != {expected}"
    # 时间戳为决策 bar（t0）
    assert t.timestamp == sub.index[0]


def test_next_bar_execution_last_bar_skipped():
    """末根 bar 的目标变化无下一 bar open → 跳过成交。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=10, seed=2)
    sub = prices.xs("SHFE.cu", level=0)
    # 目标仅在最后一根 bar 变为 1（其余 0）
    targets = pd.DataFrame({"target": 0.0}, index=prices.index)
    targets.iloc[-1] = 1.0

    eng = BacktestEngine(cfg, execution=ExecutionConfig(next_bar_execution=True))
    eng.run(prices, targets)
    assert len(eng.broker.trades) == 0, "末根无下一 bar，不应成交"


def test_next_bar_execution_missing_open_raises():
    cfg = fast_cfg()
    prices = make_prices(n_bars=20, seed=3).drop(columns=["open"])
    targets = pd.DataFrame({"target": 1.0}, index=prices.index)
    with pytest.raises(ValueError, match="open"):
        BacktestEngine(cfg, execution=ExecutionConfig(next_bar_execution=True)).run(prices, targets)


# ---------------------------------------------------------------------------
# 引擎级：volume_cap partial / reject（PRD A1.4）
# ---------------------------------------------------------------------------
def test_volume_cap_partial_fills_capped_qty():
    cfg = fast_cfg()
    prices = make_prices(n_bars=20, seed=2)
    sub = prices.xs("SHFE.cu", level=0)
    vol0 = float(sub["volume"].iloc[0])
    max_delta = vol0 * 0.05
    targets = pd.DataFrame({"target": 100000.0}, index=prices.index)
    targets.iloc[1:] = 0.0  # 仅 t0 一次目标变化，避免跨 bar 连锁追单

    eng = BacktestEngine(cfg, execution=ExecutionConfig(volume_cap=0.05, volume_cap_mode="partial"))
    eng.run(prices, targets)
    assert len(eng.broker.trades) >= 1
    assert abs(eng.broker.trades[0].qty - max_delta) < 1e-9, "部分成交 qty 应为 ±(volume×cap)"


def test_volume_cap_reject_blocks_oversize():
    cfg = fast_cfg()
    prices = make_prices(n_bars=20, seed=2)
    targets = pd.DataFrame({"target": 100000.0}, index=prices.index)
    targets.iloc[1:] = 0.0

    eng = BacktestEngine(cfg, execution=ExecutionConfig(volume_cap=0.05, volume_cap_mode="reject"))
    eng.run(prices, targets)
    assert len(eng.broker.trades) == 0, "超量整单拒绝后不应有任何成交"


def test_volume_cap_per_symbol_dict():
    cfg = fast_cfg()
    prices = make_prices(n_bars=20, seed=2, symbols=["SHFE.cu", "DCE.m"])
    sub_cu = prices.xs("SHFE.cu", level=0)
    vol_cu = float(sub_cu["volume"].iloc[0])
    targets = pd.DataFrame({"target": 100000.0}, index=prices.index)
    targets.iloc[1:] = 0.0

    eng = BacktestEngine(
        cfg, execution=ExecutionConfig(volume_cap={"SHFE.cu": 0.10}, volume_cap_mode="partial")
    )
    eng.run(prices, targets)
    # 仅覆盖品种发生部分成交，且比例取 0.10（非默认 0.05）
    trades = eng.broker.trades
    cu_trades = [t for t in trades if t.symbol == "SHFE.cu"]
    assert cu_trades and abs(cu_trades[0].qty - vol_cu * 0.10) < 1e-9


def test_volume_cap_missing_volume_raises():
    cfg = fast_cfg()
    prices = make_prices(n_bars=20, seed=4).drop(columns=["volume"])
    targets = pd.DataFrame({"target": 1.0}, index=prices.index)
    with pytest.raises(ValueError, match="volume"):
        BacktestEngine(cfg, execution=ExecutionConfig(volume_cap=0.05)).run(prices, targets)


# ---------------------------------------------------------------------------
# 双口径对照（PRD A1.3 / F1.4）
# ---------------------------------------------------------------------------
def test_run_dual_caliber_structure():
    cfg = fast_cfg()
    prices = make_prices(n_bars=120, seed=5)
    signals = make_signals(prices, seed=6)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    out = run_dual_caliber(prices, targets, cfg)
    assert set(out) == {"same_bar", "next_bar", "delta_pct", "note"}
    for k in ("same_bar", "next_bar"):
        assert set(out[k]) == {"sharpe", "calmar", "max_drawdown", "win_rate"}
    assert set(out["delta_pct"]) == {"sharpe", "calmar", "max_drawdown", "win_rate"}


def test_run_dual_caliber_delta_quantifies_samebar_bias():
    """在可学习信号上，next_bar（保守）绩效应低于或接近同 bar（乐观）——Δ% 显式量化。"""
    cfg = fast_cfg()
    prices = make_prices(n_bars=200, seed=5)
    signals = make_signals(prices, seed=6, strength=0.25)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    out = run_dual_caliber(prices, targets, cfg)
    # 同 bar 口径 Sharpe 不应低于保守口径（同 bar 更乐观，至少不应显著更差）
    assert out["same_bar"]["sharpe"] >= out["next_bar"]["sharpe"] - 1e-9
    # Δ% 已量化（None 或有限数值）
    for v in out["delta_pct"].values():
        assert v is None or np.isfinite(v)


# ---------------------------------------------------------------------------
# 默认行为零变化（PRD A1.2：<1e-12）
# ---------------------------------------------------------------------------
def _reference_close_run(prices, targets, cfg):
    """复刻改动前 engine.run 逻辑（直接调用未改动的 broker/cost/portfolio）。"""
    from hexbroker.backtest.broker import SimBroker
    from hexbroker.backtest.cost import CostModel
    from hexbroker.backtest.portfolio import Portfolio

    cost = CostModel.from_config(cfg)
    broker = SimBroker(cost, float(getattr(getattr(cfg, "backtest", None), "initial_capital", 1_000_000.0)))
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
    portfolio = Portfolio(float(getattr(getattr(cfg, "backtest", None), "initial_capital", 1_000_000.0)))
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
    cfg = fast_cfg()
    prices = make_prices(n_bars=240, seed=7)
    signals = make_signals(prices, seed=11, strength=0.18)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    eng = BacktestEngine(cfg)  # execution=None → 默认口径
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
    for k in ("sharpe", "calmar", "max_drawdown", "total_return", "final_equity"):
        assert abs(getattr(m, k) - getattr(m_ref, k)) < 1e-12, f"{k} 偏离改动前基线"


def test_explicit_default_execution_equals_implicit():
    cfg = fast_cfg()
    prices = make_prices(n_bars=200, seed=3)
    signals = make_signals(prices, seed=4)
    targets = BaselineStrategy("signal_threshold").generate(prices, signals)

    a = BacktestEngine(cfg).run(prices, targets).equity_curve
    b = BacktestEngine(cfg, execution=ExecutionConfig()).run(prices, targets).equity_curve
    assert (a - b).abs().max() < 1e-12


def test_execution_config_from_cfg_defaults():
    from hexbroker.config import load_config

    cfg = load_config()
    ex = ExecutionConfig.from_cfg(cfg)
    assert ex.next_bar_execution is False
    assert ex.volume_cap is None
    assert ex.volume_cap_mode == "partial"
