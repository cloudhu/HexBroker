"""PID 锁进程身份校验（①加固）单测。

验证锁文件升级为 ``PID:CREATION_TIME`` 后：
- 同一进程（创建时间吻合）→ 拒绝重复启动（False）；
- PID 存活但创建时间不符（模拟僵尸锁 / PID 复用）→ 覆盖并获取锁（True）；
- 旧格式（纯整型，无时间指纹）→ 维持原「存活即拒绝」语义（向后兼容）。
"""
import os
from pathlib import Path

from hexbroker.diagnostics.health_check import _pid_creation_time
from scripts.paper_trading_main import _try_acquire_pid_lock


def test_same_process_rejected(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    ct = _pid_creation_time(os.getpid())
    p.write_text(f"{os.getpid()}:{ct}", encoding="utf-8")
    # 本进程存活 + 创建时间吻合 → 拒绝
    assert _try_acquire_pid_lock(p) is False
    assert p.read_text(encoding="utf-8").strip() == f"{os.getpid()}:{ct}"


def test_pid_reuse_overwrites_stale_lock(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    ct = _pid_creation_time(os.getpid())
    # 模拟：旧锁记录的是本进程 PID，但创建时间故意错配（视为被无关进程复用）
    p.write_text(f"{os.getpid()}:{ct + 1}", encoding="utf-8")
    assert _try_acquire_pid_lock(p) is True
    written = p.read_text(encoding="utf-8").strip()
    assert written.startswith(f"{os.getpid()}:")


def test_legacy_format_alive_rejected(tmp_path: Path) -> None:
    """旧格式（纯整型、无时间指纹）存活 → 维持原拒绝语义。"""
    p = tmp_path / "paper.pid"
    p.write_text(str(os.getpid()), encoding="utf-8")
    assert _try_acquire_pid_lock(p) is False
