#!/usr/bin/env python
"""计划任务（无人值守）入口：前台运行模拟盘看门狗。

⛔ 为什么需要单独一个入口（而不是直接调度 ``scripts/paper_watchdog.py``）：
Windows 计划任务的**默认启动目录是 ``C:\\Windows\\System32``**，且不继承用户
shell 的环境变量。直接调度看门狗会有三处静默失败：
- ``--config configs/paper.yaml`` 是**相对路径** → 在 system32 下找不到配置；
- ``import hexbroker`` 依赖 ``PYTHONPATH=仓库根`` → 在 system32 下 ImportError；
- stdout/stderr 没有 shell 帮我们重定向 → 看门狗的 ``[WATCHDOG]`` 日志直接丢失，
  出问题时**无任何排障线索**。

本入口在进程内一次性把这三件事做掉（``os.chdir`` + 补 ``PYTHONPATH`` +
``PYTHONUNBUFFERED`` + 自己把 stdout/stderr 接到日志文件），因此**不依赖
任务的「起始位置」字段**（``schtasks.exe`` 根本没有该选项），调度时只需要
给出解释器与本脚本路径即可。

与人工路径 ``start_paper_trading_watchdog.bat`` 的区别（二者并存、互不替代）：
- 无 ``chcp``、无前置 ``--health-check``、无结尾 ``pause`` —— 无人值守必须
  **零交互**（``pause`` 会让计划任务永远挂在那里等一个不会到来的按键）；
- 人工 BAT 保留不变，仍供双击使用。

⛔ 单实例预检（13:25 / 20:55 的假 CRITICAL 防线）：
引擎调度器没有「时段结束即退出」，会活满全天。若 08:55 拉起的实例还活着，
13:25 新拉起的看门狗去 spawn 引擎 → 引擎撞单实例锁 ``rc=1`` → 看门狗把
``rc!=0 且 runtime < stable_sec`` 判成崩溃 → 指数退避重启 → 约 20 分钟后刷一条
**假** ``[CRITICAL] 连续崩溃 10 次``。这正是 P1-1（2026-09-06）花一整轮消灭掉的
告警污染形态。故拉起前先用**与引擎完全相同的 OS 字节锁**探测：已有存活实例
则直接退 ``0``（不是错误）。
预检实现**不另写一份锁常量**，而是运行时复用 ``launch_trading_window`` 的同源
探测函数 —— 两边各写一份、将来漂移，是最难查的一类失效。

⛔ R22 fail-open：预检失败（返回 None）→ 照常拉起，交由引擎自身锁仲裁；
绝不让「多了一道预检」变成「系统永远起不来」。

用法（计划任务 / 命令行均可，无参数）::

    C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe \\
        E:\\Workspace\\HexBroker\\scripts\\paper_watchdog_schtask.py
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 看门狗自身 stdout/stderr 落点（引擎输出另走 logs/paper_console.log，由看门狗
# 内部 _open_child_log() 负责，与本文件无关）
LOG_PATH = ROOT / "logs" / "watchdog_schtask.log"
CHILD = ROOT / "scripts" / "paper_trading_main.py"


def _load_script_module(mod_name: str, filename: str):
    """按文件路径加载 ``scripts/`` 下的同级非包脚本。

    与测试里的加载方式一致（``scripts/`` 不是包，不能常规 import）。
    加载失败 → 抛出，由调用方按 fail-open / 明确报错处理。
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(ROOT / "scripts" / filename))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {filename}（spec 为空）")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_watchdog():
    """加载看门狗模块（单独成函数，便于测试注入桩）。"""
    return _load_script_module("_hb_watchdog_for_schtask", "paper_watchdog.py")


def _engine_running() -> "bool | None":
    """复用 launcher 的**同源**单实例探测：True=有存活实例 / False=无 / None=探测失败。

    ⛔ 不在这里另写一份锁偏移或换一种锁定方式（msvcrt/flock），否则两边漂移后
    预检会**静默失效** —— 最坏是把空闲误判成占用，系统永远起不来。
    取不到 launcher 模块时返回 None（调用方 fail-open）。
    """
    try:
        ltw = _load_script_module("_hb_launcher_for_schtask", "launch_trading_window.py")
        return ltw._engine_instance_running(ltw.PID_FILE)
    except Exception:  # noqa: BLE001
        return None


def _bootstrap_runtime() -> None:
    """把「计划任务缺省的运行环境」补齐：cwd / PYTHONPATH / PYTHONUNBUFFERED。

    ⛔ 必须**先于**任何仓库内模块加载与任何相对路径解析执行。
    """
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        str(ROOT) if not existing else f"{ROOT}{os.pathsep}{existing}"
    )
    os.environ["PYTHONUNBUFFERED"] = "1"


def _redirect_stdout_stderr(path: "str | os.PathLike[str] | None" = None):
    """把本进程 stdout/stderr 追加接到日志文件（行缓冲，崩溃也不丢）。

    ⛔ 不能把落点写成默认参数 ``path=LOG_PATH``：默认值在**函数定义时**绑定，
    测试里 monkeypatch ``LOG_PATH`` 就失效了（会写脏仓库 logs/）。故用
    ``None`` 哨兵，在**调用时**再解析模块级 ``LOG_PATH``。

    返回打开的句柄，调用方可自行关闭。
    """
    target = Path(LOG_PATH) if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fh = open(target, "a", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    return fh


def main() -> int:
    """入口：补环境 → 接日志 → 单实例预检 → 前台运行看门狗。"""
    _bootstrap_runtime()
    _redirect_stdout_stderr()

    print("=" * 78)
    print(f"[SCHTASK] 计划任务入口启动 cwd={os.getcwd()} py={sys.executable}")
    print(f"[SCHTASK] 本进程日志 → {LOG_PATH}")

    running = _engine_running()
    if running is True:
        print(
            "[SCHTASK] 已有存活交易引擎实例（单实例锁被占用）——跳过本次拉起，"
            "不重复挂看门狗（避免撞锁 rc=1 被误判崩溃 → 假 CRITICAL 告警）"
        )
        return 0
    if running is None:
        # R22：探测失败绝不能变成「永远拉不起」
        print("[SCHTASK][WARN] 单实例预检失败（按无存活处理，交由引擎自身锁仲裁）")

    wd = _load_watchdog()
    child_cmd = wd._build_child_cmd(
        argparse.Namespace(config="configs/paper.yaml", offline=False, days=None)
    )
    print(f"[SCHTASK] 预检通过（无存活实例），前台运行看门狗：{' '.join(child_cmd)}")
    # log_fn=print：print 在**调用时**查 sys.stdout，故重定向后自动落盘
    return wd.Watchdog(child_cmd, log_fn=print).run()


if __name__ == "__main__":
    sys.exit(main())
