"""P1-9 中国市场规则化测试（A9.1~A9.5）。

默认等价：默认表 = 统一 0.12 保证金 / 无幅度覆盖 / 无交割限制 → 与现状数值一致（<1e-12）。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest
from pathlib import Path

from hexbroker.market import MarketRule, MarketRuleTable
from hexbroker.market.session import day_label as shared_day_label
from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.paper.sessions import TradingSession, parse_sessions


# ---------------------------------------------------------------------------
# A9.1 默认等价（统一 0.12 / 无限制）
# ---------------------------------------------------------------------------
def test_default_margin_rate_uniform():
    tbl = MarketRuleTable.default()
    assert tbl.margin_rate("au") == pytest.approx(0.12)
    assert tbl.margin_rate("cu") == pytest.approx(0.12)
    assert tbl.margin_rate("nonexistent") == pytest.approx(0.12)


def test_cost_model_default_equivalence():
    """CostModel 注入默认空规则表后，保证金与现状字段口径逐品种一致（<1e-12）。"""
    cm_default = CostModel()
    cm_tabled = CostModel(market_rules=MarketRuleTable.default())
    for sym in ("au", "cu", "zz"):
        a = cm_default.margin(3000.0, 1.0, sym)
        b = cm_tabled.margin(3000.0, 1.0, sym)
        assert abs(a - b) < 1e-12


def test_from_yaml_missing_returns_default():
    tbl = MarketRuleTable.from_yaml(None)
    assert tbl.margin_rate("au") == pytest.approx(0.12)
    assert tbl.limit("au") == (None, None)


# ---------------------------------------------------------------------------
# A9.2 分品种保证金率差异
# ---------------------------------------------------------------------------
def test_per_variety_margin_rate():
    tbl = MarketRuleTable(
        rules={
            "au": MarketRule(symbol="au", margin_rate=0.10),
            "cu": MarketRule(symbol="cu", margin_rate=0.12),
        }
    )
    assert tbl.margin_rate("au") == pytest.approx(0.10)
    assert tbl.margin_rate("cu") == pytest.approx(0.12)
    assert tbl.margin_rate("rb") == pytest.approx(0.12)  # 未列出 → 回退默认

    cm = CostModel(market_rules=tbl)
    # au 乘数 ×1000、cu 乘数 ×10（见 _SPEC_MULTIPLIER）
    assert cm.margin(3000.0, 1.0, "au") == pytest.approx(3000.0 * 1000.0 * 0.10)
    assert cm.margin(3000.0, 1.0, "cu") == pytest.approx(3000.0 * 10.0 * 0.12)


# ---------------------------------------------------------------------------
# A9.3 涨跌停幅度
# ---------------------------------------------------------------------------
def test_limit_up_down_read():
    tbl = MarketRuleTable(
        rules={"au": MarketRule(symbol="au", limit_up=0.04, limit_down=0.04)}
    )
    assert tbl.limit("au") == (0.04, 0.04)
    assert tbl.limit("cu") == (None, None)  # 未覆盖 → 沿用现状 price 列布尔拦截
    r = MarketRule(symbol="cu")
    assert r.limit_up is None and r.limit_down is None


# ---------------------------------------------------------------------------
# A9.4 交割月禁开仓
# ---------------------------------------------------------------------------
def test_delivery_no_open():
    r = MarketRule(symbol="IF", delivery_rule="no_open", delivery_months=(3, 6, 9, 12))
    assert r.allows_open(date(2026, 3, 15)) is False
    assert r.allows_open(date(2026, 6, 15)) is False
    assert r.allows_open(date(2026, 7, 15)) is True
    # 默认 "none" → 始终允许
    none_r = MarketRule(symbol="cu")
    assert none_r.allows_open(date(2026, 3, 15)) is True
    # no_open 未声明交割月 → 保守始终禁止开仓
    strict = MarketRule(symbol="X", delivery_rule="no_open")
    assert strict.allows_open(date(2026, 7, 15)) is False


# ---------------------------------------------------------------------------
# A9.5 集成：CostModel.from_config / paper day_label 复用 / engine 交割月拦截
# ---------------------------------------------------------------------------
def _rule_yaml_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "configs" / "market_rules.yaml")


def test_cost_from_config_loads_market_rules():
    cfg = load_config(market_rules=_rule_yaml_path())
    cm = CostModel.from_config(cfg)
    assert cm.market_rules is not None
    assert cm.market_rules.margin_rate("au") == pytest.approx(0.10)
    assert cm.margin(3000.0, 1.0, "au") == pytest.approx(3000.0 * 1000.0 * 0.10)


def test_from_yaml_loads_example_config():
    tbl = MarketRuleTable.from_yaml(_rule_yaml_path())
    assert tbl.margin_rate("au") == pytest.approx(0.10)
    assert tbl.margin_rate("cu") == pytest.approx(0.10)
    assert tbl.margin_rate("zz") == pytest.approx(0.12)  # 未列出 → 回退
    assert tbl.allows_open("IF", date(2026, 3, 15)) is False
    assert tbl.allows_open("IF", date(2026, 7, 15)) is True
    assert tbl.limit("au") == (None, None)


def test_paper_day_label_reuses_shared_implementation():
    """TradingSession.day_label 委托 market.session.day_label（P1-9 消除重复）。"""
    symbol_sessions = {"ag0": parse_sessions(
        [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]],
        [["21:00", "02:30"]],
    )}
    s = TradingSession(symbol_sessions=symbol_sessions, holidays=set(), night_boundary=time(21, 0))
    # 周五 21:30 夜盘 → 归属下一交易日（周一 2026-08-24）
    ts = datetime(2026, 8, 21, 21, 30)
    assert s.day_label(ts) == shared_day_label(ts, time(21, 0), set())


def test_engine_respects_delivery_no_open():
    """engine 在交割月（no_open）跳过开仓，非交割月正常成交（默认无表 → 不受影响）。"""
    cfg = load_config()
    cfg.backtest.limit_trade_allowed = True
    table = MarketRuleTable(
        rules={"X": MarketRule(symbol="X", delivery_rule="no_open", delivery_months=(1,))}
    )
    cost = CostModel(market_rules=table)
    eng = BacktestEngine(cfg, cost=cost)

    jan = pd.Timestamp("2026-01-15")  # 交割月（1 月）
    jun = pd.Timestamp("2026-06-15")  # 非交割月
    prices = pd.DataFrame(
        {"close": [100.0, 100.0]},
        index=pd.MultiIndex.from_product([["X"], [jan, jun]]),
    )
    targets = pd.DataFrame(
        {"target": [1.0, 1.0]},
        index=pd.MultiIndex.from_product([["X"], [jan, jun]]),
    )
    eng.run(prices, targets)
    traded = {t.timestamp for t in eng.broker.trades}
    assert jan not in traded   # 交割月禁开仓 → 跳过
    assert jun in traded       # 非交割月 → 正常开仓


def test_engine_without_market_rules_unchanged():
    """默认（market_rules=None）engine 行为与现状一致：P8 涨跌停拦截依旧生效。"""
    cfg = load_config()
    cfg.backtest.limit_trade_allowed = False
    dates = pd.date_range("2020-01-01", periods=3, freq="D")
    prices = pd.DataFrame(
        {"close": [100.0, 110.0, 120.0], "limit_up": [False, True, False],
         "limit_down": [False, False, False]},
        index=pd.MultiIndex.from_product([["X"], dates]),
    )
    targets = pd.DataFrame(
        {"target": [0.0, 1.0, 1.0]},
        index=pd.MultiIndex.from_product([["X"], dates]),
    )
    eng = BacktestEngine(cfg)
    eng.run(prices, targets)
    fill_ts = {t.timestamp for t in eng.broker.trades}
    assert dates[1] not in fill_ts   # 涨停 bar 仍跳过（默认路径不变）
    assert dates[2] in fill_ts
