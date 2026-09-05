"""P2-5 看门狗单测（崩溃自动重启 supervisor）。

覆盖：
① 正常退出（rc==0）→ 不重启、计数归零；
② 崩溃计数递增 + 指数退避（base×2^(n-1) 封顶 max）；
③ 达连续崩溃上限 → 不重启（告警停止）；
④ 稳定运行 ≥ stable_sec 后崩溃 → 计数重置（不耗尽上限）；
⑤ 集成：子进程先崩溃 2 次再正常退出 → 看门狗重启 2 次后停止；
⑥ 集成：子进程始终崩溃（≤ stable）→ 达 max_restarts 后停止并 CRITICAL 告警。

注：集成测试用受控假子进程（临时 .py 脚本 + 计数器文件），退避设极小避免耗时。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

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
    assert kw["stdout"] is not None  # 重定向到 logs/paper_console.log
