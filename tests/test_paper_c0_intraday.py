"""P1-4 c0 日内积累中间态持久化测试（Task D：accumulate 毕业复活）。

背景：``_c0_intraday`` 原为纯内存 dict —— 采集进程逐 tick 累积，进程退出即失；
但收盘复盘（``_on_close``，如 20:5x 新进程）跑在**无当日盘中数据**的进程里，
``_append_c0_daily`` 里 ``_c0_intraday.get(day)`` 恒 None → c0_daily.csv 永不生成
→ ``_c0_daily_row_count()`` 恒 0 → accumulate 30 日自动切 trade 是死代码。

修复：每 tick 把 ``_c0_intraday`` 原子写 json 中间态（G5：tmp + os.replace），
新进程启动时 load 恢复当日已收数据 → 落盘进程解耦。c0_daily.csv 仍是
「整日完成行」，毕业计数按真实完成交易日推进，不会因逐 tick 部分行假毕业。

本文件验证：
  1. ``_track_accumulate`` 落盘 → 重启（新 scheduler 同 tmp 中间态文件）恢复当日 OHLC；
  2. ``_on_close`` 把已收盘日从中间态剔除 → 重启不会重复追加 CSV（防重行）；
  3. 无盘中数据的「收盘进程」依赖中间态恢复即可生成 c0_daily.csv → 毕业计数推进；
  4. 中间态文件缺失/损坏 → 静默按空处理（R22，不新增停摆模式）。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from hexbroker.paper.types import Quote
from test_paper_pipeline import _DAY, _paper_cfg, _scheduler, _eff_signal

_MON = date(2026, 8, 24)
_DAY2 = date(2026, 8, 25)

# 含 c0 accumulate 模式的符号集（与 test_paper_pipeline 对齐）
_C0_SYMS = {
    "rb0": {"display": "SHFE.rb", "sina_code": "nf_RB0", "multiplier": 10, "min_tick": 1, "mode": "trade", "sessions": {"day": _DAY, "night": [["21:00", "23:00"]]}},
    "c0": {"display": "DCE.c", "sina_code": "nf_C0", "multiplier": 10, "min_tick": 1, "mode": "accumulate", "accumulate_days": 30, "sessions": {"day": _DAY, "night": []}},
}


def _c0_scheduler(tmp_path):
    """构造含 c0 且全部落盘指向 tmp 的调度器（含 c0_intraday_file 隔离）。"""
    paper_cfg = _paper_cfg(_C0_SYMS)
    sched, ctx = _scheduler(tmp_path, paper_cfg=paper_cfg, sig=_eff_signal())
    return sched, ctx, paper_cfg


def _track(sched, day: date, open_px=2260.0, high=2275.0, low=2255.0, close=2265.0) -> None:
    """在 day 上跑一次 c0 盘中累积。"""
    sched._track_accumulate(
        "c0",
        Quote(symbol="c0", ts=datetime(2026, 8, 24, 10, 0), price=close, open=open_px, high=high, low=low),
        day,
    )


# ---------------------------------------------------------------------------
# 1. 中间态落盘 + 跨进程（重启）恢复
# ---------------------------------------------------------------------------
def test_track_persists_and_restart_recovers(tmp_path):
    """A 进程盘中累积 → 落盘；B 进程（同中间态文件）启动 load 恢复当日 OHLC。"""
    sched_a, _, _ = _c0_scheduler(tmp_path)
    _track(sched_a, _MON)

    mid_file = tmp_path / "c0_intraday.json"
    assert mid_file.exists(), "每 tick 后应已原子写中间态文件"
    payload = json.loads(mid_file.read_text(encoding="utf-8"))
    assert _MON.isoformat() in payload, "中间态应含当日键"

    # 模拟进程重启：同 cfg/tmp 再建一个调度器 → __init__ 应 load 恢复
    sched_b, _, _ = _c0_scheduler(tmp_path)
    stats = sched_b._c0_intraday.get(_MON)
    assert stats is not None, "重启进程应恢复当日 c0 日内积累"
    assert stats["open"] == pytest.approx(2260.0)
    assert stats["high"] == pytest.approx(2275.0)
    assert stats["low"] == pytest.approx(2255.0)
    assert stats["close"] == pytest.approx(2265.0)


# ---------------------------------------------------------------------------
# 2. _on_close 剔除已收盘日 → 重启不重复追加
# ---------------------------------------------------------------------------
def test_on_close_removes_day_then_restart_no_dup(tmp_path):
    """A 进程盘中累积 → _on_close 收盘落盘并把当日从中间态剔除；
    B 进程重启不应再带当日 → 不会重复 append 生成重复 CSV 行。"""
    sched_a, _, paper_cfg = _c0_scheduler(tmp_path)
    _track(sched_a, _MON)
    sched_a._on_close(_MON)   # 收盘：append CSV + 清理 _c0_intraday[day] + save

    mid_file = tmp_path / "c0_intraday.json"
    payload = json.loads(mid_file.read_text(encoding="utf-8"))
    assert _MON.isoformat() not in payload, "已收盘之日应从中间态剔除（_on_close 末尾 save）"

    csv = tmp_path / "c0_daily.csv"
    rows = csv.read_text(encoding="utf-8").splitlines()
    assert sum(1 for r in rows if r.startswith("2026-08-24,")) == 1, "收盘落盘应只有 1 行当日行"

    # 重启进程：不得把已收盘日当作待落盘日恢复 → 再 _on_close 不重复 append
    sched_b, _, _ = _c0_scheduler(tmp_path)
    assert _MON not in sched_b._c0_intraday, "重启进程不得把已收盘日当作待落盘日恢复"
    sched_b._on_close(_MON)
    rows2 = csv.read_text(encoding="utf-8").splitlines()
    assert sum(1 for r in rows2 if r.startswith("2026-08-24,")) == 1, "重启后 _on_close 不得重复 append"


# ---------------------------------------------------------------------------
# 3. accumulate 毕业复活：无盘中数据的收盘进程靠中间态生成 c0_daily.csv
# ---------------------------------------------------------------------------
def test_fresh_close_process_graduates_from_intermediate(tmp_path):
    """核心回归：采集进程盘中累积后退出；全新「收盘进程」（无盘中 tick）启动，
    靠中间态恢复即可 _append_c0_daily 生成日线 → 毕业计数可推进。"""
    # 采集进程：盘中累积 1 天并持久化
    sched_collect, _, _ = _c0_scheduler(tmp_path)
    _track(sched_collect, _MON)

    # 收盘进程：全新实例（等价于 20:5x 无当日盘中的新进程）
    sched_close, ctx, paper_cfg = _c0_scheduler(tmp_path)
    assert sched_close._c0_intraday.get(_MON), "收盘进程应通过中间态恢复当日数据"
    sched_close._append_c0_daily(_MON)

    csv = tmp_path / "c0_daily.csv"
    assert csv.exists(), "收盘进程应能生成 c0_daily.csv（此前死代码）"
    rows = csv.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "date,open,high,low,close,settle"
    assert rows[1].startswith("2026-08-24,"), f"首行应为 2026-08-24 整日行: {rows[1]}"
    # 1 行 < 30 → 仍 accumulate（毕业需满 30 真实完成交易日）
    assert sched_close._effective_mode("c0") == "accumulate"


def test_graduation_after_30_full_rows(tmp_path):
    """满 30 个真实完成交易日 → 自动切 trade（毕业从死代码复活后的最终目标）。"""
    sched_collect, _, _ = _c0_scheduler(tmp_path)
    for i in range(30):
        d = date(2026, 7, 1 + i)
        _track(sched_collect, d, open_px=2200.0 + i, high=2220.0 + i, low=2190.0 + i, close=2210.0 + i)
    # 收盘进程把 30 天逐日落盘
    sched_close, _, paper_cfg = _c0_scheduler(tmp_path)
    for i in range(30):
        d = date(2026, 7, 1 + i)
        assert d in sched_close._c0_intraday
        sched_close._append_c0_daily(d)
    assert sched_close._c0_daily_row_count() >= 30
    assert sched_close._effective_mode("c0") == "trade", "满 30 完成日应自动切 trade"


# ---------------------------------------------------------------------------
# 4. R22：中间态缺失/损坏 → 静默按空，不新增停摆模式
# ---------------------------------------------------------------------------
def test_missing_intermediate_is_silent_empty(tmp_path):
    """中间态文件不存在 → 不抛错、按空处理。"""
    sched, _, _ = _c0_scheduler(tmp_path)
    assert sched._c0_intraday == {}
    # 且仍可继续盘中累积（不因文件缺失而失败）
    _track(sched, _MON)
    assert sched._c0_intraday.get(_MON) is not None


def test_corrupt_intermediate_is_silent_empty(tmp_path):
    """中间态文件损坏（非法 JSON/非 dict/坏日期键）→ 静默跳过坏条目，不抛错。"""
    mid_file = tmp_path / "c0_intraday.json"
    # 非法 JSON
    mid_file.write_text("{ not json", encoding="utf-8")
    sched, _, _ = _c0_scheduler(tmp_path)
    assert sched._c0_intraday == {}

    # 合法 JSON 但含坏条目（非 dict 值 / 非法日期键）→ 应被跳过，好条目保留
    good = {"2026-08-25": {"open": 2200.0, "high": 2220.0, "low": 2180.0, "close": 2210.0}}
    payload = {
        "2026-08-24": [1, 2, 3],          # 非 dict → 跳过
        "not-a-date": {"open": 1.0},      # 非法日期 → 跳过
        "2026-99-99": {"open": 1.0},      # 非法日期 → 跳过
        **good,
    }
    mid_file.write_text(json.dumps(payload), encoding="utf-8")
    sched2, _, _ = _c0_scheduler(tmp_path)
    assert date(2026, 8, 24) not in sched2._c0_intraday, "非 dict 条目应被跳过"
    assert sched2._c0_intraday.get(date(2026, 8, 25))["close"] == pytest.approx(2210.0)
