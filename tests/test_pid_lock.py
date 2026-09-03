"""P1-C 单实例 OS 句柄锁互斥单测（scripts/paper_trading_main._acquire_instance_lock）。

新语义（方案①，2026-09-03 终裁）：
- 互斥由 OS 仲裁（msvcrt.locking / fcntl.flock），句柄持到进程退出；
- 锁文件常驻不删，内容 ``PID:CREATION_TIME`` 仅作事后取证；
- 锁被占用 → 返回 None（调用方拒启，fail-closed）；
- 进程死亡 → OS 自动释放，无僵尸锁（子进程实证）。

旧内容指纹式语义（读→判活→覆盖）已退役——P1-C 取证定罪其两处 fail-open
出口是 09-01 多进程并存的根因，见 deliverables/p1c_pid_lock_forensics_20260903.md。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.paper_trading_main import _acquire_instance_lock

ROOT = Path(__file__).resolve().parents[1]


def test_acquire_returns_handle_and_writes_content(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    lock = _acquire_instance_lock(p)
    assert lock is not None
    try:
        content = p.read_text(encoding="utf-8").strip()
        assert content.split(":")[0] == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_in_same_process_rejected(tmp_path: Path) -> None:
    """同进程（不同句柄）二次加锁 → None（OS 仲裁互斥，同进程也不豁免）。"""
    p = tmp_path / "paper.pid"
    first = _acquire_instance_lock(p)
    assert first is not None
    try:
        second = _acquire_instance_lock(p)
        assert second is None
        # 被拒的 acquire 不得覆写首持有者内容
        assert p.read_text(encoding="utf-8").strip().split(":")[0] == str(os.getpid())
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path: Path) -> None:
    p = tmp_path / "paper.pid"
    first = _acquire_instance_lock(p)
    assert first is not None
    first.release()
    second = _acquire_instance_lock(p)
    assert second is not None
    second.release()


def test_lock_file_resident_after_release(tmp_path: Path) -> None:
    """锁文件常驻不删（句柄锁语义；unlink 与 TOCTOU 回归点已废除）。"""
    p = tmp_path / "paper.pid"
    lock = _acquire_instance_lock(p)
    assert lock is not None
    last_content = p.read_text(encoding="utf-8").strip()
    lock.release()
    assert p.exists()
    assert p.read_text(encoding="utf-8").strip() == last_content


def test_preexisting_garbage_content_overwritten(tmp_path: Path) -> None:
    """预存在损坏内容（旧格式/垃圾）→ 直接重写，无需解析。"""
    p = tmp_path / "paper.pid"
    p.write_text("not-a-pid", encoding="utf-8")
    lock = _acquire_instance_lock(p)
    assert lock is not None
    try:
        assert p.read_text(encoding="utf-8").strip().split(":")[0] == str(os.getpid())
    finally:
        lock.release()


def test_subprocess_holds_lock_then_os_auto_releases(tmp_path: Path) -> None:
    """跨进程互斥 + 僵尸锁免疫实证：

    子进程持锁 → 父进程 acquire 必拒（None）；
    子进程被杀（未优雅释放）→ OS 自动关句柄释放锁 → 父进程立即可复得。
    这是 P1-C 方案①的核心价值：互斥质量不再依赖任何存活判定。
    """
    p = tmp_path / "paper.pid"
    # 注意：-c 代码串必须用真实换行（try: 是复合语句，分号续行是 SyntaxError）
    child_code = "\n".join([
        "import sys, time",
        f"sys.path.insert(0, r'{ROOT}')",
        "from pathlib import Path",
        "from scripts.paper_trading_main import _acquire_instance_lock",
        "try:",
        f"    lock = _acquire_instance_lock(Path(r'{p}'))",
        "    print('LOCKED' if lock is not None else 'REJECTED', flush=True)",
        "    time.sleep(30)",
        "except Exception as exc:",
        "    print(f'ERROR:{type(exc).__name__}', flush=True)",
    ])
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(ROOT),
    )
    try:
        line = proc.stdout.readline().strip() if proc.stdout else ""
        assert line == "LOCKED", f"子进程未取得锁：{line!r}"
        # 子进程持锁期间 → 父进程被拒
        assert _acquire_instance_lock(p) is None
    finally:
        proc.kill()
        proc.wait(timeout=10)
        if proc.stdout:
            proc.stdout.close()
    # 子进程已被杀（非优雅退出）→ OS 自动释放 → 父进程可复得。
    # 容错重试 2s：句柄关闭/锁释放属内核异步路径（独立脚本实测 ≤0.05s）。
    parent_lock = None
    for _ in range(40):
        parent_lock = _acquire_instance_lock(p)
        if parent_lock is not None:
            break
        time.sleep(0.05)
    assert parent_lock is not None, (
        f"子进程 returncode={proc.returncode}，被杀后 2s 内锁仍未释放——"
        "若稳定复现说明锁被其他存活进程持有（检查进程树/沙箱 shim），非 OS 语义问题"
    )
    parent_lock.release()
