"""共享测试夹具。

P3-C（2026-08-31）后，信号新鲜度口径依赖**交易日历**（主湖全品种并集）。
任何涉及新鲜度的用例都必须显式注入日历，否则：

- 本地开发机主湖存在 → 日历可用，但日期随数据更新而漂移（用例不可复现）；
- CI 全新 checkout 主湖缺失（数据产物不入 git）→ 日历为空 → 全部判为
  「无法判定」，用例结果与真实语义脱节。

``use_calendar`` 把日历固定在用例手里，使两类环境行为一致。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pytest


def weekdays(start: date, end: date, exclude: Iterable[date] = ()) -> tuple[date, ...]:
    """生成 ``start..end``（含两端）之间的周一~周五日期升序元组，剔除 ``exclude``。

    仅用于构造测试用迷你日历——**不代表真实交易日历**（不含法定节假日，
    除非显式传入 ``exclude``）。真实日历一律由
    ``hexbroker.paper.signals.load_trading_calendar`` 从主湖并集推导。
    """
    drop = set(exclude)
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in drop:
            out.append(d)
        d += timedelta(days=1)
    return tuple(out)


@pytest.fixture
def use_calendar(monkeypatch):
    """注入迷你交易日历，使用例与文件系统解耦。

    返回 ``callable(start, end, exclude=()) -> tuple[date, ...]``：既构造日历、
    也把它打成补丁返回给调用方复用。

    补丁打在 ``hexbroker.paper.signals.load_trading_calendar`` 上——消费方
    （``health_check.signal_freshness_days`` / ``SignalEngine.trading_calendar``）
    都在**调用时**才导入该函数，故补丁生效。

    用法::

        def test_x(use_calendar):
            cal = use_calendar(date(2026, 8, 1), date(2026, 9, 30))
            assert signal_freshness_days(...) == 0

    若只需注入一份**自定义**日历（含手工指定的节假日/缺口），用
    ``inject_calendar``。
    """
    import hexbroker.paper.signals as sig

    def _use(
        start: date,
        end: date,
        exclude: Iterable[date] = (),
    ) -> tuple[date, ...]:
        cal = weekdays(start, end, exclude)
        monkeypatch.setattr(sig, "load_trading_calendar", lambda *a, **k: cal, raising=True)
        return cal

    return _use


@pytest.fixture
def inject_calendar(monkeypatch):
    """注入**自定义**交易日历（调用方自备日期序列）。

    返回 ``callable(cal) -> tuple[date, ...]``。用于需要手工安排节假日缺口
    （如春节 2/14~2/23）而不便用 ``use_calendar`` 生成的场景。
    """
    import hexbroker.paper.signals as sig

    def _inject(cal: Iterable[date]) -> tuple[date, ...]:
        frozen = tuple(cal)
        monkeypatch.setattr(sig, "load_trading_calendar", lambda *a, **k: frozen, raising=True)
        return frozen

    return _inject
