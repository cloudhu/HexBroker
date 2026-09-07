"""P2-5 看门狗单测（崩溃自动重启 supervisor）。

覆盖：
① 正常退出（rc==0）→ 不重启、计数归零；
② 崩溃计数递增 + 指数退避（base×2^(n-1) 封顶 max）；
③ 达连续崩溃上限 → 不重启（告警停止）；
④ 稳定运行 ≥ stable_sec 后崩溃 → 计数重置（不耗尽上限）；
⑤ 集成：子进程先崩溃 2 次再正常退出 → 看门狗重启 2 次后停止；
⑥ 集成：子进程始终崩溃（≤ stable）→ 达 max_restarts 后停止并 CRITICAL 告警；
⑦ P0-A：子进程必须「无窗口 + 独立进程组 + 日志重定向」；
⑧ P2-2（2026-09-06）：子进程日志句柄**契约上**不泄漏——Popen 返回即显式关闭，
   多次重启后输出完整，且每次 spawn 重新打开（容忍启动间隔的外部轮转）。

注：集成测试用受控假子进程（临时 .py 脚本 + 计数器文件），退避设极小避免耗时。
P2-2 相关用例为**真实 spawn**（不开 mock Popen），并把 wd_mod.ROOT 重定向到
tmp，绝不写仓库 logs/。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# paper_watchdog.py 位于 scripts/（非包），按文件路径加载模块
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_watchdog.py"
_SPEC = importlib.util.spec_from_file_location("paper_watchdog", str(_SCRIPT))
wd_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wd_mod)

_CHILD_TMPL = (
    "import sys, pathlib\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "c = int(p.read_text()) if p.exists() else 0\n"
    "p.write_text(str(c + 1))\n"
    "sys.exit(0 if c >= {ok} else 1)\n"
)


def _fake_child(tmp_path: Path, ok_after: int) -> list[str]:
    """生成一个假子进程脚本：前 ok_after 次退出 1，之后退出 0。"""
    script = tmp_path / "fake_child.py"
    script.write_text(_CHILD_TMPL.format(ok=ok_after))
    return [sys.executable, str(script), str(tmp_path / "counter.txt")]


def _collecting_log():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------------------
# ① 正常退出
# ---------------------------------------------------------------------------
def test_decide_restart_clean_exit():
    w = wd_mod.Watchdog(["x"], max_restarts=3, backoff_base=5.0, backoff_max=300.0, stable_sec=120.0)
    should, count, backoff = w.decide_restart(0, 10.0, 2)
    assert should is False and count == 0 and backoff == 0.0


# ---------------------------------------------------------------------------
# ② 崩溃计数 + 指数退避
# ---------------------------------------------------------------------------
def test_decide_restart_backoff_escalates():
    w = wd_mod.Watchdog(["x"], max_restarts=10, backoff_base=5.0, backoff_max=300.0, stable_sec=120.0)
    should, c1, b1 = w.decide_restart(1, 1.0, 0)
    assert should and c1 == 1 and b1 == 5.0
    should, c2, b2 = w.decide_restart(1, 1.0, c1)
    assert should and c2 == 2 and b2 == 10.0
    should, c3, b3 = w.decide_restart(1, 1.0, c2)
    assert should and c3 == 3 and b3 == 20.0
    # 第 5 次崩溃退避 = base×2^4 = 80（未达上限 300）
    should, c5, b5 = w.decide_restart(1, 1.0, 4)
    assert should and c5 == 5 and b5 == 80.0
    # 封顶：第 7 次崩溃 base×2^6 = 320 → 封顶 300
    should, c7, b7 = w.decide_restart(1, 1.0, 6)
    assert b7 == 300.0


# ---------------------------------------------------------------------------
# ③ 达连续崩溃上限 → 不重启
# ---------------------------------------------------------------------------
def test_decide_restart_max_reached():
    w = wd_mod.Watchdog(["x"], max_restarts=3, backoff_base=5.0, backoff_max=300.0, stable_sec=120.0)
    should, count, backoff = w.decide_restart(1, 1.0, 2)  # 第 3 次崩溃
    assert should is False and count == 3 and backoff == 0.0


# ---------------------------------------------------------------------------
# ④ 稳定运行后崩溃 → 计数重置
# ---------------------------------------------------------------------------
def test_decide_restart_stable_run_resets():
    w = wd_mod.Watchdog(["x"], max_restarts=3, backoff_base=5.0, backoff_max=300.0, stable_sec=120.0)
    # 已连崩 5 次，但本次运行 200s（≥ stable）→ 重置为 0，仍重启
    should, count, backoff = w.decide_restart(1, 200.0, 5)
    assert should is True and count == 0 and backoff == 5.0


# ---------------------------------------------------------------------------
# ⑤ 集成：崩溃 2 次后正常退出 → 重启 2 次后停止
# ---------------------------------------------------------------------------
def test_watchdog_restarts_then_clean_exit(tmp_path):
    child_cmd = _fake_child(tmp_path, ok_after=2)
    logs, _ = _collecting_log()
    w = wd_mod.Watchdog(
        child_cmd, max_restarts=5, backoff_base=0.01, backoff_max=0.01, stable_sec=0.0,
        sleep_fn=time.sleep, log_fn=logs.append,
    )
    rc = w.run()
    assert rc == 0  # 最终子进程正常退出
    counter = int((tmp_path / "counter.txt").read_text())
    assert counter == 3  # 共拉起 3 次（2 崩溃 + 1 正常）
    assert any("第 1 次重启" in m for m in logs)
    assert any("第 2 次重启" in m for m in logs)
    assert any("正常退出" in m for m in logs)


# ---------------------------------------------------------------------------
# ⑥ 集成：始终崩溃（≤ stable）→ 达上限后停止并告警
# ---------------------------------------------------------------------------
def test_watchdog_stops_on_max_restarts(tmp_path):
    # ok_after 极大 → 子进程永远退出 1
    child_cmd = _fake_child(tmp_path, ok_after=999)
    logs, _ = _collecting_log()
    w = wd_mod.Watchdog(
        child_cmd, max_restarts=3, backoff_base=0.01, backoff_max=0.01, stable_sec=100000.0,
        sleep_fn=time.sleep, log_fn=logs.append,
    )
    rc = w.run()
    assert rc == 1  # 达上限放弃
    counter = int((tmp_path / "counter.txt").read_text())
    assert counter == 3  # 仅拉起 max_restarts 次
    assert any("[CRITICAL]" in m for m in logs)
    assert any("达到上限" in m for m in logs)


# ---------------------------------------------------------------------------
# ⑦ P0-A：看门狗拉起的子进程必须是「无窗口 + 独立进程组」
# ---------------------------------------------------------------------------
# ⛔ CI（ubuntu runner）2026-09-06：断言里的 subprocess.CREATE_* 是 Windows-only
#    常量，POSIX 上 AttributeError → 本用例只能声明为 Windows 专属契约。
#    跨平台 spawn 正确性由下方真实 spawn 集成用例（⑤⑥ + P2-2 三条）覆盖——
#    那些用例在修掉生产代码的平台硬编码后（_CHILD_CREATIONFLAGS 模块级分支）
#    于 POSIX 上同样真实可跑。
@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="断言 subprocess.CREATE_*（Windows-only 常量）的无窗口语义；POSIX 无此概念",
)
def test_watchdog_child_uses_windowless_flags():
    captured = []

    class _FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            captured.append(kw)

        def poll(self):
            return 0

    orig = wd_mod.subprocess.Popen
    wd_mod.subprocess.Popen = _FakeProc
    try:
        w = wd_mod.Watchdog(
            ["x"], max_restarts=1, backoff_base=0.01, backoff_max=0.01,
            stable_sec=1.0, sleep_fn=lambda s: None, log_fn=lambda m: None,
        )
        w.run()
    finally:
        wd_mod.subprocess.Popen = orig

    assert captured, "child Popen 未被调用"
    kw = captured[0]
    assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    # P2-1 同款硬化（QA 2026-09-06 用变异测试打穿过）：`is not None` 挡不住
    # stdout=DEVNULL(-3)/PIPE(-1)，两者都不是 None → 引擎控制台输出会静默消失，
    # 正是 P0-1「冻结行情在日志里不可见」的同形故障。
    assert kw["stdout"] not in (None, subprocess.DEVNULL, subprocess.PIPE), (
        "stdout 必须重定向到真实日志文件，不得为 None/DEVNULL/PIPE"
    )
    assert str(getattr(kw["stdout"], "name", "")).endswith("paper_console.log")
    assert kw["stderr"] == subprocess.STDOUT


# ---------------------------------------------------------------------------
# P2-2（2026-09-06）：子进程日志句柄——**契约上**不泄漏
# ---------------------------------------------------------------------------
# 先说清楚这条修的是什么、不是什么（实测取证见 artifacts/_tmp/_R3_*.txt）：
#
# - 原写法 ``stdout=_open_child_log()`` 从不 close，看起来每次重启漏一个句柄。
#   **实测：常规路径下并不漏**——``subprocess.Popen`` 对非 PIPE 的 stdout 文件
#   对象**不做保留**（``Popen.stdout`` 恒为 None，属文档规定行为），那个匿名
#   句柄会在 ``Popen()`` 返回后 refcount 归零被立刻回收。20 次真实重启后父进程
#   对该日志的 OS 句柄数为 0，日志内容 1000/1000 行完整。
# - 但那是**侥幸正确**而非**契约正确**：释放完全依赖 CPython refcount 时机。
#   只要有人给这个句柄多加一个引用（最典型：提出来做变量、``self._log_fh = fh``、
#   异常 traceback 滞留），立刻变成随重启次数线性增长的泄漏——实测 6 次重启
#   → 6 个活着的文件对象、总句柄 +6。
#
# 故修法是「Popen 返回即显式 close，用 finally 兜住」，让"释放"与"谁还持有引用"
# 解耦。下面的判别式用例**故意保留句柄引用**来逼出这个差异：旧代码在保留场景
# 下必然红，新代码必须为绿。
_MARKER_CHILD_TMPL = (
    "import sys, pathlib\n"
    "counter = pathlib.Path(sys.argv[1])\n"
    "n = int(counter.read_text()) if counter.exists() else 0\n"
    "counter.write_text(str(n + 1))\n"
    "for i in range({lines}):\n"
    "    print('%s|%05d' % (n, i), flush=True)\n"
    "sys.exit(1)\n"
)


def _marker_child(tmp_path: Path, lines: int = 50) -> list[str]:
    """子进程：每次运行打印 ``<run序号>|<行号>`` 共 lines 行后退出 1（崩溃）。"""
    script = tmp_path / "marker_child.py"
    script.write_text(_MARKER_CHILD_TMPL.format(lines=lines))
    return [sys.executable, str(script), str(tmp_path / "counter.txt")]


def _spawn_watchdog(wd_mod, tmp_path, n_runs, lines=50, on_sleep=None):
    """真实 spawn 跑 n_runs 次，返回 (日志路径, 计数器值, 保留下来的句柄列表)。"""
    wd_mod.ROOT = tmp_path  # ⛔ 绝不写仓库 logs/，全部重定向到临时目录
    log_path = tmp_path / "logs" / "paper_console.log"

    held: list = []
    real_open = wd_mod._open_child_log

    def _spy():
        fh = real_open()
        held.append(fh)  # 故意保留引用：逼出"释放依赖 refcount"的侥幸正确
        return fh

    wd_mod._open_child_log = _spy
    try:
        default_sleep = on_sleep if on_sleep is not None else (lambda s: time.sleep(0))
        wd = wd_mod.Watchdog(
            _marker_child(tmp_path, lines=lines),
            max_restarts=n_runs,
            backoff_base=0.01,
            backoff_max=0.01,
            stable_sec=1e9,  # 每次都算「崩溃」→ 连续计数递增，跑满 n_runs 次
            sleep_fn=default_sleep,
            log_fn=lambda m: None,
        )
        wd.run()
    finally:
        wd_mod._open_child_log = real_open

    counter = int((tmp_path / "counter.txt").read_text())
    return log_path, counter, held


def test_parent_closes_child_log_handle_each_spawn(tmp_path):
    """P2-2 判别式：即使**故意保留句柄引用**，父进程也必须在每次 spawn 后关掉它。

    旧代码在保留引用的场景下会留下 N 个未关闭句柄（实测 6 次重启 → 6 个活句柄、
    总句柄 +6）；新代码因为 Popen 返回即 close，保留的只是已关闭对象。

    旁证（若环境有 psutil）：进程级 OS 句柄层面同样无滞留。
    """
    log_path, runs, held = _spawn_watchdog(wd_mod, tmp_path, n_runs=6, lines=50)

    assert runs == 6, f"应真实 spawn 6 次，实际 {runs}"
    assert len(held) == 6, f"每次 spawn 都应开一个日志句柄，实际 {len(held)}"
    still_open = [fh for fh in held if not fh.closed]
    assert not still_open, (
        f"父进程未关闭 {len(still_open)} 个日志句柄——释放不能依赖 refcount 时机；"
        "只要有人多持一个引用就会变成随重启次数线性增长的泄漏"
    )

    # 旁证：OS 句柄层面同样无滞留（psutil 缺失时跳过这一层，主断言不受影响）
    try:
        import psutil
    except Exception:  # pragma: no cover - 可选依赖
        psutil = None
    if psutil is not None:
        target = str(log_path).lower()
        leaked = [
            f.path for f in psutil.Process().open_files()
            if str(f.path).lower() == target
        ]
        assert not leaked, f"OS 句柄层面仍有 {len(leaked)} 个滞留: {leaked}"


def test_child_log_content_complete_after_restarts(tmp_path):
    """P2-2 ②：多次重启后子进程输出**完整**落盘（真实 spawn，逐行核对）。

    这是「Windows 上多进程复用 append 句柄是否安全」的实测回答：我们不复用
    句柄（每次 spawn 新开、Popen 返回即关），且每次都是 append 模式串行写入。
    """
    n_runs, lines = 5, 60
    log_path, runs, _ = _spawn_watchdog(wd_mod, tmp_path, n_runs=n_runs, lines=lines)

    assert runs == n_runs, f"应真实 spawn {n_runs} 次，实际 {runs}"
    text = log_path.read_text(encoding="utf-8")
    got = [ln for ln in text.splitlines() if ln.strip()]
    expected = {f"{r}|{i:05d}" for r in range(n_runs) for i in range(lines)}

    assert len(got) == n_runs * lines, (
        f"日志行数 {len(got)} != 期望 {n_runs * lines}（有内容丢失）"
    )
    missing, extra = expected - set(got), set(got) - expected
    assert not missing, f"缺失 {len(missing)} 行，样例 {sorted(missing)[:5]}"
    assert not extra, f"多出 {len(extra)} 行，样例 {sorted(extra)[:5]}"


def test_log_reopened_each_spawn_tolerates_external_rotation(tmp_path):
    """P2-2 ③：日志**每次 spawn 重新打开** → 启动间隔的外部轮转/删除被容忍。

    证明两点：
    ① ``_open_child_log`` 被调用次数 == spawn 次数（不是全程共用一个句柄，
       那样一旦文件被换掉就永远写进旧的/已删除的文件里）；
    ② 日志文件被外部整个删掉后，下一次 spawn 能重新创建并继续写。

    ⚠️ 已知限制（不装作没问题）：**子进程存活期间**该日志无法被外部轮转或
    删除——实测 ``rename`` 直接 ``PermissionError [WinError 32]``（继承句柄未带
    ``FILE_SHARE_DELETE``）。这是 Windows 句柄继承的固有限制，看门狗这一层
    解决不了，要支持"运行中轮转"必须改引擎侧（自己打开日志并周期性 reopen）。
    """
    wd_mod.ROOT = tmp_path
    log_path = tmp_path / "logs" / "paper_console.log"

    open_calls: list = []
    real_open = wd_mod._open_child_log

    def _spy():
        fh = real_open()
        open_calls.append(fh)
        return fh

    wd_mod._open_child_log = _spy
    try:
        # 第一轮：spawn 2 次
        _spawn_watchdog(wd_mod, tmp_path, n_runs=2, lines=20)
        assert len(open_calls) == 2, (
            f"_open_child_log 应每 spawn 一次调用一次，实际 {len(open_calls)}"
        )
        first_text = log_path.read_text(encoding="utf-8")
        assert "0|" in first_text and "1|" in first_text
        assert all(fh.closed for fh in open_calls), "spawn 后句柄必须已关闭"

        # 外部把整个日志删掉（模拟轮转/清理）
        log_path.unlink()
        assert not log_path.exists()

        # 第二轮：必须重新创建文件并继续写
        open_calls.clear()
        _spawn_watchdog(wd_mod, tmp_path, n_runs=2, lines=20)
    finally:
        wd_mod._open_child_log = real_open

    assert len(open_calls) == 2, f"第二轮也应每 spawn 开一次，实际 {len(open_calls)}"
    assert log_path.exists(), "日志被外部删除后，下一次 spawn 必须重新创建"
    second_text = log_path.read_text(encoding="utf-8")
    assert "2|" in second_text and "3|" in second_text, "新一轮运行内容必须落盘"
    assert "0|" not in second_text, "被删掉的旧内容不应复活（证明写的是重建后的新文件）"


def test_log_handle_closed_even_when_spawn_fails(tmp_path, monkeypatch):
    """P2-2：Popen 抛异常时，finally 也必须关掉已打开的日志句柄。"""
    wd_mod.ROOT = tmp_path
    held: list = []
    real_open = wd_mod._open_child_log

    def _spy():
        fh = real_open()
        held.append(fh)
        return fh

    def _boom_popen(cmd, **kw):
        raise OSError("模拟子进程启动失败")

    monkeypatch.setattr(wd_mod, "_open_child_log", _spy)
    monkeypatch.setattr(wd_mod.subprocess, "Popen", _boom_popen)

    wd = wd_mod.Watchdog(
        ["x"], max_restarts=1, backoff_base=0.01, backoff_max=0.01,
        stable_sec=1.0, sleep_fn=lambda s: None, log_fn=lambda m: None,
    )
    rc = wd.run()

    assert rc == 1, "启动失败达上限应返回 1"
    assert len(held) == 1, f"应只开过 1 次日志句柄，实际 {len(held)}"
    assert held[0].closed, "Popen 抛异常时也必须关掉日志句柄（finally 兜底）"


# ---------------------------------------------------------------------------
# ⑨ 2026-09-07「进程被外部回收」修复：子进程创建标志降级链 + fail-open
#    （晚间补丁：真值表实验后重排——DETACHED_PROCESS 全链禁用）
# ---------------------------------------------------------------------------
# 与 launch_trading_window.py 同一套口径（两条链数值必须一致，见
# test_child_tiers_match_launcher_tiers）：第 1/2 级为真值表实测的「无窗口
# console」家族（子进程 GetConsoleWindow()==0，收不到 CTRL_CLOSE_EVENT）；
# DETACHED_PROCESS(0x8) 真值表定罪（经 venv 重定向器链路让真引擎重新挂上
# 带窗 console）全链禁用；BREAKAWAY 被 OS 拒绝时降级重试。
#
# ⛔ 为什么看门狗这一层**尤其**需要 fail-open：看门狗把「子进程启动失败」计入
# crash_count 并退避重试，若 BREAKAWAY 在每次重试时都失败，会连刷 10 次假的
# [CRITICAL] 连续崩溃——正是 P1-1 花一整轮才消灭的告警污染形态，而且真因
# （一条 WinError 5）会被淹没在 10 条 CRITICAL 里。
#
# ⛔ 断言引用 subprocess.CREATE_*（Windows-only 常量），必须带 _WIN_ONLY，
#    否则 CI ubuntu runner 会红。
_WD_WIN_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows 专属契约（subprocess.CREATE_* 常量）；POSIX 无此概念",
)


def test_child_creationflags_tiers_platform_shape():
    """跨平台形状：POSIX 恒 [0]；Windows 3 级单调降级，全链无 DETACHED_PROCESS。"""
    tiers = wd_mod._child_creationflags_tiers()
    assert tiers, "降级链不得为空（空链 = 永远拉不起子进程）"
    if sys.platform.startswith("win"):
        assert len(tiers) == 3
        for i in range(len(tiers) - 1):
            assert tiers[i + 1] & ~tiers[i] == 0, "降级链只许做减法"
        assert tiers[0] == (
            subprocess.CREATE_BREAKAWAY_FROM_JOB
            | subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ), f"第 1 级必须 = BREAKAWAY|NO_WINDOW|NPG（真值表 C6），实际 0x{tiers[0]:08X}"
        assert tiers[-1] == subprocess.CREATE_NEW_PROCESS_GROUP, (
            "末级必须退到仅 CREATE_NEW_PROCESS_GROUP（fail-open 兜底，R22）"
        )
        for i, flags in enumerate(tiers):
            assert not (flags & 0x00000008), (
                f"第 {i + 1} 级含 DETACHED_PROCESS(0x8)——真值表已定罪，全链禁用"
            )
    else:
        assert tiers == [0], "POSIX 上 creationflags 非 0 会被 CPython 直接 ValueError"


@_WD_WIN_ONLY
def test_child_tiers_match_launcher_tiers():
    """耦合护栏：看门狗与 launcher 的降级链必须**逐值相同**。

    两边各写一份、将来漂移，是最难查的一类回归——表现是「launcher 说用了
    第 1 级、看门狗却在用第 3 级」，日志上看不出任何异常。故用测试钉死。
    """
    import importlib.util

    launcher = (
        Path(__file__).resolve().parents[1] / "scripts" / "launch_trading_window.py"
    )
    spec = importlib.util.spec_from_file_location("ltw_for_tiers", str(launcher))
    ltw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ltw)

    assert wd_mod._child_creationflags_tiers() == ltw._engine_creationflags_tiers()


@_WD_WIN_ONLY
def test_child_flags_exclude_detached_and_pin_windowless_tier1():
    """真值表关键断言：无窗口家族占前两级，DETACHED_PROCESS 全链禁用。"""
    tiers = wd_mod._child_creationflags_tiers()
    assert tiers[0] == 0x09000200, f"tier-1 必须是真值表 C6（0x9000200），实际 0x{tiers[0]:08X}"
    assert tiers[1] == 0x08000200, f"tier-2 必须是真值表 C2（0x8000200），实际 0x{tiers[1]:08X}"
    for i, flags in enumerate(tiers):
        assert not (flags & 0x00000008), (
            f"第 {i + 1} 级含 DETACHED_PROCESS——真值表 C3/C4/C7/C8 实测全部"
            f"让真引擎重新挂上带窗 console（GetConsoleWindow()≠0）"
        )


@_WD_WIN_ONLY
def test_spawn_child_falls_back_when_breakaway_rejected(tmp_path):
    """R22 fail-open：BREAKAWAY 被拒 → 降级重试，且**同一个**日志句柄复用。"""
    calls = []
    logs = []

    class _FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            calls.append(kw)
            if kw.get("creationflags", 0) & subprocess.CREATE_BREAKAWAY_FROM_JOB:
                raise OSError(5, "拒绝访问（模拟 Job 未授权 breakaway）")

        def poll(self):
            return 0

    orig = wd_mod.subprocess.Popen
    wd_mod.subprocess.Popen = _FakeProc
    try:
        log_fh = tmp_path / "paper_console.log"
        with open(log_fh, "a", encoding="utf-8") as fh:
            proc, flags = wd_mod._spawn_child(["x"], fh, logs.append)
    finally:
        wd_mod.subprocess.Popen = orig

    assert len(calls) == 2, "应恰好降级重试一次"
    assert flags == 0x08000200, f"降级后应 = tier-2（真值表 C2 无窗口），实际 0x{flags:08X}"
    assert not (flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
    assert not (flags & 0x00000008), "降级链任何一级都不得引入 DETACHED_PROCESS"
    assert calls[0]["stdout"] is calls[1]["stdout"], "降级重试必须复用同一日志句柄"
    assert any("[WATCHDOG][WARN]" in m for m in logs), "降级必须留痕"


@_WD_WIN_ONLY
def test_spawn_child_falls_back_to_npg_only_when_windowless_rejected(tmp_path):
    """NO_WINDOW 也被拒 → 降到仅 CREATE_NEW_PROCESS_GROUP（fail-open 兜底，R22）。"""
    calls = []

    class _FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            calls.append(kw)
            if kw.get("creationflags", 0) & (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB
            ):
                raise OSError(5, "拒绝访问")

        def poll(self):
            return 0

    orig = wd_mod.subprocess.Popen
    wd_mod.subprocess.Popen = _FakeProc
    try:
        log_fh = tmp_path / "paper_console.log"
        with open(log_fh, "a", encoding="utf-8") as fh:
            proc, flags = wd_mod._spawn_child(["x"], fh, lambda m: None)
    finally:
        wd_mod.subprocess.Popen = orig

    assert len(calls) == 3, "两级被拒后应降到第 3 级"
    assert flags == subprocess.CREATE_NEW_PROCESS_GROUP


@_WD_WIN_ONLY
def test_spawn_child_all_tiers_fail_raises(tmp_path):
    """反向红线：全链失败必须抛出（由 run() 的崩溃分支处理），绝不返回假 proc。"""
    calls = []

    def _always_reject(cmd, **kw):
        calls.append(kw)
        raise OSError(5, "拒绝访问")

    orig = wd_mod.subprocess.Popen
    wd_mod.subprocess.Popen = _always_reject
    try:
        log_fh = tmp_path / "paper_console.log"
        with open(log_fh, "a", encoding="utf-8") as fh:
            with pytest.raises(OSError):
                wd_mod._spawn_child(["x"], fh, lambda m: None)
    finally:
        wd_mod.subprocess.Popen = orig

    assert len(calls) == len(wd_mod._child_creationflags_tiers())


# ---------------------------------------------------------------------------
# ⑩ 2026-09-07 死亡取证增强（B-3）：子进程异常退出时写一行
#    [WATCHDOG][FORENSIC]（rc + runtime + 所用标志 + 疑似死因）。
#    ⛔ 取证是旁路增强：诊断函数内部一切失败都 fail-open 成「未知…」，
#    绝不影响重启决策（R22）——下面两条 fail-open 用例钉死这一前提。
# ---------------------------------------------------------------------------
def test_diagnose_child_death_window_close_marker(tmp_path):
    """日志尾部出现 forrtl window-CLOSE → 死因指向 CTRL_CLOSE_EVENT（console 被关）。"""
    log = tmp_path / "paper_console.log"
    log.write_text(
        "正常启动行...\nforrtl: error (200): window-CLOSE\n", encoding="utf-8"
    )
    verdict = wd_mod._diagnose_child_death(1, console_log_path=log)
    assert "window-CLOSE" in verdict, f"应指向 window-CLOSE，实际：{verdict}"


def test_diagnose_child_death_traceback_marker(tmp_path):
    log = tmp_path / "paper_console.log"
    log.write_text("...\nTraceback (most recent call last):\n  ...\n", encoding="utf-8")
    verdict = wd_mod._diagnose_child_death(1, console_log_path=log)
    assert "Python" in verdict and "异常" in verdict, f"应指向 Python 异常，实际：{verdict}"


def test_diagnose_child_death_clean_log_falls_back_to_rc_semantics(tmp_path):
    """日志无已知特征：rc=1 → 提示启动早期失败；未知 rc → 明说「未知」。"""
    log = tmp_path / "paper_console.log"
    log.write_text("一切正常输出\n", encoding="utf-8")
    assert "启动早期失败" in wd_mod._diagnose_child_death(1, console_log_path=log)
    assert "未知" in wd_mod._diagnose_child_death(-9, console_log_path=log)


def test_diagnose_child_death_status_control_c_exit(tmp_path):
    """0xC000013A（控制台 CTRL 事件终止）即使日志无特征也要点名。"""
    log = tmp_path / "paper_console.log"
    log.write_text("无特征输出\n", encoding="utf-8")
    for rc in (3221225786, -1073741510):
        verdict = wd_mod._diagnose_child_death(rc, console_log_path=log)
        assert "0xC000013A" in verdict, f"rc={rc} 应点名 STATUS_CONTROL_C_EXIT，实际：{verdict}"


def test_diagnose_child_death_missing_log_is_fail_open(tmp_path):
    """⛔ R22：日志读不到必须返回「未知…」而不是抛错——取证绝不能影响重启决策。"""
    verdict = wd_mod._diagnose_child_death(1, console_log_path=tmp_path / "nope.log")
    assert verdict.startswith("未知")


def test_run_writes_forensic_line_once_on_crash(tmp_path, monkeypatch):
    """集成：run() 检测到子进程崩溃（rc!=0）时恰好写一条 [WATCHDOG][FORENSIC]。

    ⛔ 只读真实 paper_console.log 的尾部做诊断（fail-open），但断言只钉
    「有这条 + 含 rc/flags」——日志内容本身不注入断言，保证用例与仓库
    日志状态无关（hermetic）。
    """
    logs = []
    opened = []

    class _FakeProc:
        returncode = 1
        pid = 424242

        def poll(self):
            return 1  # 一轮 poll 即「已死」

    def _fake_open_child_log():
        fh = open(tmp_path / "child_console.log", "a", encoding="utf-8", buffering=1)
        opened.append(fh)
        return fh

    monkeypatch.setattr(
        wd_mod, "_spawn_child", lambda cmd, fh, log_fn: (_FakeProc(), 0x09000200)
    )
    monkeypatch.setattr(wd_mod, "_open_child_log", _fake_open_child_log)

    wd = wd_mod.Watchdog(["x"], max_restarts=1, sleep_fn=lambda s: None, log_fn=logs.append)
    rc = wd.run()

    assert rc == 1, "max_restarts=1 的崩溃应直接达上限停止"
    forensic = [m for m in logs if "[WATCHDOG][FORENSIC]" in m]
    assert len(forensic) == 1, f"应恰好一条取证行，实际 {len(forensic)} 条：{forensic}"
    assert "rc=1" in forensic[0]
    assert "0x09000200" in forensic[0], "取证行必须带上本次 spawn 实际使用的创建标志"
    for fh in opened:
        assert fh.closed, "日志句柄契约（P2-2）：run() 必须 close 子日志句柄"


def test_run_no_forensic_line_on_clean_exit(tmp_path, monkeypatch):
    """rc==0 正常退出不是「死亡」，不得刷取证行（避免噪音淹没真异常）。"""
    logs = []

    class _FakeProc:
        returncode = 0
        pid = 424243

        def poll(self):
            return 0

    monkeypatch.setattr(
        wd_mod, "_spawn_child", lambda cmd, fh, log_fn: (_FakeProc(), 0x08000200)
    )
    monkeypatch.setattr(
        wd_mod,
        "_open_child_log",
        lambda: open(tmp_path / "child_console.log", "a", encoding="utf-8", buffering=1),
    )

    wd = wd_mod.Watchdog(["x"], sleep_fn=lambda s: None, log_fn=logs.append)
    rc = wd.run()

    assert rc == 0
    assert not [m for m in logs if "[WATCHDOG][FORENSIC]" in m], "正常退出不得出现取证行"
