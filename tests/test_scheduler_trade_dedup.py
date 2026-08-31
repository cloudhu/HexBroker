"""Scheduler._record_trade 对称去重回归测试（四缺陷潜伏加固债 ②）。

防御「同一 event 对象被重复记录」导致内存聚合器（_day_trades / _all_trades）
与审计日志计数翻倍。注意边界：trade_id 为单调序列，本守卫不覆盖重入执行产生
新 trade_id 的情形（需 execute_plan 幂等性保障，独立设计项）。
"""
from datetime import date
from types import SimpleNamespace

import pytest

from hexbroker.paper.scheduler import TradingScheduler as Scheduler


class _Logger:
    def __init__(self):
        self.trade_calls = 0

    def trade(self, event):
        self.trade_calls += 1


def _make_recorder():
    obj = type("Rec", (), {})()
    obj._day_trades = {}
    obj._all_trades = []
    obj._seen_trade_ids = set()
    obj._logger = _Logger()
    return obj


def _make_event(trade_id):
    return SimpleNamespace(trade_id=trade_id)


def _bind(obj):
    return Scheduler._record_trade.__get__(obj)


def test_record_trade_dedup_same_id():
    obj = _make_recorder()
    rec = _bind(obj)
    day = date(2026, 8, 31)
    rec(_make_event("T000001"), day)
    rec(_make_event("T000001"), day)  # 同 id 重复记录
    assert len(obj._all_trades) == 1
    assert len(obj._day_trades[day]) == 1
    assert obj._logger.trade_calls == 1


def test_record_trade_distinct_ids():
    obj = _make_recorder()
    rec = _bind(obj)
    day = date(2026, 8, 31)
    rec(_make_event("T000001"), day)
    rec(_make_event("T000002"), day)
    assert len(obj._all_trades) == 2
    assert obj._logger.trade_calls == 2


def test_record_trade_multi_day_independent():
    obj = _make_recorder()
    rec = _bind(obj)
    d1 = date(2026, 8, 31)
    d2 = date(2026, 9, 1)
    rec(_make_event("T000001"), d1)
    rec(_make_event("T000002"), d2)
    assert len(obj._day_trades[d1]) == 1
    assert len(obj._day_trades[d2]) == 1
    assert len(obj._all_trades) == 2
    assert obj._logger.trade_calls == 2
