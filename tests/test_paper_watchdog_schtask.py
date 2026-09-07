"""计划任务入口 ``scripts/paper_watchdog_schtask.py`` 单测。

覆盖：
① ``_bootstrap_runtime()`` 把「计划任务缺省环境」补齐（cwd / PYTHONPATH /
   PYTHONUNBUFFERED）—— 这是直接调度 ``paper_watchdog.py`` 会静默失败的三处根因；
② 日志重定向落点必须是 ``logs/watchdog_schtask.log``，且在**调用时**解析模块级
   ``LOG_PATH``（写成默认参数会让 monkeypatch 失效、测试写脏仓库 logs/）；
③ 单实例预检：已有存活实例 → 跳过拉起、不构造看门狗、rc==0（不是错误）；
④ R22 fail-open：预检失败（None）→ 照常拉起；
⑤ 无存活实例 → 正常**前台**运行看门狗；
⑥ import 本模块**不得**有副作用（不 chdir、不写日志）。

⛔ 平台：全部为纯逻辑（文件系统 / 进程环境 / 桩注入），Windows 与 POSIX 均可跑，
   故**不加** skipif —— 计划任务虽是 Windows 概念，但被验证的这段代码本身是
   跨平台的（CI 上真跑比跳过有价值）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_watchdog_schtask.py"
_SPEC = importlib.util.spec_from_file_location("paper_watchdog_schtask", str(_SCRIPT))
st_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(st_mod)


def _install_stub_watchdog(monkeypatch, calls):
    """注入看门狗桩：记录构造参数，``run()`` 返回 0（不真拉子进程）。"""

    class _StubWatchdog:
        def __init__(self, child_cmd, **kw):
            calls.append(("construct", list(child_cmd), kw))

        def run(self):
            calls.append(("run",))
            return 0

    class _StubModule:
        Watchdog = _StubWatchdog

        @staticmethod
        def _build_child_cmd(args):
            return [sys.executable, "MAIN", "--config", args.config]

    monkeypatch.setattr(st_mod, "_load_watchdog", lambda: _StubModule())


def _run_main_isolated(monkeypatch, tmp_path, probe_result, calls):
    """在隔离环境下跑 ``main()``：cwd 与 stdout/stderr 都指向 tmp，避免副作用外溢。"""
    monkeypatch.chdir(tmp_path)
    _install_stub_watchdog(monkeypatch, calls)
    monkeypatch.setattr(st_mod, "LOG_PATH", tmp_path / "logs" / "watchdog_schtask.log")
    monkeypatch.setattr(st_mod, "_engine_running", lambda: probe_result)

    old_pythonpath = os.environ.get("PYTHONPATH")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        return st_mod.main()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


# ---------------------------------------------------------------------------
# ⑥ import 无副作用
# ---------------------------------------------------------------------------
def test_import_has_no_side_effects():
    """import 本模块**不得** chdir、不得改动日志文件。

    ⛔ 这条是测试能安全加载本模块的前提：若模块级就 ``os.chdir(ROOT)``，
    整轮 pytest 的工作目录会被悄悄改掉，产生一堆难以归因的假失败。

    ⚠️ 不能断言「日志文件不存在」——生产上该文件本来就可能已存在（真实入口
    跑过就会创建）。改为断言**指纹不变**（存在性 + 大小 + mtime）。
    """
    spec = importlib.util.spec_from_file_location("_st_side_effect_probe", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)

    def _fingerprint(path: Path):
        if not path.exists():
            return None
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)

    cwd_before = os.getcwd()
    log_before = _fingerprint(Path(st_mod.LOG_PATH))

    spec.loader.exec_module(mod)

    assert os.getcwd() == cwd_before, "import 不得改变进程工作目录"
    assert _fingerprint(Path(st_mod.LOG_PATH)) == log_before, "import 不得写日志文件"


# ---------------------------------------------------------------------------
# ① 运行环境补齐
# ---------------------------------------------------------------------------
def test_bootstrap_sets_cwd_pythonpath_and_unbuffered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)

    st_mod._bootstrap_runtime()
    try:
        assert Path(os.getcwd()).resolve() == Path(st_mod.ROOT).resolve(), (
            "必须把 cwd 切到仓库根：计划任务默认启动目录是 system32，"
            "否则 --config configs/paper.yaml 相对路径找不到配置"
        )
        assert str(st_mod.ROOT) in os.environ["PYTHONPATH"], (
            "必须补 PYTHONPATH=仓库根，否则 import hexbroker 失败"
        )
        assert os.environ["PYTHONUNBUFFERED"] == "1", "日志必须无缓冲（崩溃也不丢）"
    finally:
        os.environ.pop("PYTHONPATH", None)


def test_bootstrap_preserves_existing_pythonpath(tmp_path, monkeypatch):
    """已有 PYTHONPATH 时**追加**而非覆盖（覆盖会破坏调用方其它依赖）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/some/other/path")
    try:
        st_mod._bootstrap_runtime()
        parts = os.environ["PYTHONPATH"].split(os.pathsep)
        assert str(st_mod.ROOT) in parts
        assert "/some/other/path" in parts
    finally:
        os.environ["PYTHONPATH"] = "/some/other/path"


