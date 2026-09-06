"""P0-A 启动器单测：引擎子进程必须以「无窗口 + 独立进程组」拉起。

覆盖：
① ``_engine_creationflags()`` 含 CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP，且不带可见窗口标志；
② ``main()`` 实际 Popen 时透传上述标志，并把 stdout/stderr 重定向到日志文件；
③ ``--watchdog`` 接线（2026-09-06 补缺口）：走看门狗 supervisor 实现崩溃自愈；
④ 回归护栏：不传 ``--watchdog`` 时仍直接 spawn MAIN（默认行为零变更）；
⑤ P1-1 单实例预检（仅 --watchdog 路径）：避免撞锁被误判崩溃刷假 CRITICAL；
⑥ 锁常量同源（team-lead 点名）：偏移引用引擎模块 + 锁定机制双向互操作钉死。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    # P2-1（QA 2026-09-06）：`is not None` 挡不住 stdout=DEVNULL(-3)/PIPE(-1)，
    # 那两者会让引擎控制台输出静默消失（= P0-1「行情冻结日志不可见」原故障形态）。
    # 改为同时排除哨兵值 + 断言真实落盘文件名。
    assert kw["stdout"] not in (None, subprocess.DEVNULL, subprocess.PIPE), (
        "stdout 必须重定向到真实日志文件，不得为 None/DEVNULL/PIPE"
    )
    assert str(getattr(kw["stdout"], "name", "")).endswith("paper_console.log")
    assert kw["stderr"] == subprocess.STDOUT


# ---------------------------------------------------------------------------
# P0-A 补缺口（2026-09-06）：--watchdog 崩溃自愈接线
# ---------------------------------------------------------------------------
# 背景：引擎崩溃（非 0 退出）后无人重启，只能等下一时段（08:55/13:25/20:55）
# 自动化来拉，中间整段行情断档。--watchdog 改为拉起 scripts/paper_watchdog.py
# （supervisor），由它按指数退避自动重启引擎。
# ⛔ 不用 start_paper_trading_watchdog.bat：该 BAT 需 cmd.exe，而自动化沙箱
#    拦截 cmd.exe 启动，故必须在 Python 启动器内加开关走纯 Python 路径。
def _install_fake_popen(monkeypatch, calls):
    """替换 Popen，捕获 (cmd, kw) 二元组。"""

    class _FakeProc:
        returncode = 0
        pid = 12345

        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

        def poll(self):
            return 0

    monkeypatch.setattr(ltw_mod.subprocess, "Popen", _FakeProc)


def _prepare(monkeypatch, tmp_path):
    """把入口脚本/日志路径都指向 tmp，避免 early return 与写脏仓库 logs/。"""
    main_py = tmp_path / "paper_trading_main.py"
    main_py.write_text("")
    monkeypatch.setattr(ltw_mod, "MAIN", str(main_py))
    wd_py = tmp_path / "paper_watchdog.py"
    wd_py.write_text("")
    monkeypatch.setattr(ltw_mod, "WATCHDOG", str(wd_py))
    monkeypatch.setattr(ltw_mod, "CONSOLE_LOG", str(tmp_path / "logs" / "paper_console.log"))
    return main_py, wd_py


def test_watchdog_spawns_watchdog_script_not_main(tmp_path, monkeypatch):
    """传 --watchdog → Popen 入口是 paper_watchdog.py（崩溃自愈 supervisor）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    main_py, wd_py = _prepare(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0
    assert calls, "Popen 未被调用"
    cmd = calls[0][0]
    assert cmd[0] == ltw_mod.PY
    assert cmd[1] == str(wd_py), "传 --watchdog 时应 spawn 看门狗"
    assert cmd[1] != str(main_py), "传 --watchdog 时不得再直接 spawn 引擎"


def test_without_watchdog_still_spawns_main(tmp_path, monkeypatch):
    """回归护栏：不传 --watchdog → 仍直接 spawn MAIN（默认行为零变更）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    main_py, wd_py = _prepare(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=[])
    assert rc == 0
    assert calls, "Popen 未被调用"
    cmd = calls[0][0]
    assert cmd[1] == str(main_py), "默认路径必须仍 spawn 引擎（防回归）"
    assert cmd[1] != str(wd_py)


def test_watchdog_path_keeps_windowless_and_log_redirect(tmp_path, monkeypatch):
    """看门狗路径同样必须「无窗口 + 独立进程组 + 日志重定向」，与引擎路径一致。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0
    kw = calls[0][1]
    assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not (kw["creationflags"] & ltw_mod.CREATE_NEW_CONSOLE), "不得使用可见窗口标志"
    # P2-1：同既有用例，必须排除 DEVNULL/PIPE 哨兵并锁定真实日志文件
    assert kw["stdout"] not in (None, subprocess.DEVNULL, subprocess.PIPE), (
        "stdout 必须重定向到真实日志文件，不得为 None/DEVNULL/PIPE"
    )
    assert str(getattr(kw["stdout"], "name", "")).endswith("paper_console.log")
    assert kw["stderr"] == subprocess.STDOUT


def test_watchdog_missing_returns_1_without_spawn(tmp_path, monkeypatch):
    """传 --watchdog 但看门狗脚本缺失 → rc==1 且未 spawn（对齐 MAIN 缺失处理）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    main_py, _ = _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "WATCHDOG", str(tmp_path / "nope_paper_watchdog.py"))

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 1
    assert not calls, "入口缺失时不得 spawn"


def test_watchdog_passthrough_args(tmp_path, monkeypatch):
    """透传参数（--config/--offline/--days/--max-restarts 等）正确附加到命令行。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _, wd_py = _prepare(monkeypatch, tmp_path)

    rc = ltw_mod.main(
        argv=[
            "--watchdog",
            "--config", "configs/paper.yaml",
            "--offline",
            "--days", "3",
            "--max-restarts", "7",
            "--stable-sec", "120",
        ]
    )
    assert rc == 0
    cmd = calls[0][0]
    assert cmd[1] == str(wd_py)
    tail = cmd[2:]
    assert tail[tail.index("--config") + 1] == "configs/paper.yaml"
    assert "--offline" in tail
    assert tail[tail.index("--days") + 1] == "3"
    assert tail[tail.index("--max-restarts") + 1] == "7"
    # --stable-sec 是 float 参数 → 序列化为 "120.0"；按数值断言，不锁死字符串格式
    assert float(tail[tail.index("--stable-sec") + 1]) == pytest.approx(120.0)
    # 未显式传入的项不得注入（保持子进程自身默认值）
    assert "--backoff-base-sec" not in tail


# ---------------------------------------------------------------------------
# P1-1（2026-09-06）：--watchdog 单实例预检——避免撞锁被误判崩溃刷假 CRITICAL
# ---------------------------------------------------------------------------
def _hold_instance_lock(pid_path):
    """真正持住引擎同款 OS 字节锁（msvcrt/flock），返回释放函数。

    偏移量从 launcher 的同源解析器取（不再引用任何本地字面量），这样本 helper
    与生产路径用的是同一个数，测试不会自己给自己打保票。
    """
    offset = ltw_mod._engine_lock_offset()
    assert offset is not None, "同源偏移必须可取（引擎模块不可加载则全部预检失效）"
    fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o644)
    os.lseek(fd, offset, os.SEEK_SET)
    if sys.platform.startswith("win"):
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release():
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return _release


