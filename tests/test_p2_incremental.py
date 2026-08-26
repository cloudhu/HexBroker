"""P2-3 报价时效校验 + P2-2 最小持仓时长（今平门）单测。

复用 test_scheduler_halt 的共享脚手架（_scheduler / _q / RecordingLog / _sig /
StaleSignals / ValidQuotes）。通过把 tests/ 插入 sys.path 复用，避免重复装配整条
调度链路。

P2-3 报价时效：单报价年龄 = now - quote.ts 超 ``quote_max_age_sec`` 视为过期，跳过撮合
（warning + 无成交），与 P0-2 HALT（连续失败语义）互补。

P2-2 最小持仓时长（今平门）：持仓不足 N 分钟且为今平（当日新开）时拦截平今，从节奏降
今平频率；不拦风控强平（liquidate=True）；held>=min 或平昨（非今开）则正常。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# 复用 test_scheduler_halt 的调度器脚手架
sys.path.insert(0, str(Path(__file__).parent))
from test_scheduler_halt import (  # noqa: E402
    RecordingLog,
    StaleSignals,
    ValidQuotes,
    _q,
    _scheduler,
    _sig,
    scheduler_mod,
)

from hexbroker.paper.types import Quote  # noqa: E402
from hexbroker.risk.types import RiskDecision  # noqa: E402


# ===========================================================================
# P2-3 报价时效校验
# ===========================================================================
def test_stale_quote_skipped(tmp_path, monkeypatch):
    """陈旧 ts 的 Quote（age > quote_max_age_sec）应被 _process_symbol 跳过：
    打印过期告警 + 无成交、无持仓变化。"""
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    now = datetime(2026, 8, 24, 10, 0, 0)
    # 默认 quote_max_age_sec=3×poll_interval=180s；陈旧 300s 触发过期
    stale_ts = now - timedelta(seconds=300)
    q = Quote(
        symbol="rb0", ts=stale_ts, price=3038.0,
        open=3030.0, high=3040.0, low=3030.0, pre_settle=3030.0,
    )
    sched._process_symbol("rb0", now, q, {"rb0": 3038.0})
    assert sched._broker.position("rb0") == 0.0
    assert sched._all_trades == []
    assert any("行情过期" in w for w in rec.warnings)


def test_fresh_quote_not_skipped(tmp_path, monkeypatch):
    """新鲜 ts（age≈0）不应触发过期，正常走信号/风控链路（本例无有效持仓变化也至少不报错）。"""
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    now = datetime(2026, 8, 24, 10, 0, 0)
    q = Quote(
        symbol="rb0", ts=now, price=3038.0,
        open=3030.0, high=3040.0, low=3030.0, pre_settle=3030.0,
    )
    # 新鲜行情：不打印「行情过期」告警
    sched._process_symbol("rb0", now, q, {"rb0": 3038.0})
    assert not any("行情过期" in w for w in rec.warnings)


# ===========================================================================
# P2-2 最小持仓时长（今平门）
# ===========================================================================
class _FakeRG:
    """最小风控门：evaluate 直接返回预设决策，隔离 RiskManager 细节。"""

    def __init__(self, decision: RiskDecision) -> None:
        self._d = decision

    def evaluate(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._d

    def set_cost(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass


def _build_sched(tmp_path, min_hold: int):
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    sched._min_hold_minutes = min_hold
    # 平今门测试用「目标平仓」决策（target=0, liquidate=False）驱动减仓路径
    sched._risk_gate = _FakeRG(RiskDecision(target_position=0.0, liquidate=False))
    return sched


def _open_one_lot(sched, ts: datetime) -> None:
    """直接经 SimBroker 开 1 手并记录开仓时刻（绕过信号/风控链路）。"""
    sched._broker._broker.execute("rb0", 1.0, ref_price=3038.0, timestamp=ts)
    sched._broker._broker.open_dates["rb0"] = ts


def test_min_hold_blocks_today_close_when_held_lt_min(tmp_path):
    """今平且 held<min → 拦截平今，持仓维持、无新成交。"""
    sched = _build_sched(tmp_path, 5)
    now = datetime(2026, 8, 24, 10, 0, 0)
    _open_one_lot(sched, now)  # 今开，held=0 < 5
    before = abs(sched._broker.position("rb0"))
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    after = abs(sched._broker.position("rb0"))
    assert after == before and after > 0  # 持仓维持
    assert len(sched._all_trades) == 0    # 无新成交


def test_min_hold_allows_when_held_ge_min(tmp_path):
    """held>=min（同今开但已持满）→ 不拦截，正常平仓。"""
    sched = _build_sched(tmp_path, 5)
    now = datetime(2026, 8, 24, 10, 0, 0)
    _open_one_lot(sched, now - timedelta(minutes=10))  # held=10 >= 5
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) < 1e-9
    assert len(sched._all_trades) == 1


def test_min_hold_allows_yesterday_close(tmp_path):
    """平昨（open_ts 非今日）→ 不拦截，正常平仓。"""
    sched = _build_sched(tmp_path, 5)
    now = datetime(2026, 8, 24, 10, 0, 0)
    yday = now - timedelta(days=1)
    _open_one_lot(sched, yday)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) < 1e-9
    assert len(sched._all_trades) == 1


def test_min_hold_does_not_block_liquidate(tmp_path):
    """风控强平（liquidate=True）不被 min_hold 拦截，正常平仓。"""
    sched = _build_sched(tmp_path, 5)
    now = datetime(2026, 8, 24, 10, 0, 0)
    _open_one_lot(sched, now)  # 今开 held=0 < 5
    sched._risk_gate._d = RiskDecision(target_position=0.0, liquidate=True)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) < 1e-9
    assert len(sched._all_trades) == 1
