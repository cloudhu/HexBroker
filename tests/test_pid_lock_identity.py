"""P1-C 锁文件诊断内容单测（``PID:CREATION_TIME`` 取证格式）。

句柄锁（P1-C 方案①）互斥由 OS 仲裁，内容不再参与互斥判定，仅作事后取证：
- 成功 acquire 后内容 = ``<pid>:<creation_time>``；
- 释放后内容保留（记录最后持有者身份）；
- 被拒的 acquire 不得覆写既有内容。

旧「身份校验语义」（存活+创建时间吻合才拒绝）已随内容指纹式锁一并退役。
"""

import os
from pathlib import Path

from hexbroker.diagnostics.health_check import _pid_creation_time
from scripts.paper_trading_main import _acquire_instance_lock


def test_content_is_pid_ct_format(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    lock = _acquire_instance_lock(p)
    assert lock is not None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        pid_part, ct_part = raw.split(":")
        assert pid_part == str(os.getpid())
        assert int(ct_part) == _pid_creation_time(os.getpid())
    finally:
        lock.release()


def test_content_survives_release(tmp_path: Path) -> None:
    """release 后文件常驻且内容不变——事后可取证最后持有者。"""
    p = tmp_path / "paper.pid"
    lock = _acquire_instance_lock(p)
    assert lock is not None
    expected = f"{os.getpid()}:{_pid_creation_time(os.getpid())}"
    lock.release()
    assert p.read_text(encoding="utf-8").strip() == expected


def test_rejected_acquire_keeps_holder_content(tmp_path: Path) -> None:
    """锁被占时的新 acquire 被拒，不得覆写首持有者诊断内容。"""
    p = tmp_path / "paper.pid"
    first = _acquire_instance_lock(p)
    assert first is not None
    expected = f"{os.getpid()}:{_pid_creation_time(os.getpid())}"
    second = _acquire_instance_lock(p)
    assert second is None
    assert p.read_text(encoding="utf-8").strip() == expected
    first.release()