# ---------------------------------------------------------------------------
# ② 日志落点
# ---------------------------------------------------------------------------
def test_redirect_targets_module_level_log_path(tmp_path, monkeypatch):
    """落点必须在**调用时**解析 LOG_PATH —— 写成默认参数会让 monkeypatch 失效。"""
    target = tmp_path / "logs" / "watchdog_schtask.log"
    monkeypatch.setattr(st_mod, "LOG_PATH", target)
    old_out, old_err = sys.stdout, sys.stderr
    try:
        fh = st_mod._redirect_stdout_stderr()
        try:
            assert Path(fh.name) == target, "必须写到 monkeypatch 后的 LOG_PATH"
            assert sys.stdout is fh and sys.stderr is fh
            assert fh.line_buffering, "行缓冲：进程被杀时已写入的内容不能丢"
            print("probe-line")
            assert "probe-line" in target.read_text(encoding="utf-8")
        finally:
            fh.close()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_default_log_path_is_repo_logs_dir():
    """默认落点必须是仓库内 logs/watchdog_schtask.log（排障时凭直觉能找到）。"""
    assert st_mod.LOG_PATH.parent.name == "logs"
    assert st_mod.LOG_PATH.name == "watchdog_schtask.log"
    assert st_mod.LOG_PATH.parent.parent == st_mod.ROOT


# ---------------------------------------------------------------------------
# ③④⑤ 单实例预检 / fail-open / 正常拉起
# ---------------------------------------------------------------------------
def test_skips_launch_when_instance_already_running(tmp_path, monkeypatch):
    """已有存活实例 → 跳过、不构造看门狗、rc==0。

    ⛔ 防的是 P1-1 原故障：13:25 若 08:55 的实例还活着，新看门狗去 spawn 引擎
    → 撞单实例锁 rc=1 → 被判崩溃 → 退避重启 → 刷 10 条**假** [CRITICAL]。
    """
    calls: list = []
    rc = _run_main_isolated(monkeypatch, tmp_path, True, calls)

    assert rc == 0, "已有存活实例属预期状态，不得报失败"
    assert calls == [], "已有存活实例时不得再构造看门狗（否则必撞锁 → 假 CRITICAL）"


def test_fail_open_when_probe_fails(tmp_path, monkeypatch):
    """预检失败（None）→ 照常拉起（R22：绝不让预检变成「永远拉不起」）。"""
    calls: list = []
    rc = _run_main_isolated(monkeypatch, tmp_path, None, calls)

    assert rc == 0
    assert any(c[0] == "construct" for c in calls), "预检失败必须 fail-open 照常拉起"
    assert any(c[0] == "run" for c in calls)


def test_launches_watchdog_when_no_live_instance(tmp_path, monkeypatch):
    calls: list = []
    rc = _run_main_isolated(monkeypatch, tmp_path, False, calls)

    assert rc == 0
    construct = [c for c in calls if c[0] == "construct"]
    assert construct, "无存活实例时应正常构造看门狗"
    child_cmd = construct[0][1]
    assert any("MAIN" in part for part in child_cmd), (
        f"子进程命令必须指向交易引擎，实际 {child_cmd}"
    )
    assert "configs/paper.yaml" in child_cmd
    assert any(c[0] == "run" for c in calls), "必须**前台**运行看门狗（阻塞，不 detach）"


def test_logs_reason_when_skipping(tmp_path, monkeypatch):
    """跳过时必须把原因**落盘** —— 计划任务无人值守，日志是唯一的排障线索。

    ⛔ 若只是静默 return 0，运维看到「任务成功但引擎没起来」将无从下手。
    """
    calls: list = []
    _run_main_isolated(monkeypatch, tmp_path, True, calls)
    log_text = (tmp_path / "logs" / "watchdog_schtask.log").read_text(encoding="utf-8")

    assert "已有存活交易引擎实例" in log_text, "跳过原因必须落盘，不得静默 return"


def test_entry_has_no_blocking_or_gating_calls():
    """无人值守红线：入口不得有阻塞等待或前置门禁。

    ⛔ Python 里 ``pause`` 的等价物是 ``input()`` —— 它会让计划任务永远挂在
    「等待一个不会到来的按键」，任务看起来「正在运行」而引擎根本没起。
    ⛔ ``--health-check`` 前置同理：行情源/信号缓存临时不可用时，本该「拉起来
    再说」，前置门禁会把它变成「系统整天没起来」，属 R22 禁止的形态。

    ⛔ 断言必须只扫**代码**、不扫字符串：本文件 docstring 里正当提到了
    ``--health-check``（说明为什么不要它），朴素子串断言会误伤自己。
    故用 tokenize 剥掉 STRING/COMMENT 后再判。
    """
    import io
    import tokenize

    src = _SCRIPT.read_text(encoding="utf-8")
    code_only = "".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.STRING, tokenize.COMMENT)
    )
    assert "input(" not in code_only, "无人值守入口不得等待按键（等价 BAT 里的 pause）"
    assert "health_check" not in code_only, "不得前置 health-check 门禁（R22：不许全停）"
    assert "health-check" not in code_only
