"""模拟盘看门狗（P2-5 崩溃自动重启 supervisor）。

包裹 ``scripts/paper_trading_main.py``：子进程崩溃（非 0 退出）后自动重启，
避免 8-24 那种"崩溃后人工重跑 bat"的断档；同时设**连续崩溃上限**避免无限重启抖动。

设计要点（§8 / 审计报告 P2-5）：
- 仅监控**一个**子进程；子进程退出后（崩溃）才重启，杜绝双实例对敲。
- 子进程自带 pid 锁（``paper_trading_main.py:_try_acquire_pid_lock``）：前次崩溃若残留
  死 pid 文件，新子进程启动时会判定僵尸 pid 并覆盖，故看门狗反复拉起安全。
- 连续崩溃达 ``--max-restarts`` → 停止重启并输出 ``[CRITICAL]`` 告警（不陷入无限循环）。
- 子进程**稳定运行 ≥ --stable-sec 后**才崩溃 → 不计入连续崩溃（视为长生命周期后的偶发，
  重置计数），避免单次瞬时崩溃耗尽上限。
- 子进程 ``returncode==0``（正常退出，如 ``--days N`` 跑满）→ 看门狗停止（不重启）。
- SIGINT/SIGTERM → 转发给子进程并优雅退出。

P2-2（2026-09-06）日志句柄契约：
- 子进程 stdout/stderr 重定向到 ``logs/paper_console.log``；父进程这边的句柄
  **在 ``Popen()`` 返回后立刻关闭**（子进程持有的是副本，不受影响）。
- ⚠️ **已知限制（无法在本层解决）**：子进程存活期间该日志**不能被外部轮转或
  删除**——实测 ``rename`` 直接 ``PermissionError [WinError 32]``。原因是继承
  句柄未带 ``FILE_SHARE_DELETE``，且子进程持有期间任何人都动不了这个文件。
  要支持"运行中轮转"必须改**引擎侧**（自己打开日志并周期性 reopen），看门狗
  这一层做不到。两次重启之间的空档期（无子进程存活）可以自由轮转。

用法::

    python scripts/paper_watchdog.py [--config configs/paper.yaml] [--offline] [--days N] \\
        [--max-restarts 10] [--backoff-base-sec 5] [--backoff-max-sec 300] [--stable-sec 120]
"""

from __future__ import annotations

import argparse
import errno
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 子进程创建标志（P0-A：无可见窗口 + 独立进程组）。⛔ 必须在**模块级**按平台取好，
# 不能写在 Popen 调用点的 kwargs 里：``subprocess.CREATE_*`` 是 Windows-only 常量，
# POSIX 上 AttributeError——写在调用点会让每次 spawn 都抛出、又被下方 ``except``
# 吞成「子进程启动失败」（CI ubuntu runner 实证：真实 spawn 集成用例 5 条连崩、
# counter.txt 永不生成）。POSIX 上传 0（creationflags 参数被忽略，行为正确）。
if sys.platform.startswith("win"):
    _CHILD_CREATIONFLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    # 2026-09-07 新增「脱离」语义（与 launch_trading_window.py 同一套口径）：
    # - DETACHED_PROCESS：子进程不继承看门狗的控制台；
    # - CREATE_BREAKAWAY_FROM_JOB：子进程不继承看门狗所在的 Job Object。
    _CHILD_DETACHED_PROCESS = subprocess.DETACHED_PROCESS
    _CHILD_BREAKAWAY_FROM_JOB = subprocess.CREATE_BREAKAWAY_FROM_JOB
else:  # POSIX：无「窗口/Job」概念，独立进程组语义由 POSIX 进程模型天然满足
    _CHILD_CREATIONFLAGS = 0
    _CHILD_DETACHED_PROCESS = 0
    _CHILD_BREAKAWAY_FROM_JOB = 0