def test_probe_detects_live_instance_via_real_os_lock(tmp_path):
    """真锁验证：别人持住同款字节锁 → 探测必须报「有存活实例」。"""
    pid_file = tmp_path / "paper.pid"
    pid_file.write_text("999999:0")  # 内容是死 PID，但锁是活的 → 必须按锁判定
    release = _hold_instance_lock(pid_file)
    try:
        assert ltw_mod._engine_instance_running(str(pid_file)) is True, (
            "锁被占用必须判为有存活实例（互斥由 OS 句柄仲裁，不依赖 PID 文本）"
        )
    finally:
        release()


def test_probe_reports_free_when_no_lock(tmp_path):
    """无人持锁 → 探测报「无存活实例」，且**不得残留占用**（否则引擎永远起不来）。"""
    pid_file = tmp_path / "paper.pid"
    pid_file.write_text("3876:0")  # 陈旧内容/死 PID，无进程持锁
    assert ltw_mod._engine_instance_running(str(pid_file)) is False

    # 探测后必须能立刻被别人拿到锁 —— 防止「探测函数自己占住锁」的灾难性 bug
    release = _hold_instance_lock(pid_file)
    release()


def test_probe_reports_free_when_pid_file_absent(tmp_path):
    """锁文件不存在 → 无存活实例（且不应凭空创建文件）。"""
    pid_file = tmp_path / "nope" / "paper.pid"
    assert ltw_mod._engine_instance_running(str(pid_file)) is False
    assert not pid_file.exists(), "预检不应凭空创建锁文件"


