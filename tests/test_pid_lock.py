"""PID 锁启动互斥单测（scripts/paper_trading_main._try_acquire_pid_lock）。

场景：无锁文件 / 锁文件为存活进程（拒绝）/ 僵尸 pid（覆盖）/ 损坏内容（覆盖）。
"""

import os
from pathlib import Path

from scripts.paper_trading_main import _try_acquire_pid_lock


def test_pid_lock_no_file(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    assert _try_acquire_pid_lock(p) is True
    assert p.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_pid_lock_alive_rejected(tmp_path: Path) -> None:
    """锁文件为本进程（存活）→ 拒绝第二实例（返回 False），文件内容不变。"""
    p = tmp_path / "paper.pid"
    p.write_text(str(os.getpid()), encoding="utf-8")
    assert _try_acquire_pid_lock(p) is False
    assert p.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_pid_lock_zombie_overwrite(tmp_path: Path) -> None:
    """僵尸 pid（进程已死）→ 覆盖并获取锁。"""
    p = tmp_path / "paper.pid"
    p.write_text("999999999", encoding="utf-8")
    assert _try_acquire_pid_lock(p) is True
    assert p.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_pid_lock_garbage_overwrite(tmp_path: Path) -> None:
    """锁文件内容损坏 → 容错覆盖。"""
    p = tmp_path / "paper.pid"
    p.write_text("not-a-pid", encoding="utf-8")
    assert _try_acquire_pid_lock(p) is True
    assert p.read_text(encoding="utf-8").strip() == str(os.getpid())