def _child_creationflags_tiers() -> list[int]:
    """子进程创建标志**降级链**（最强 → 最弱），与 launcher 口径一致。

    ⛔ 为什么不能只用一个「最强」值：``CREATE_BREAKAWAY_FROM_JOB`` 在所在 Job
    未授予 breakaway 权限时会让 ``CreateProcess`` **直接失败**（WinError 5），
    也就是「加护栏」本身会把引擎变成**拉不起来**。看门狗若在这里失败，会被
    下面 ``run()`` 的 ``except`` 当成一次崩溃计入 ``crash_count``，退避重试后
    连刷 10 次假 ``[CRITICAL]``——正是 P1-1 花了一整轮才消灭的告警污染形态。
    故先在本函数内降级重试，把「标志被拒」挡在看门狗的崩溃计数之外。

    POSIX：``[0]``（CPython 在 POSIX 上对非 0 creationflags 直接 ValueError）。
    """
    if not sys.platform.startswith("win"):
        return [0]
    full = (
        _CHILD_CREATIONFLAGS
        | _CHILD_DETACHED_PROCESS
        | _CHILD_BREAKAWAY_FROM_JOB
    )
    return [full, full & ~_CHILD_BREAKAWAY_FROM_JOB, _CHILD_CREATIONFLAGS]