def test_watchdog_skips_when_engine_already_running(tmp_path, monkeypatch):
    """已有存活引擎 → 跳过拉起、不 spawn、rc==0（不是错误）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: True)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0, "已有存活实例属预期状态，不得报失败"
    assert not calls, "已有存活实例时不得再 spawn 看门狗（否则必撞锁→假 CRITICAL）"


def test_watchdog_proceeds_when_no_live_instance(tmp_path, monkeypatch):
    """无存活实例 → 正常拉起看门狗。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: False)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0
    assert calls, "无存活实例时应正常 spawn 看门狗"


def test_watchdog_probe_failure_is_fail_open(tmp_path, monkeypatch, capsys):
    """预检失败（返回 None）→ fail-open 照常拉起，绝不变成「永远拉不起」（R22）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: None)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0
    assert calls, "预检失败必须 fail-open 照常拉起"
    assert "[LAUNCH][WARN]" in capsys.readouterr().out


def test_probe_not_applied_without_watchdog(tmp_path, monkeypatch):
    """回归护栏：不带 --watchdog 时**不做**预检（默认路径零变更）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("默认路径不得调用单实例预检")

    monkeypatch.setattr(ltw_mod, "_engine_instance_running", _boom)
    rc = ltw_mod.main(argv=[])
    assert rc == 0
    assert calls


def test_unknown_args_do_not_error_under_watchdog(tmp_path, monkeypatch, capsys):
    """未知参数在 --watchdog 下不报错（`parse_known_args`），只告警并忽略。

    看门狗自身用严格 parse_args()：把未知参数喂给它会导致看门狗启动即崩
    （等于没装自愈），故未知参数只给直连引擎路径，并显式告警不做静默丢失。
    """
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=["--watchdog", "--totally-unknown", "42"])
    assert rc == 0, "未知参数不得导致启动失败"
    tail = calls[0][0][2:]
    assert "--totally-unknown" not in tail, "未知参数不得喂给严格解析的看门狗"
    assert "[LAUNCH][WARN]" in capsys.readouterr().out, "应显式告警被忽略的参数"


# ---------------------------------------------------------------------------
# 锁常量同源（2026-09-06，team-lead 点名确认，方案 A + B 组合）
# ---------------------------------------------------------------------------
# ⛔ 风险：预检与引擎必须抢**同一个字节**。若两边各写一份 _LOCK_OFFSET，
#    将来谁改了一边，预检**不会报错，只会静默失效**——最坏情况是「把空闲
#    误判成占用 → 系统永远拉不起来」，这是项目铁律最忌讳的全停失效模式。
#
#    方案：偏移走 A（运行时引用引擎模块，漂移物理上不可能发生）；
#          锁定机制走 B（无法复用引擎的内联实现，用互操作测试钉死行为）。
def test_lock_offset_is_sourced_from_engine(monkeypatch):
    """字节偏移必须是**同源引用**：launcher 里不得再留字面量。

    变异护栏：把 ``_engine_lock_offset()`` 改成返回另一个数（如 8192）→ ②③红。
    """
    # ① launcher 自身不得再持有 _LOCK_OFFSET 字面量
    assert not hasattr(ltw_mod, "_LOCK_OFFSET"), (
        "launcher 不得再定义 _LOCK_OFFSET 字面量（必须运行时从引擎模块取同源值），"
        "否则重新引入『两边各写一份、将来静默漂移』的全停风险"
    )

    engine_mod = ltw_mod._load_engine_module()
    assert engine_mod is not None, "引擎模块必须可加载——预检依赖它取同源偏移"

    # ② 解析出的值必须与引擎逐值相等（是引用，不是抄一份）
    resolved = ltw_mod._engine_lock_offset()
    assert resolved == engine_mod._LOCK_OFFSET, (
        f"预检偏移 {resolved} 与引擎偏移 {engine_mod._LOCK_OFFSET} 不一致——"
        "两边锁的不是同一个字节，预检完全失去意义"
    )

    # ③ 必须是**调用时**从引擎模块取，而不是 import 期快照或写死的常量。
    #    把引擎模块换成桩：桩说 7777，预检就必须用 7777。
    class _StubEngine:
        _LOCK_OFFSET = 7777

    monkeypatch.setattr(ltw_mod, "_load_engine_module", lambda: _StubEngine())
    assert ltw_mod._engine_lock_offset() == 7777, (
        "预检偏移必须每次调用时从引擎模块现场读取；读成别的值说明又退化成了写死常量"
    )

    # ④ 引擎模块取不到 → 返回 None（调用方 fail-open），绝不自己猜一个偏移
    monkeypatch.setattr(ltw_mod, "_load_engine_module", lambda: None)
    assert ltw_mod._engine_lock_offset() is None, (
        "取不到引擎同源偏移时必须返回 None 让调用方 fail-open，"
        "绝不能猜一个字节去锁（猜错 = 静默失效，比不做预检更危险）"
    )


