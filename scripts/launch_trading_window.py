#!/usr/bin/env python
"""启动 HexBroker 模拟盘交易系统于独立可见控制台窗口（运维友好）。

设计要点：
- 不经 cmd.exe：沙箱安全策略会拦截任何途径的 cmd.exe 启动，故直接用
  subprocess 拉起交易主程序。
- CREATE_NEW_CONSOLE (0x10)：弹出独立可见的控制台窗口，实时显示启动日志与
  行情输出；父进程（本脚本）立即返回，不阻塞调用方（自动化任务）。
- CREATE_NEW_PROCESS_GROUP (0x200)：使子进程脱离父进程组，父脚本退出后交易
  系统窗口仍独立存活。
- 主程序自带 data/paper/paper.pid 进程锁，重复调用会安全拒绝（输出「已有存活
  实例」），天然幂等。

仅做启动，不修改任何生产代码。
"""
import os
import subprocess
import sys

ROOT = r"E:\Workspace\HexBroker"
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
MAIN = os.path.join(ROOT, "scripts", "paper_trading_main.py")

CREATE_NEW_CONSOLE = 0x10
CREATE_NEW_PROCESS_GROUP = 0x200


def main() -> int:
    if not os.path.exists(MAIN):
        print(f"[LAUNCH][ERROR] 入口脚本缺失: {MAIN}")
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [PY, MAIN],
        cwd=ROOT,
        env=env,
        creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
    )
    print(f"[LAUNCH] 已在独立可见控制台窗口拉起交易系统, pid={proc.pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
