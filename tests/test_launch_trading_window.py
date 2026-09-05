"""P0-A 启动器单测：引擎子进程必须以「无窗口 + 独立进程组」拉起。

覆盖：
① ``_engine_creationflags()`` 含 CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP，且不带可见窗口标志；
② ``main()`` 实际 Popen 时透传上述标志，并把 stdout/stderr 重定向到日志文件。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "launch_trading_window.py"
_SPEC = importlib.util.spec_from_file_location("launch_trading_window", str(_SCRIPT))
ltw_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ltw_mod)


def test_engine_creationflags_windowless():
    flags = ltw_mod._engine_creationflags()
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    # 不再使用可见窗口标志（P0-A 根因）
    assert not (flags & ltw_mod.CREATE_NEW_CONSOLE)


def test_main_spawns_windowless_with_log_redirect(tmp_path, monkeypatch):
    captured = []

    class _FakeProc:
        returncode = 0
        pid = 12345

        def __init__(self, cmd, **kw):
            captured.append(kw)

        def poll(self):
            return 0

    monkeypatch.setattr(ltw_mod.subprocess, "Popen", _FakeProc)
    # MAIN 存在，避免 early return
    main_py = tmp_path / "main.py"
    main_py.write_text("")
    monkeypatch.setattr(ltw_mod, "MAIN", str(main_py))

    rc = ltw_mod.main()
    assert rc == 0
    assert captured, "Popen 未被调用"
    kw = captured[0]
    assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    assert kw["stdout"] is not None  # 重定向到 logs/paper_console.log
    assert kw["stderr"] == subprocess.STDOUT