def test_lock_probe_interops_with_engine_lock(tmp_path):
    """双向互操作：用**引擎自己的** ``_acquire_instance_lock`` 验证探测行为。

    这条才是锁耦合的真正护栏——它不比对数字，而比对**行为**：引擎拿到的锁，
    预检必须认；引擎放掉的锁，预检必须放行。任一边改了偏移或换锁定方式
    （一边 ``msvcrt.locking``、另一边 ``LockFileEx``），这里立刻红。

    变异护栏：把引擎 ``_acquire_instance_lock`` 里的 ``os.lseek(fd, _LOCK_OFFSET)``
    改成 ``_LOCK_OFFSET + 1``（模拟单边漂移）→ 本用例红。
    """
    engine_mod = ltw_mod._load_engine_module()
    pid_file = tmp_path / "paper.pid"
    pid_file.write_text("0:0")

    # ① 引擎持锁 → 探测必须报「有存活实例」
    #    失配方向：报 False → 13:25/20:55 重复挂看门狗 → 撞锁 rc=1 → 假 CRITICAL
    lock = engine_mod._acquire_instance_lock(pid_file)
    assert lock is not None, "首个实例必须能拿到锁（否则本用例前提不成立）"
    try:
        assert ltw_mod._engine_instance_running(str(pid_file)) is True, (
            "引擎持有的锁，launcher 预检必须识别为占用；识别不了就等于没做预检，"
            "会退化回 P1-1 的假 [CRITICAL] 连续崩溃告警"
        )
        # P1-C 附带保证：锁区避开文件头 → 实例运行期间诊断内容仍可读
        assert pid_file.read_text(encoding="utf-8").startswith(str(os.getpid())), (
            "锁区不得覆盖文件头，否则实例运行期间健康检查读不到 PID 诊断内容"
            "（P1-C 取证已证明锁 byte 0 会导致实测 PermissionError）"
        )
    finally:
        lock.release()

    # ② 引擎释放 → 探测必须报「无存活实例」
    #    失配方向：报 True → **永远拉不起来**，项目铁律最忌讳的全停失效模式
    assert ltw_mod._engine_instance_running(str(pid_file)) is False, (
        "引擎已释放锁，预检必须放行；误判为占用会导致系统永远拉不起来（全停）"
    )

    # ③ 探测之后引擎仍能再次拿到锁（探测不得残留占用）
    lock2 = engine_mod._acquire_instance_lock(pid_file)
    assert lock2 is not None, "预检后引擎必须仍能取得锁——探测函数不得残留占用"
    lock2.release()


def test_pid_file_matches_engine_data_dir():
    """锁**路径**也必须与引擎一致（同一类漂移面，走 Plan B 护栏）。

    引擎用 ``Path(paper_cfg.get("data_dir", "data/paper")) / "paper.pid"``，
    launcher 里是常量。若将来有人改 ``configs/paper.yaml::paper.data_dir`` 而
    launcher 没跟着改 → 预检永远探测不到锁 → 静默退化回 P1-1 假 CRITICAL。
    因改动生产配置属于低频且需评审的操作，这里用「读配置逐值比对」兜底，
    不把配置加载塞进 launcher 的运行时路径（避免新增运行时依赖与失败面）。
    """
    from omegaconf import OmegaConf

    cfg = OmegaConf.load(Path(ltw_mod.ROOT) / "configs" / "paper.yaml")
    data_dir = str(OmegaConf.select(cfg, "paper.data_dir", default="data/paper"))
    expected = os.path.normpath(os.path.join(ltw_mod.ROOT, data_dir, "paper.pid"))
    assert os.path.normpath(ltw_mod.PID_FILE) == expected, (
        f"launcher.PID_FILE 与引擎实际锁路径不一致：{ltw_mod.PID_FILE} != {expected}"
        "（引擎按 configs/paper.yaml::paper.data_dir 解析，改配置必须同步本常量）"
    )