def _spawn_child(
    child_cmd: list[str], log_fh: "io.TextIOWrapper", log_fn=print
) -> "tuple[subprocess.Popen, int]":
    """按降级链拉起子进程，返回 ``(proc, flags_used)``。

    ⛔ R22 fail-open：某一级被 OS 拒绝（典型 WinError 5）→ 打 WARN 并降级。
    ⛔ 全链失败 → **原样抛出**最后一个异常，由 ``run()`` 的既有崩溃分支处理
    （计入崩溃 + 退避重试）；**绝不**在这里吞掉后返回一个假 proc——那会让
    看门狗以为子进程在跑，实际没有任何引擎（静默全停）。

    ``log_fh`` 由调用方打开并负责关闭（P2-2 契约）；本函数**只借用**，不 close，
    否则降级重试时句柄已关、后续各级必然失败。
    """
    tiers = _child_creationflags_tiers()
    last_exc: BaseException | None = None
    for idx, flags in enumerate(tiers):
        try:
            proc = subprocess.Popen(
                child_cmd,
                creationflags=flags,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            return proc, flags
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise  # 解释器/脚本缺失不是标志问题，重试只会淹没真因
            last_exc = exc
            log_fn(
                f"[WATCHDOG][WARN] 第 {idx + 1}/{len(tiers)} 组创建标志 "
                f"(0x{flags:08X}) 拉起失败：{type(exc).__name__}: {exc} —— 降级重试"
            )
        except Exception:  # noqa: BLE001
            raise
    if last_exc is None:
        raise RuntimeError("_child_creationflags_tiers() 返回空链，无法拉起子进程")
    raise last_exc


def _open_child_log():
    """交易引擎（子进程）无可见窗口，stdout/stderr 重定向到日志文件。

    ⛔ 契约（P2-2，2026-09-06）：本函数返回的句柄**必须由调用方显式关闭**，
    且必须在 ``subprocess.Popen()`` 返回后**立刻**关——子进程拿到的是句柄
    **副本**（句柄继承是复制语义），父进程关自己的副本不影响子进程写盘。

    不要指望 CPython 的 refcount 顺手关掉它。``Popen`` 对非 PIPE 的 stdout
    文件对象**不做保留**（``Popen.stdout`` 恒为 None，属文档规定行为），故
    当前写法下这个匿名句柄会在 ``Popen()`` 返回后 refcount 归零被立刻回收，
    实测常规路径确实不漏（20 次真实重启，结束后父进程对该日志的句柄数 0）。
    但那是**侥幸正确**而非**契约正确**：只要有人给它多加一个引用（提出来做
    变量、``self._log_fh = fh``、异常 traceback 滞留），立刻变成随重启次数
    线性增长的泄漏（实测 6 次重启 → 6 个活句柄、总句柄 +6）。显式 close 才能
    把"释放"与"谁还持有引用"解耦。
    """
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return open(log_dir / "paper_console.log", "a", encoding="utf-8", buffering=1)


class Watchdog:
    """子进程看门狗：崩溃后指数退避重启，设连续崩溃上限。"""

    def __init__(
        self,
        child_cmd: list[str],
        *,
        max_restarts: int = 10,
        backoff_base: float = 5.0,
        backoff_max: float = 300.0,
        stable_sec: float = 120.0,
        sleep_fn=time.sleep,
        log_fn=print,
    ) -> None:
        self._child_cmd = list(child_cmd)
        self._max_restarts = int(max_restarts)
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)
        self._stable_sec = float(stable_sec)
        self._sleep = sleep_fn
        self._log = log_fn
        self._stop = False
        self._restarts = 0

    # ------------------------------------------------------------------
    # 重启决策（可单测，不依赖子进程）
    # ------------------------------------------------------------------
    def decide_restart(self, returncode: int, runtime: float, crash_count: int) -> tuple[bool, int, float]:
        """返回 ``(是否重启, 新连续崩溃计数, 退避秒数)``。

        - 正常退出（rc==0）→ 不重启、计数归零。
        - 崩溃（rc!=0）：
          - 若稳定运行 ≥ stable_sec → 视为长生命周期后偶发，计数**重置**为 0；
          - 否则连续崩溃 +1；
          - 达到上限 → 不重启（告警停止）；
          - 否则退避 = min(base × 2^(count-1), max)。
        """
        if returncode == 0:
            return (False, 0, 0.0)
        new_count = 0 if runtime >= self._stable_sec else crash_count + 1
        if new_count >= self._max_restarts:
            return (False, new_count, 0.0)
        backoff = min(self._backoff_base * (2 ** max(new_count - 1, 0)), self._backoff_max)
        return (True, new_count, backoff)

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame) -> None:  # noqa: ANN001
            self._log(f"[WATCHDOG] 收到信号 {signum}，准备停止…")
            self._stop = True

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, OSError):
            # 非主线程等环境：忽略信号安装失败
            pass

    def _sleep_backoff(self, sec: float) -> None:
        """分段退避睡眠，期间可响应停止信号。"""
        remaining = float(sec)
        while remaining > 0 and not self._stop:
            step = min(0.5, remaining)
            self._sleep(step)
            remaining -= step

    def run(self) -> int:
        """运行看门狗循环，返回子进程最终退出码（0=正常 / 非0=达上限放弃）。"""
        self._install_signal_handlers()
        crash_count = 0
        self._log(f"[WATCHDOG] 启动：子命令 {' '.join(self._child_cmd)}")
        self._log(
            f"[WATCHDOG] 策略：max_restarts={self._max_restarts} "
            f"backoff={self._backoff_base}→{self._backoff_max}s stable_sec={self._stable_sec}s"
        )
        while not self._stop:
            log_fh = None
            try:
                log_fh = _open_child_log()
                # P0-A（2026-09-04）：子进程（交易引擎）必须「无可见窗口 + 独立进程组」，
                # 否则 (a) 可见控制台窗口被误关 = 引擎被杀（CTRL_CLOSE_EVENT，非 SIGTERM，
                # 日志无 _shutdown）；(b) 与看门狗共用控制台时关窗两者同死。
                # CREATE_NO_WINDOW 消除窗口故障面；CREATE_NEW_PROCESS_GROUP 使引擎脱离
                # 看门狗所在控制台组，看门狗窗口被关亦不影响引擎存活。
                # ⛔ 常量经模块级 _CHILD_CREATIONFLAGS 平台分支取得（勿在此处直接
                # 引用 subprocess.CREATE_*，POSIX 上 AttributeError，见模块头注释）。
                # 2026-09-07：改为走 _spawn_child() 的**降级链**（最强一组带
                # DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB；被 OS 拒绝时
                # 自动降级，绝不让护栏变成拉不起来 —— R22）。
                proc = _spawn_child(self._child_cmd, log_fh, self._log)[0]
            except Exception as exc:  # noqa: BLE001
                # 子命令无法启动（解释器/脚本缺失）→ 视为一次崩溃
                self._log(f"[WATCHDOG][ERROR] 子进程启动失败：{exc}")
                crash_count += 1
                if crash_count >= self._max_restarts:
                    self._log(
                        f"[WATCHDOG][CRITICAL] 子进程连续启动失败 {crash_count} 次，停止重启。"
                    )
                    return 1
                self._sleep_backoff(self._backoff_base)
                continue
            finally:
                # P2-2（2026-09-06）：父进程这边的句柄**在 Popen 返回后即可关闭**。
                # 子进程持有的是句柄副本，父进程关闭自己的副本不影响子进程写盘
                # （实测：关闭后子进程 50 行输出 100% 完整落盘）。
                # 用 finally 而非"子进程退出后再关"：后者在 return / continue /
                # 异常三条路径上都要各写一遍，漏一条就是泄漏；finally 一次性兜住。
                if log_fh is not None:
                    try:
                        log_fh.close()
                    except Exception:  # noqa: BLE001
                        # 关闭失败绝不影响看门狗主流程（R22：不引入新停摆模式）
                        pass

            start = time.monotonic()
            while proc.poll() is None:
                if self._stop:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:  # noqa: BLE001
                        proc.kill()
                    self._log("[WATCHDOG] 已终止子进程，看门狗退出。")
                    return 0 if proc.returncode == 0 else proc.returncode
                self._sleep(0.5)

            rc = int(proc.returncode)
            runtime = time.monotonic() - start
            should, crash_count, backoff = self.decide_restart(rc, runtime, crash_count)
            if not should:
                if rc == 0:
                    self._log("[WATCHDOG] 子进程正常退出（code 0），看门狗停止。")
                else:
                    self._log(
                        f"[WATCHDOG][CRITICAL] 子进程连续崩溃 {crash_count} 次达到上限 "
                        f"{self._max_restarts}，停止重启并告警（请排查根因）。"
                    )
                return rc
            self._restarts += 1
            self._log(
                f"[WATCHDOG] 子进程异常退出（code {rc}，运行 {runtime:.0f}s），"
                f"{backoff:.0f}s 后第 {self._restarts} 次重启…"
            )
            self._sleep_backoff(backoff)
        # 收到停止信号且子进程已不在运行
        self._log("[WATCHDOG] 看门狗已停止。")
        return 0


