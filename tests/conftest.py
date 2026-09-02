"""共享测试夹具。

P3-C（2026-08-31）后，信号新鲜度口径依赖**交易日历**（主湖全品种并集）。
任何涉及新鲜度的用例都必须显式注入日历，否则：

- 本地开发机主湖存在 → 日历可用，但日期随数据更新而漂移（用例不可复现）；
- CI 全新 checkout 主湖缺失（数据产物不入 git）→ 日历为空 → 全部判为
  「无法判定」，用例结果与真实语义脱节。

``use_calendar`` 把日历固定在用例手里，使两类环境行为一致。

⛔ P1-8（2026-09-01）：本模块还提供一个 **session 级 autouse 守卫**
``_isolate_production_audit_log``，禁止测试把结构化审计事件写进生产日志
``data/paper/trades.log``。详见该 fixture 的文档串。
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
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


# ----------------------------------------------------------------------
# ⛔ P1-8（2026-09-01）：生产审计日志隔离守卫
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def _isolate_production_audit_log(tmp_path_factory):
    """禁止测试把结构化审计事件写进**生产**日志 ``data/paper/trades.log``。

    **背景（实测取证）**：``log_structured()``（``hexbroker/utils/logging.py``）
    用 loguru **广播到所有已注册 sink**。全量回归里只要有一个用例构建了生产组件
    （``build_components_safe`` → ``TradeLogger()`` 用默认路径），生产文件 sink
    就注册了；此后**任何**用例的 ``log_structured`` 都会落盘到生产日志。

    实测污染：``tests/test_size_qty_risk_cap.py`` + ``tests/test_trade_logger.py``
    同时跑，稳定写入 2 行伪造 trade 事件（`T000259 ag0 平今` 与
    ``ts=2026-08-24T09:05:30``）。而生产调度器本身会读这个文件做统计
    （``scheduler.py`` → ``analyze_trades_log``），
    其中 ``raw_count``/``unique_count``/``copy_dist`` 按**全文件**计算、
    按日过滤在其**之后** → 去重统计直接被污染。

    **修法（选项 A：只动测试，生产代码零改动）**：守卫打在
    ``hexbroker.paper.logger._ensure_sinks`` 上——它是**唯一**的文件 sink 注册点。
    凡目标路径不在系统临时目录内（即非 pytest 的 ``tmp_path``），一律重定向到
    本次会话的沙箱文件。

    ⛔ 为什么不在生产代码里判断（选项 B）：``sys.modules`` / ``PYTEST_CURRENT_TEST``
    之类的探测会让**生产代码带上测试意识**，对交易系统是不可接受的耦合。
    守卫放在测试侧，生产侧保持纯粹。

    ⛔ 已知边界：本 fixture 在 session 首次用例前生效。若将来有用例在**模块级**
    （import 期）就构造 ``TradeLogger``，会早于守卫——届时需在 conftest 顶部
    提前打补丁。当前代码库无此情况。

    返回沙箱日志路径，供 ``tests/test_trade_logger.py`` 的契约用例断言。
    """
    import hexbroker.paper.logger as plog

    tmp_root = Path(tempfile.gettempdir()).resolve()
    sandboxed = tmp_path_factory.mktemp("paper_audit_sandbox") / "trades.log"
    original = plog._ensure_sinks

    def _guarded(log_file):
        target = Path(log_file)
        try:
            resolved = target.resolve()
        except OSError:  # 路径不存在时 resolve 可能失败（strict 模式）
            resolved = target.absolute()
        # pytest 的 tmp_path 一律在系统临时目录下 —— 这些是测试自有文件，放行
        if str(resolved).startswith(str(tmp_root)):
            return original(log_file)
        # 其余一律视为生产路径，重定向到沙箱
        return original(str(sandboxed))

    plog._ensure_sinks = _guarded
    try:
        yield sandboxed
    finally:
        plog._ensure_sinks = original
