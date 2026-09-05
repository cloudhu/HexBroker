"""P1-4 任务C 回归锁：异常交易日 08-24 补记 + 计数口径修正。

背景（用户已裁决）
-----------------
2026-08-24 因**三进程并发**导致数据不可信：该日复盘从未生成、_c0_intraday 等
内存态早失，成交/盈亏口径被并发污染。裁决：
  ① 08-24 **计入** trading_day_count（该日是一段真实交易时段）；
  ② 08-24 标为异常日，其成交/盈亏**不参与** 20 日策略评估样本（只留审计）。

锁定契约
--------
1. ``is_anomaly_day``：识别 2026-08-24（date/datetime/str 三种入参皆可）；
2. ``record_trading_day`` **单调**：拒绝"后登记的更早日期"回卷 last_trading_day
   （杜绝回卷导致后续重复计数），同一天幂等；
3. ``count_anomaly_day``：补记异常日只 ``count+=1``，**不改** last_trading_day，
   幂等（同一日重复调用不计第二次），随快照持久化；
4. 评估样本剔除：异常日成交不进入策略评估 sample（审计仍保留）。
"""
from __future__ import annotations

from datetime import date, datetime

from hexbroker.paper.broker import (
    ANOMALY_TRADING_DAYS,
    PaperBroker,
    is_anomaly_day,
)
from hexbroker.backtest.cost import CostModel

A24 = date(2026, 8, 24)


def _cost() -> CostModel:
    return CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                     slippage_ticks=1.0, margin_rate=0.12, multiplier=10.0, min_tick=1.0)


def _broker(data_dir="data/paper_test_anomaly") -> PaperBroker:
    return PaperBroker(_cost(), initial_capital=100_000.0, data_dir=data_dir)


# --------------------------------------------------------------------------- #
# 1. is_anomaly_day 识别
# --------------------------------------------------------------------------- #
def test_default_anomaly_set_contains_0824():
    assert "2026-08-24" in ANOMALY_TRADING_DAYS


def test_is_anomaly_day_recognizes_0824_across_types():
    assert is_anomaly_day(A24) is True, "date 入参应识别 08-24"
    assert is_anomaly_day(datetime(2026, 8, 24, 15, 30)) is True, "datetime 入参应识别"
    assert is_anomaly_day("2026-08-24") is True, "str 入参应识别"


def test_is_anomaly_day_false_for_normal_day():
    assert is_anomaly_day(date(2026, 8, 25)) is False
    assert is_anomaly_day("2026-09-03") is False
    assert is_anomaly_day(None) is False


def test_is_anomaly_day_extra_set_union():
    assert is_anomaly_day(date(2026, 9, 1), extra=frozenset({"2026-09-01"})) is True
    assert is_anomaly_day(A24, extra=frozenset()) is True, "默认集始终生效"


# --------------------------------------------------------------------------- #
# 2. record_trading_day 单调 + 幂等
# --------------------------------------------------------------------------- #
def test_record_trading_day_monotonic_forward():
    b = _broker()
    b.record_trading_day(date(2026, 8, 25))
    assert b.trading_day_count == 1
    b.record_trading_day(date(2026, 8, 26))
    assert b.trading_day_count == 2
    assert b._last_trading_day == date(2026, 8, 26)


def test_record_trading_day_rejects_rewound_earlier_day():
    """后登记更早日期 → 拒绝（防 last_trading_day 回卷 + 重复计数）。"""
    b = _broker()
    b.record_trading_day(date(2026, 9, 3))  # 先登记较晚日期
    assert b._last_trading_day == date(2026, 9, 3)
    assert b.trading_day_count == 1
    b.record_trading_day(date(2026, 8, 24))  # 回卷尝试 → 必须拒绝
    assert b._last_trading_day == date(2026, 9, 3), "last 不得被回卷"
    assert b.trading_day_count == 1, "回卷不得重复计数"


def test_record_trading_day_same_day_idempotent():
    b = _broker()
    b.record_trading_day(date(2026, 8, 25))
    b.record_trading_day(date(2026, 8, 25))
    assert b.trading_day_count == 1, "同一天重复登记不重复计数"


# --------------------------------------------------------------------------- #
# 3. count_anomaly_day 补记（幂等，不改 last）
# --------------------------------------------------------------------------- #
def test_count_anomaly_day_increments_without_rewinding_last():
    b = _broker()
    b.record_trading_day(date(2026, 9, 3))  # 现有 count=1, last=09-03
    assert b.trading_day_count == 1
    # 补记更早的 08-24 → count 2，但 last 仍 09-03
    assert b.count_anomaly_day(A24) is True
    assert b.trading_day_count == 2
    assert b._last_trading_day == date(2026, 9, 3), "count_anomaly_day 不得回卷 last"