def _build_child_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, str(ROOT / "scripts" / "paper_trading_main.py")]
    if args.config:
        cmd += ["--config", args.config]
    if args.offline:
        cmd.append("--offline")
    if args.days is not None:
        cmd += ["--days", str(args.days)]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘看门狗（崩溃自动重启 supervisor）")
    parser.add_argument("--config", default="configs/paper.yaml", help="传给子进程的 paper.yaml 路径")
    parser.add_argument("--offline", action="store_true", help="离线 mock 行情模式（透传子进程）")
    parser.add_argument("--days", type=int, default=None, help="运行 N 个交易日后退出（透传子进程）")
    parser.add_argument("--max-restarts", type=int, default=10, help="连续崩溃上限，达此数停止重启并告警")
    parser.add_argument("--backoff-base-sec", type=float, default=5.0, help="退避基数（秒），每次 ×2")
    parser.add_argument("--backoff-max-sec", type=float, default=300.0, help="退避上限（秒）")
    parser.add_argument("--stable-sec", type=float, default=120.0, help="子进程稳定运行 ≥ 此秒数后崩溃不计连续崩溃")
    args = parser.parse_args()

    child_cmd = _build_child_cmd(args)
    wd = Watchdog(
        child_cmd,
        max_restarts=args.max_restarts,
        backoff_base=args.backoff_base_sec,
        backoff_max=args.backoff_max_sec,
        stable_sec=args.stable_sec,
    )
    return wd.run()


if __name__ == "__main__":
    sys.exit(main())
