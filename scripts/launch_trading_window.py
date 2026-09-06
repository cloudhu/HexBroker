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

----
``--watchdog`` 崩溃自愈（P0-A 补缺口，2026-09-06）
------------------------------------------------
默认（不带开关）本启动器直接拉起 ``paper_trading_main.py``：引擎一旦崩溃（非 0 退出）
无人重启，只能等下一个时段（08:55/13:25/20:55）的自动化来拉，中间整段行情断档 ——
09-04 已两次因进程死亡导致收盘复盘缺失。

带 ``--watchdog`` 时改为拉起 ``scripts/paper_watchdog.py``（supervisor），由它再拉起
引擎，崩溃后按指数退避自动重启，连续崩溃达上限才停止并告警；子进程稳定运行
≥ ``--stable-sec`` 后崩溃会重置连续计数。引擎 rc==0 正常退出则不重启。

⛔ 为什么不用现成的 ``start_paper_trading_watchdog.bat``：该 BAT 需要 cmd.exe，
而自动化运行环境的沙箱**拦截 cmd.exe 启动**（自动化自身 prompt 已写明「未调用
cmd.exe（沙箱会拦截 cmd.exe 启动）」）。故必须在 Python 启动器内加开关走纯 Python 路径。

⛔ 向后兼容：不传 ``--watchdog`` 时行为**与修改前完全一致**（直接 spawn MAIN），
默认路径零变更，防回归。

⛔ 未知参数（``parse_known_args`` 的 ``rest``）**不报错、予以透传**；但看门狗自身
用严格 ``parse_args()``，把未知参数喂给它会导致看门狗启动即崩（等于没装自愈），
故未知参数只透传给「直连引擎」路径，并在日志里显式列出被丢弃的项，不做静默丢失。

仅做启动，不修改任何生产代码。
"""
import argparse
import io
import os
import subprocess
import sys

ROOT = r"E:\Workspace\HexBroker"
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
MAIN = os.path.join(ROOT, "scripts", "paper_trading_main.py")
# P0-A：崩溃自愈 supervisor（不传 --watchdog 时不涉及，默认路径零变更）
WATCHDOG = os.path.join(ROOT, "scripts", "paper_watchdog.py")

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


def _build_parser() -> "argparse.ArgumentParser":
    """命令行解析器：未知参数**不报错**（``parse_known_args`` 交给调用方透传）。"""
    parser = argparse.ArgumentParser(
        prog="launch_trading_window.py",
        description="后台拉起 HexBroker 模拟盘（无可见窗口）；--watchdog 下崩溃自愈。",
    )
    parser.add_argument(
        "--watchdog", action="store_true",
        help="经 scripts/paper_watchdog.py 拉起，崩溃后指数退避自动重启",
    )
    # 以下均为「显式传入才透传」（默认 None → 不写入命令行，保持子进程自身默认值）
    parser.add_argument("--config", default=None, help="paper.yaml 路径（透传）")
    parser.add_argument("--offline", action="store_true", help="离线 mock 行情（透传）")
    parser.add_argument("--days", type=int, default=None, help="运行 N 个交易日后退出（透传）")
    parser.add_argument("--max-restarts", type=int, default=None, help="看门狗连续崩溃上限")
    parser.add_argument("--backoff-base-sec", type=float, default=None, help="看门狗退避基数（秒）")
    parser.add_argument("--backoff-max-sec", type=float, default=None, help="看门狗退避上限（秒）")
    parser.add_argument("--stable-sec", type=float, default=None, help="稳定运行 ≥ 此秒数重置连续崩溃")
    return parser


def _passthrough_args(args: "argparse.Namespace") -> list[str]:
    """按「显式传入才附加」原则拼出透传参数（None → 不附加，避免覆盖子进程默认值）。"""
    out: list[str] = []
    if args.config:
        out += ["--config", args.config]
    if args.offline:
        out.append("--offline")
    if args.days is not None:
        out += ["--days", str(args.days)]
    if args.max_restarts is not None:
        out += ["--max-restarts", str(args.max_restarts)]
    if args.backoff_base_sec is not None:
        out += ["--backoff-base-sec", str(args.backoff_base_sec)]
    if args.backoff_max_sec is not None:
        out += ["--backoff-max-sec", str(args.backoff_max_sec)]
    if args.stable_sec is not None:
        out += ["--stable-sec", str(args.stable_sec)]
    return out


def main(argv: "list[str] | None" = None) -> int:
    """后台拉起模拟盘。

    ``argv=None`` → 取 ``sys.argv[1:]``（保持 ``main()`` 可无参调用的既有约定）。
    不带 ``--watchdog`` 时行为与 P0-A 前**完全一致**：直接 spawn ``MAIN``。
    """
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    args, rest = parser.parse_known_args(argv)

    entry = WATCHDOG if args.watchdog else MAIN
    if not os.path.exists(entry):
        print(f"[LAUNCH][ERROR] 入口脚本缺失: {entry}")
        return 1

    # 透传：显式传入的已知项（看门狗与引擎都认）
    passthrough = _passthrough_args(args)
    cmd = [PY, entry] + passthrough

    if args.watchdog:
        # 看门狗用严格 parse_args()：喂未知参数会让它启动即崩（等于没装自愈）。
        # 故未知参数只给直连引擎路径，并显式告警，不做静默丢失。
        if rest:
            print(
                f"[LAUNCH][WARN] 以下参数无法透传给看门狗（其解析器严格），已忽略: {' '.join(rest)}"
            )
    else:
        cmd += rest

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONUNBUFFERED"] = "1"

    logf = _open_console_log()
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        creationflags=_engine_creationflags(),
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    kind = "看门狗（崩溃自愈）" if args.watchdog else "交易系统"
    print(
        f"[LAUNCH] 已在后台（无窗口）拉起{kind}, pid={proc.pid} "
        f"—— 控制台输出见 {CONSOLE_LOG}"
    )
    if args.watchdog:
        print(f"[LAUNCH] 看门狗命令行: {' '.join(cmd)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
