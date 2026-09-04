"""回撤口径回归锁（2026-09-04 核查）。

核查结论（务必与交付报告一致）：
``data/paper/account.json`` 里 ``peak_equity = 100000``（等于初始资金、8 个交易日
未抬升）是**数据事实正确**，不是更新逻辑缺失 —— 802 条快照实证：
max equity = 100000.00（开盘首条）、min = 94483.56、**超过 100000 的快照 0 条**。
账户从第一笔起就净亏，峰值就该停在初始资金。

但核查同时挖出一个**潜在口径缺陷**：``update_peak`` 原本只在 ``execute_plan``
内被调用 —— 即**只在成交时**抬升峰值。盘中权益创新高但无成交 → 峰值不抬升 →
``drawdown = (peak - equity) / peak`` 被**系统性低估** → ``risk_hard_stop``
（0.20）触发偏晚甚至永不触发。本文件锁定修复后的口径。

三条铁律：
1. **峰值单调不减**（只升不降）；
2. **分母为零不炸**（peak <= 0 → drawdown 记 0，绝不除零）；
3. **重启后不丢**（save → load 逐位还原）。
外加一条契约：``snapshot()`` **不得**偷偷抬升峰值（用兜底 marks 抬峰会
把 drawdown 推向高估，与漏更新的低估方向相反，但同样有害）。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.types import Plan, Quote

from test_paper_pipeline import MON, _cost

NOW = datetime(2026, 8, 24, 10, 0)
INITIAL = 100_000.0


def _broker(tmp_path) -> PaperBroker:
    return PaperBroker(_cost(), initial_capital=INITIAL, budget_ratio=0.30,
                       data_dir=str(tmp_path))


def _quote(price: float, symbol: str = "rb0") -> Quote:
    return Quote(symbol=symbol, ts=NOW, price=price, open=price, high=price, low=price)


def _open(broker: PaperBroker, symbol: str = "rb0", qty: float = 1.0, price: float = 3000.0) -> None:
    plan = Plan(symbol=symbol, direction=1 if qty > 0 else -1, target_qty=qty,
                target_pos_pct=0.1, stop_price=price * 0.97, take_profit=price * 1.07)
    broker.execute_plan(plan, _quote(price, symbol), NOW)


# --------------------------------------------------------------------------- #
# 1. 单调不减
# --------------------------------------------------------------------------- #
def test_peak_equity_starts_at_initial_capital(tmp_path):
    b = _broker(tmp_path)
    assert b.peak_equity == pytest.approx(INITIAL)
    assert b.snapshot().peak_equity == pytest.approx(INITIAL)


def test_peak_equity_never_decreases(tmp_path):
    """抬到高位后再喂低估值 marks，峰值必须**停在高位**。"""
    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)            # 先建仓，marks 才会影响 equity
    b.update_peak({"rb0": 4000.0})          # 大幅浮盈 → equity 抬升
    raised = b.peak_equity
    assert raised > INITIAL, "前置：峰值应被抬升"

    b.update_peak({"rb0": 1.0})             # 暴跌
    assert b.peak_equity == pytest.approx(raised), "峰值只升不降"
    b.update_peak({})
    assert b.peak_equity == pytest.approx(raised), "空 marks 也不得拉低峰值"


def test_peak_never_below_initial_capital(tmp_path):
    """不变量：峰值恒 >= 初始资金（起点即初始资金，之后只升）。"""
    b = _broker(tmp_path)
    for px in (1.0, 0.0, -100.0, 3000.0, 99999.0):
        b.update_peak({"rb0": px})
        assert b.peak_equity >= INITIAL - 1e-9


# --------------------------------------------------------------------------- #
# 2. 分母为零不炸
# --------------------------------------------------------------------------- #
def test_drawdown_zero_when_peak_is_zero(tmp_path):
    """peak <= 0 → drawdown 记 0，绝不除零（ZeroDivisionError 会打断整轮 tick）。"""
    b = _broker(tmp_path)
    for bad in (0.0, -1.0):
        b._peak_equity = bad
        snap = b.snapshot()
        assert snap.drawdown == pytest.approx(0.0), f"peak={bad} 时 drawdown 必须为 0"
        assert snap.drawdown == snap.drawdown, "不得为 NaN"


def test_drawdown_is_clamped_at_zero_when_above_peak(tmp_path):
    """equity > peak（刚抬升前的瞬间）→ drawdown 夹到 0，不得为负。"""
    b = _broker(tmp_path)
    b._peak_equity = 90_000.0   # 峰值低于当前权益
    snap = b.snapshot()
    assert snap.drawdown == pytest.approx(0.0)
    assert snap.drawdown >= 0.0


# --------------------------------------------------------------------------- #
# 3. 重启后不丢
# --------------------------------------------------------------------------- #
def test_peak_persists_across_restart(tmp_path):
    """save → 新进程 load → 峰值逐位还原。"""
    b = _broker(tmp_path)
    _open(b, price=3000.0)
    b._peak_equity = 123_456.78
    path = b.save_snapshot(tmp_path / "account.json")
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["peak_equity"] == pytest.approx(123_456.78), "落盘字段必须是抬升后的峰值"

    fresh = _broker(tmp_path)   # 模拟重启：全新实例
    assert fresh.peak_equity == pytest.approx(INITIAL), "前置：新实例应回到初始资金"
    assert fresh.load_snapshot(tmp_path / "account.json") is True
    assert fresh.peak_equity == pytest.approx(123_456.78), "重启后峰值必须还原"


def test_drawdown_after_restart_uses_restored_peak(tmp_path):
    """恢复瞬间 drawdown 就应是「用还原的峰值」算出来的，不是 0。

    实证：09-03 重启日志为 `equity=94866.70 peak=100000.00 drawdown=5.13%`。
    """
    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)
    b._peak_equity = 100_000.0
    b.save_snapshot(tmp_path / "account.json")

    fresh = _broker(tmp_path)
    fresh.load_snapshot(tmp_path / "account.json")
    snap = fresh.snapshot({"rb0": 2800.0})     # 权益跌到 98,000 量级
    assert snap.peak_equity == pytest.approx(100_000.0)
    assert snap.drawdown > 0.0, "恢复后不得把 drawdown 抹平为 0"
    assert snap.drawdown == pytest.approx((100_000.0 - snap.equity) / 100_000.0)


def test_missing_peak_in_snapshot_falls_back_to_initial(tmp_path):
    """快照缺 peak_equity 字段（旧版本/手工改过）→ 退回初始资金，不炸。"""
    path = tmp_path / "account.json"
    path.write_text(json.dumps({"positions": {}, "avg_entry": {}, "realized": {}}),
                    encoding="utf-8")
    fresh = _broker(tmp_path)
    assert fresh.load_snapshot(path) is True
    assert fresh.peak_equity == pytest.approx(INITIAL)


# --------------------------------------------------------------------------- #
# 4. 修复点：峰值不能只在成交时更新
# --------------------------------------------------------------------------- #
def test_peak_updates_without_any_trade(tmp_path):
    """修复核心：无成交时权益创新高，峰值也必须抬升。

    修复前 update_peak 只在 execute_plan 内调用 → 这条用例会红。
    """
    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)
    before = b.peak_equity
    assert len(b.trades()) == 1

    b.update_peak({"rb0": 3600.0})   # 持仓大幅浮盈，但**没有任何成交**
    assert len(b.trades()) == 1, "抬升峰值本身不得产生成交"
    assert b.peak_equity > before, "无成交时权益创新高，峰值必须抬升"


def test_stale_peak_understates_drawdown(tmp_path):
    """反证：不抬升峰值会把 drawdown 算小（这正是修复要消除的偏差方向）。"""
    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)

    # 峰值停在成交时点（旧行为）
    stale_drawdown = b.snapshot({"rb0": 2500.0}).drawdown

    # 先按高点抬升峰值，再回落到同一价位（新行为）
    b2 = _broker(tmp_path)
    _open(b2, "rb0", 1.0, 3000.0)
    b2.update_peak({"rb0": 3600.0})
    fresh_drawdown = b2.snapshot({"rb0": 2500.0}).drawdown

    assert fresh_drawdown > stale_drawdown, (
        "峰值越陈旧，drawdown 被低估得越多 —— 低估会让 risk_hard_stop 触发偏晚"
    )


def test_snapshot_never_silently_raises_peak(tmp_path):
    """契约：snapshot() 不得偷偷抬峰。

    若 snapshot 内部自动 update_peak，那么任何一次用「成本价兜底 marks」
    或「部分品种 marks」调 snapshot 的地方都会把峰值抬到虚高 →
    drawdown 高估 → 硬止损误触发。抬峰必须由调用方显式发起。
    """
    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)
    peak_before = b.peak_equity

    b.snapshot({"rb0": 999_999.0})     # 荒谬高 marks
    assert b.peak_equity == pytest.approx(peak_before), "snapshot 不得产生抬峰副作用"

    b.snapshot()                       # 无 marks（成本价兜底）
    assert b.peak_equity == pytest.approx(peak_before)


def test_update_peak_then_snapshot_order_is_stable(tmp_path):
    """先抬峰再快照 = 先快照再抬峰 之后的峰值一致（无顺序依赖陷阱）。"""
    a = _broker(tmp_path)
    _open(a, "rb0", 1.0, 3000.0)
    a.update_peak({"rb0": 3600.0})
    a.snapshot({"rb0": 3000.0})

    b = _broker(tmp_path)
    _open(b, "rb0", 1.0, 3000.0)
    b.snapshot({"rb0": 3000.0})
    b.update_peak({"rb0": 3600.0})

    assert a.peak_equity == pytest.approx(b.peak_equity)


# --------------------------------------------------------------------------- #
# 5. 调度器接线：每 tick / 每次快照落盘前都要抬峰
# --------------------------------------------------------------------------- #
def test_tick_raises_peak_without_any_trade(tmp_path, monkeypatch):
    """锁定修复接线：``_tick`` 必须在建好 marks 后显式抬峰。

    这是本次修复的**两个调用点**（``_tick`` 与 ``_maybe_snapshot``）的回归锁。
    去掉任一处 `self._broker.update_peak(marks)`，峰值就停在初始资金，本例必红。

    隔离手段：``sig=None``（无信号 → 禁止开新仓，全程零成交），
    只靠持仓浮盈抬高权益。
    """
    from test_paper_pipeline import _scheduler

    sched, _ = _scheduler(tmp_path, sig=None)
    _open(sched._broker, "rb0", 1.0, 3000.0)
    assert len(sched._broker.trades()) == 1
    peak_before = sched._broker.peak_equity

    # 把调度器的时间/行情依赖钉死，避免依赖真实时钟与网络
    monkeypatch.setattr(sched._session, "is_tradable", lambda s, now: True)
    monkeypatch.setattr(sched._session, "day_closed", lambda now, buf: None)
    monkeypatch.setattr(sched._session, "day_label", lambda now: MON)
    monkeypatch.setattr(
        sched._quotes,
        "fetch_quotes",
        lambda syms: {s: _quote(3600.0 if s == "rb0" else 1.0, s) for s in syms},
    )

    sched._tick()

    assert len(sched._broker.trades()) == 1, "本轮不得产生任何成交"
    assert sched._broker.peak_equity > peak_before, (
        "无成交时权益创新高，调度器必须抬升峰值（否则 drawdown 被低估、"
        "risk_hard_stop 触发偏晚）"
    )

    # 峰值必须已随本次快照落盘，重启后不丢
    payload = json.loads((tmp_path / "account.json").read_text(encoding="utf-8"))
    assert payload["peak_equity"] > peak_before, "落盘的 peak_equity 应为抬升后的值"


def test_maybe_snapshot_raises_peak_without_any_trade(tmp_path, monkeypatch):
    """单独锁定 ``_maybe_snapshot`` 调用点（300s 快照节奏）。

    与上一例分开是**必须的**：``_tick`` 里也有一处 update_peak，若只测
    ``_tick``，删掉 ``_maybe_snapshot`` 那处测试照样绿（已实证：变异 site2
    后 test_tick_raises_peak_without_any_trade 仍然 PASSED）。
    """
    from test_paper_pipeline import _scheduler

    # 注意：_scheduler 的初始资金是 30 万（test_paper_pipeline._paper_cfg），
    # 不是本文件的 INITIAL=10 万 —— 故一律以 peak_before 为基准，不写死数值。
    sched, _ = _scheduler(tmp_path, sig=None)
    _open(sched._broker, "rb0", 1.0, 3000.0)
    peak_before = sched._broker.peak_equity

    monkeypatch.setattr(sched._session, "day_label", lambda now: MON)
    monkeypatch.setattr(
        sched._quotes,
        "fetch_quotes",
        lambda syms: {s: _quote(3600.0 if s == "rb0" else 1.0, s) for s in syms},
    )

    # 直接驱动快照步骤，**绕开 _tick**（否则 _tick 的调用点会掩盖本点缺失）
    sched._maybe_snapshot(NOW)

    assert len(sched._broker.trades()) == 1, "本步骤不得产生成交"
    assert sched._broker.peak_equity > peak_before, "_maybe_snapshot 必须在落盘前抬峰"
    payload = json.loads((tmp_path / "account.json").read_text(encoding="utf-8"))
    assert payload["peak_equity"] > peak_before