def test_count_anomaly_day_is_idempotent():
    b = _broker()
    b.record_trading_day(date(2026, 9, 3))
    assert b.count_anomaly_day(A24) is True
    assert b.count_anomaly_day(A24) is False, "同一异常日重复补记不计第二次"
    assert b.trading_day_count == 2


def test_count_anomaly_day_persists_across_restart(tmp_path):
    b = _broker(data_dir=str(tmp_path))
    b.record_trading_day(date(2026, 9, 3))
    b.count_anomaly_day(A24)
    path = b.save_snapshot(tmp_path / "account.json")

    b2 = _broker(data_dir=str(tmp_path))
    assert b2.load_snapshot(path) is True
    assert b2.trading_day_count == 2, "重启后补计异常日计数必须存活"
    assert A24.isoformat() in b2._counted_anomaly_days
    # 重启后再补记同一异常日 → 幂等（不计第二次）
    assert b2.count_anomaly_day(A24) is False
    assert b2.trading_day_count == 2


def test_record_trading_day_normal_forward_after_anomaly_count_still_works():
    """先补记异常日、再正常登记次日 → count 正常累进。"""
    b = _broker()
    b.record_trading_day(date(2026, 8, 25))  # count=1 last=08-25
    b.count_anomaly_day(A24)  # count=2 (补记早于 last 的 08-24)
    assert b.trading_day_count == 2
    b.record_trading_day(date(2026, 8, 26))  # 前向登记 → count=3
    assert b.trading_day_count == 3
    assert b._last_trading_day == date(2026, 8, 26)


# --------------------------------------------------------------------------- #
# 4. 评估样本剔除逻辑（is_anomaly_day 过滤）
# --------------------------------------------------------------------------- #
def test_evaluation_sample_exclusion_filter():
    """异常日成交应从评估 sample 剔除（审计仍保留在 _all_trades）。"""
    from hexbroker.paper.types import TradeEvent

    # 构造一个含 08-24 + 正常日成交的 _all_trades，模拟 scheduler 评估路径的剔除
    trades = [
        TradeEvent(trade_id="T1", ts=datetime(2026, 8, 24, 10, 0), symbol="rb0",
                   direction=1, qty=1.0, entry=3000.0, stop=None, take_profit=None,
                   price=3000.0, fee=1.0, is_open=True),
        TradeEvent(trade_id="T2", ts=datetime(2026, 8, 28, 10, 0), symbol="rb0",
                   direction=-1, qty=1.0, entry=3000.0, stop=None, take_profit=None,
                   price=2900.0, fee=1.0, is_open=False),
    ]
    sample = [t for t in trades if not is_anomaly_day(getattr(t, "ts", None))]
    assert len(sample) == 1, "08-24 成交应被剔除出评估样本"
    assert sample[0].ts == datetime(2026, 8, 28, 10, 0)
    assert len(trades) == 2, "审计轨迹 _all_trades 仍保留全部（不物理删除）"


# --------------------------------------------------------------------------- #
# 5. load_snapshot 自动补记异常日（P1-4 自动补记，2026-09-05）
# --------------------------------------------------------------------------- #
def test_load_snapshot_auto_counts_anomaly_days(tmp_path):
    """load_snapshot 必须把 ANOMALY_TRADING_DAYS 自动计入 count（不回卷 last）。

    这样任意一次重启都会补记 08-24，无需手动触发；幂等（已计入不重复 +1）。
    """
    import json

    b = _broker(data_dir=str(tmp_path))
    b.record_trading_day(date(2026, 9, 3))  # count=1, last=09-03, 异常日未手动补记
    path = b.save_snapshot(tmp_path / "account.json")
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert "2026-08-24" not in snap.get("counted_anomaly_days", [])

    b2 = _broker(data_dir=str(tmp_path))
    assert b2.load_snapshot(path) is True
    assert "2026-08-24" in b2._counted_anomaly_days, "重启后钩子应自动补记 08-24"
    assert b2.trading_day_count == 2, "1(09-03) + 1(08-24 自动补记)"
    assert b2._last_trading_day == date(2026, 9, 3), "自动补记不得回卷 last"

    # 再加载同一快照（幂等）→ 不重复计数
    b3 = _broker(data_dir=str(tmp_path))
    assert b3.load_snapshot(path) is True
    assert b3.trading_day_count == 2, "幂等：重复加载不得重复 +1"
