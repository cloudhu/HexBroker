#!/usr/bin/env python
"""启动 HexBroker 模拟盘交易系统于后台（无可见窗口，运维安全）。

P0-A（2026-09-04 根因修复）：
原实现以 ``CREATE_NEW_CONSOLE (0x10)`` 弹出**独立可见控制台窗口**，该窗口被关闭 =
交易引擎被系统直接杀死（CTRL_CLOSE_EVENT，非 SIGTERM），且不写 ``_shutdown`` 日志，
导致「日志静止 + 无 shutdown = 被外部杀」且下次需靠夜盘重启补复盘。

现改为：
- ``CREATE_NEW_PROCESS_GROUP (0x200)``：引擎脱离父进程组，launcher 退出不影响引擎；
- ``CREATE_NO_WINDOW (0x08000000)``：引擎**完全没有可见窗口** → 物理上消除「可误关的
  窗口」这一故障面；
- ``stdout/stderr`` 重定向到 ``logs/paper_console.log``，保留原窗口的启动/行情输出。

主程序自带 ``data/paper/paper.pid`` 进程锁，重复调用安全拒绝（「已有存活实例」），天然幂等。

仅做启动，不修改任何生产代码。
"""
import io
import os
import subprocess
import sys

ROOT = r"E:\Workspace\HexBroker"
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
MAIN = os.path.join(ROOT, "scripts", "paper_trading_main.py")

# 进程组隔离：引擎脱离 launcher 所在控制台组，launcher 退出/被杀均不影响引擎。
CREATE_NEW_PROCESS_GROUP = 0x200
# 完全无可见窗口：消除「误关窗口杀进程」故障面（替代原 CREATE_NEW_CONSOLE）。
CREATE_NO_WINDOW = 0x08000000
# 旧名（仅文档引用，已弃用）：原实现用它弹出可见窗口，是 P0-A 根因。
CREATE_NEW_CONSOLE = 0x10

CONSOLE_LOG = os.path.join(ROOT, "logs", "paper_console.log")


def _engine_creationflags() -> int:
    """引擎子进程创建标志：独立进程组 + 无窗口。"""
    return CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _open_console_log() -> "io.TextIOWrapper":
    log_dir = os.path.dirname(CONSOLE_LOG)
    os.makedirs(log_dir, exist_ok=True)
    # 行缓冲，保证崩溃时也已落盘
    return open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)


def main() -> int:
    if not os.path.exists(MAIN):
        print(f"[LAUNCH][ERROR] 入口脚本缺失: {MAIN}")
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONUNBUFFERED"] = "1"

    logf = _open_console_log()
    proc = subprocess.Popen(
        [PY, MAIN],
        cwd=ROOT,
        env=env,
        creationflags=_engine_creationflags(),
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    print(
        f"[LAUNCH] 已在后台（无窗口）拉起交易系统, pid={proc.pid} "
        f"—— 控制台输出见 {CONSOLE_LOG}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
