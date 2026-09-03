"""P1-B：冷却状态落盘改「合并写」（逐品种按 opened_at 新者保留，2026-09-03）。

**缺陷现场**（09-01 实测，deliverables/tqsdk_status_and_trade_analysis_20260902.md §3.2）：
三窗口切换存在 PID 锁缺口，长寿旧进程（内存持有 08-24 ``--smoke`` 污染冷却记录）
优雅退出时 ``_shutdown → _save_cooldown_state`` 用陈旧内存**整体覆写**磁盘，
把 14:30 真实开仓的新鲜记录回滚成 9 天前的陈旧状态，次日 08:55 窗口照单恢复。

**修法**：``_save_cooldown_state`` 落盘前读磁盘，逐品种保留 ``opened_at`` 更新的
记录；磁盘不可读/单条损坏 → fail-open（跳过或整体写内存）。双向防护：

- 内存旧、磁盘新 → 不把磁盘拖回过去（P1-B 主场景）；
- 内存新、磁盘旧 → 正常落盘不被回退（P0-1 主路径不变）。

测试用 ``TradingScheduler.__new__`` 最小实例 —— ``_save_cooldown_state`` 仅触
``_signal_cooldown_persist`` / ``_last_sig_fp`` / ``_cooldown_file`` 三属性，
无需完整构造（行情/风控/计划全部免挂）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from hexbroker.paper.scheduler import TradingScheduler
from hexbroker.paper.types import CooldownRecord

# 08-24 --smoke 污染记录（P1-B 现场原样）：fp 源 "smoke"
_STALE = CooldownRecord(
    fp=(0.66, 0.3, "smoke"),
    opened_at=datetime(2026, 8, 24, 10, 0, 0),
    day="2026-08-24",
    reentries=0,
)
# 09-01 14:30 真实开仓记录（新鲜）
_FRESH = CooldownRecord(
    fp=(0.999999, 1.639979, "engine_a"),
    opened_at=datetime(2026, 9, 1, 14, 30, 18),
    day="2026-09-01",
    reentries=2,
)
# 另一品种的新鲜记录（合并语义用）
_FRESH_AG = CooldownRecord(
    fp=(0.99, 0.8, "engine_b"),
    opened_at=datetime(2026, 9, 1, 21, 50, 0),
    day="2026-09-01",
    reentries=0,
)


def _mk_sched(tmp_path: Path, records: dict) -> TradingScheduler:
    """最小实例：只挂 _save_cooldown_state 触及的三个属性。"""
    sched = TradingScheduler.__new__(TradingScheduler)
    sched._signal_cooldown_persist = True
    sched._cooldown_file = tmp_path / "cooldown.json"
    sched._last_sig_fp = dict(records)
    return sched


def _write_disk(tmp_path: Path, records: dict) -> None:
    payload = {
        "schema_version": "1.0",
        "saved_at": "2026-09-01T21:58:59",
        "records": {
            sym: {
                "fp": [rec.fp[0], rec.fp[1], rec.fp[2]],
                "opened_at": rec.opened_at.isoformat(timespec="seconds"),
                "day": rec.day,
                "reentries": rec.reentries,
            }
            for sym, rec in records.items()
        },
    }
    (tmp_path / "cooldown.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_disk(tmp_path: Path) -> dict:
    payload = json.loads((tmp_path / "cooldown.json").read_text(encoding="utf-8"))
    return payload["records"]


def test_p1b_stale_memory_cannot_rollback_fresh_disk(tmp_path):
    """P1-B 主场景：内存陈旧（08-24 smoke）+ 磁盘新鲜（09-01 开仓）→ 磁盘不被回滚。"""
    _write_disk(tmp_path, {"rb0": _FRESH})
    sched = _mk_sched(tmp_path, {"rb0": _STALE})  # 长寿旧进程的陈旧内存
    sched._save_cooldown_state()
    disk = _read_disk(tmp_path)
    assert disk["rb0"]["opened_at"] == "2026-09-01T14:30:18", (
        "陈旧内存不得把磁盘上的新鲜开仓记录回滚到 9 天前（P1-B）"
    )
    assert disk["rb0"]["fp"][2] == "engine_a"


def test_p1b_fresh_memory_wins_over_stale_disk(tmp_path):
    """反向防护：内存新鲜 + 磁盘陈旧 → 正常落盘（P0-1 主路径不被合并削弱）。"""
    _write_disk(tmp_path, {"rb0": _STALE})
    sched = _mk_sched(tmp_path, {"rb0": _FRESH})
    sched._save_cooldown_state()
    disk = _read_disk(tmp_path)
    assert disk["rb0"]["opened_at"] == "2026-09-01T14:30:18"
    assert disk["rb0"]["reentries"] == 2


def test_p1b_different_symbols_merge(tmp_path):
    """不同品种互补合并：内存 rb0 + 磁盘 ag0 → 两者都保留，互不覆盖。"""
    _write_disk(tmp_path, {"ag0": _FRESH_AG})
    sched = _mk_sched(tmp_path, {"rb0": _FRESH})
    sched._save_cooldown_state()
    disk = _read_disk(tmp_path)
    assert set(disk) == {"rb0", "ag0"}
    assert disk["ag0"]["fp"][2] == "engine_b"
    assert disk["rb0"]["fp"][2] == "engine_a"


def test_p1b_equal_timestamp_memory_wins(tmp_path):
    """opened_at 相同 → 内存（进程自身视角）保留，确定性归一。"""
    same_ts = CooldownRecord(fp=(0.5, 0.1, "mine"), opened_at=_FRESH.opened_at,
                             day="2026-09-01", reentries=1)
    _write_disk(tmp_path, {"rb0": _FRESH})
    sched = _mk_sched(tmp_path, {"rb0": same_ts})
    sched._save_cooldown_state()
    disk = _read_disk(tmp_path)
    assert disk["rb0"]["fp"][2] == "mine"


def test_p1b_corrupt_disk_falls_back_to_memory_write(tmp_path):
    """磁盘 JSON 损坏 → fail-open 整体写内存，绝不抛异常中断交易 tick。"""
    (tmp_path / "cooldown.json").write_text("{ 损坏", encoding="utf-8")
    sched = _mk_sched(tmp_path, {"rb0": _FRESH})
    sched._save_cooldown_state()  # 不得抛
    disk = _read_disk(tmp_path)
    assert disk["rb0"]["fp"][2] == "engine_a"


def test_p1b_malformed_disk_record_skipped(tmp_path):
    """磁盘单条记录结构损坏 → 跳过该条，其余品种合并照常。"""
    payload = {
        "schema_version": "1.0",
        "saved_at": "2026-09-01T21:58:59",
        "records": {
            "ag0": {"fp": "不是列表", "opened_at": "2026-09-01T21:50:00"},
            "cu0": {"fp": [0.9, 0.5, "engine_c"], "opened_at": "2026-09-01T10:00:00",
                    "day": "2026-09-01", "reentries": 0},
        },
    }
    (tmp_path / "cooldown.json").write_text(json.dumps(payload), encoding="utf-8")
    sched = _mk_sched(tmp_path, {"rb0": _FRESH})
    sched._save_cooldown_state()
    disk = _read_disk(tmp_path)
    assert set(disk) == {"rb0", "cu0"}, "损坏条目跳过，健康条目照常合并"
    assert disk["cu0"]["fp"][2] == "engine_c"


def test_p1b_persist_disabled_no_write(tmp_path):
    """persist=False → 完全不写（既有语义不回归）。"""
    sched = _mk_sched(tmp_path, {"rb0": _FRESH})
    sched._signal_cooldown_persist = False
    sched._save_cooldown_state()
    assert not (tmp_path / "cooldown.json").exists()


def test_p1b_tz_aware_disk_record_still_wins(tmp_path):
    """R26g QA 🟡1 回归：磁盘 opened_at 带 tz（aware）不得让整次合并崩塌。

    修复前：naive（内存）与 aware（磁盘）比较抛 TypeError → 外层 except 吞掉
    → fail-open 退回整体写内存 → 陈旧内存回滚新鲜磁盘（恰好复刻 P1-B 主场景）。
    修复后：比较前统一归一化 naive，磁盘新者仍胜。
    """
    payload = {
        "schema_version": "1.0",
        "saved_at": "2026-09-01T21:58:59",
        "records": {
            "rb0": {
                "fp": [0.999999, 1.639979, "engine_a"],
                "opened_at": "2026-09-01T14:30:18+00:00",
                "day": "2026-09-01",
                "reentries": 2,
            }
        },
    }
    (tmp_path / "cooldown.json").write_text(json.dumps(payload), encoding="utf-8")
    sched = _mk_sched(tmp_path, {"rb0": _STALE})  # 陈旧内存 + aware 新鲜磁盘
    sched._save_cooldown_state()  # 不得抛
    disk = _read_disk(tmp_path)
    assert disk["rb0"]["fp"][2] == "engine_a", (
        "aware 磁盘记录应胜过陈旧内存——合并不因 tz 混比较崩塌（QA 🟡1）"
    )
