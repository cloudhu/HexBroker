"""P0-A 启动器单测：引擎子进程必须以「无窗口 + 独立进程组」拉起。

覆盖：
① ``_engine_creationflags()`` 含 CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP，且不带可见窗口标志；
② ``main()`` 实际 Popen 时透传上述标志，并把 stdout/stderr 重定向到日志文件；
③ ``--watchdog`` 接线（2026-09-06 补缺口）：走看门狗 supervisor 实现崩溃自愈；
④ 回归护栏：不传 ``--watchdog`` 时仍直接 spawn MAIN（默认行为零变更）；
⑤ P1-1 单实例预检（仅 --watchdog 路径）：避免撞锁被误判崩溃刷假 CRITICAL；
⑥ 锁常量同源（team-lead 点名）：偏移引用引擎模块 + 锁定机制双向互操作钉死。
⑦ 抢锁异常窄化（team-lead 点名收口）：非「被占用」errno 必须 fail-open，
   不得判成「有存活实例」→ 防止「系统永远起不来且日志无异常」的全停形态；
⑧ 抢锁**非阻塞**前提的时延断言（QA 发现后 team-lead 点名）：换成阻塞锁时
   返回值语义完全不变、19 条用例仍全绿，只有耗时从 0.01s 涨到 ~9s，
   故必须断言「持锁时 <1s 返回」——纯返回值断言抓不到这个退化。
⑨ 2026-09-07「进程被外部回收」修复：创建标志**降级链** —— 最强一组含
   DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB；被 OS 拒绝时必须 fail-open
   降级重试，绝不让护栏变成「引擎拉不起来」（R22）；全链失败必须抛出，
   绝不静默返回成功。
"""
from __future__ import annotations

import errno
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

# ⛔ CI（ubuntu runner）2026-09-06：本文件有两类**本质 Windows 专属**的用例——
# ① 断言引用 subprocess.CREATE_NO_WINDOW / CREATE_NEW_PROCESS_GROUP（仅 Windows
#    存在，POSIX 上 AttributeError）；② 单实例锁同源契约依赖引擎模块
#    （paper_trading_main.py，msvcrt 字节锁），POSIX 上引擎模块不可加载、
#    _load_engine_module() 返回 None → 同源偏移断言必红。这两类统一标 skipif，
#    其余 13 条（参数解析/命令构建/预检 fail-open/日志重定向路径选择等纯逻辑）
#    跨平台真实可跑、继续在 CI 覆盖。
_WIN_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows 专属契约（subprocess.CREATE_* 常量 / msvcrt 字节锁同源）；POSIX 上前提不成立",
)


@_WIN_ONLY
def test_engine_creationflags_windowless():
    flags = ltw_mod._engine_creationflags()
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    # 不再使用可见窗口标志（P0-A 根因）
    assert not (flags & ltw_mod.CREATE_NEW_CONSOLE)


@_WIN_ONLY
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


@_WIN_ONLY
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


@_WIN_ONLY
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


@_WIN_ONLY
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


def test_slow_probe_emits_warn_without_changing_control_flow(tmp_path, monkeypatch, capsys):
    """P3-3：预检耗时超阈值 → 打慢预检告警，**且控制流完全不受影响**。

    背景：非阻塞预检 ~0.01s，退化成阻塞锁（LK_NBLCK→LK_LOCK）~9s 且返回值
    语义完全不变 —— 时延护栏只在跑测试时生效，三个时段无人值守，需要这条
    运行时信号。⛔ 告警只许打印：不许 return/raise/改拉起判定（R22）。
    """
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)

    import time as _time

    def _slow_probe(*a, **k):
        _time.sleep(2.5)  # > SLOW_PROBE_WARN_SEC(2.0)
        return False

    monkeypatch.setattr(ltw_mod, "_engine_instance_running", _slow_probe)
    rc = ltw_mod.main(argv=["--watchdog"])

    assert rc == 0, "慢预检只告警，不得影响返回码"
    assert calls, "慢预检不得阻断后续拉起逻辑"
    out = capsys.readouterr().out
    assert "[LAUNCH][WARN] 单实例预检耗时" in out, "超阈值必须打慢预检告警"
    assert "2." in out, "告警必须携带实测耗时（可读性：运维要知道慢到什么程度）"


def test_fast_probe_emits_no_slow_probe_warn(tmp_path, monkeypatch, capsys):
    """P3-3 反向：正常速度预检**不得**出现慢预检告警（否则告警变新噪声源）。"""
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: False)

    rc = ltw_mod.main(argv=["--watchdog"])

    assert rc == 0
    assert calls
    out = capsys.readouterr().out
    assert "单实例预检耗时" not in out, "正常速度预检不得触发慢预检告警（告警必须保持可信）"


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
@_WIN_ONLY
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


@_WIN_ONLY
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


# ---------------------------------------------------------------------------
# 异常窄化（2026-09-06 收口，team-lead 点名）
# ---------------------------------------------------------------------------
# ⛔ 防「全停」：未窄化前 `except Exception: return True` 把**所有**抢锁异常
#    都判成「有存活实例」→ 启动器直接跳过拉起 → 系统永远起不来，而自动化只会
#    安静回报「已有实例跳过」，日志上看不出任何异常。
def test_non_busy_oserror_during_lock_is_fail_open(tmp_path, monkeypatch):
    """抢锁抛**非「被占用」**的 errno → 必须返回 None（fail-open），绝不判 True。

    白名单是**实测标定**的（win32 / CPython 3.13）：真被占用 = EACCES(13)，
    非占用类失败 = EBADF(9) / EINVAL(22)，见 artifacts/_tmp/_R4_errno.txt。
    """
    pid_file = tmp_path / "paper.pid"
    pid_file.write_text("0:0")
    monkeypatch.setattr(ltw_mod, "_engine_lock_offset", lambda: 4096)

    # 正向护栏：白名单不能是空的，也不能漏掉实测的「被占用」errno
    assert errno.EACCES in ltw_mod._LOCK_BUSY_ERRNOS, (
        "EACCES 是实测的『锁被占用』errno，必须在白名单内；漏掉会把真占用判成空闲，"
        "退化回 P1-1（13:25/20:55 重复挂看门狗 → 假 CRITICAL）"
    )

    injected = OSError(errno.EPERM, "模拟非占用类抢锁失败")
    assert injected.errno not in ltw_mod._LOCK_BUSY_ERRNOS, (
        "注入的 errno 必须不在『锁被占用』白名单内，否则本用例失去意义"
    )

    def _raise(*_a, **_k):
        raise injected

    if sys.platform.startswith("win"):
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", _raise)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", _raise)

    assert ltw_mod._engine_instance_running(str(pid_file)) is None, (
        f"errno={injected.errno} 属非占用类失败，必须 fail-open 返回 None；"
        "判成 True 会让启动器永远跳过拉起（全停），且日志上看不出任何异常"
    )


@_WIN_ONLY
def test_lock_probe_returns_promptly_when_lock_held(tmp_path):
    """⛔ 时延护栏：持锁时预检必须**秒级**返回 True，绝不能退化成阻塞等待。

    ⛔ 为什么必须断言**时延**、光断言返回值不够：把非阻塞锁 ``LK_NBLCK`` 换成
    阻塞锁 ``LK_LOCK``，返回值语义**完全不变**——阻塞重试耗尽后抛出的
    ``EDEADLOCK(36)`` 恰好落在 ``_LOCK_BUSY_ERRNOS`` 白名单里，照样 return True。
    QA 实测：19 条用例**全绿**，日志正常、无任何告警，只有单用例耗时从 0.34s
    变成 9.12s。也就是说，纯返回值断言**抓不到**这个退化。

    后果不是报错，而是**慢性降级**：08:55 / 13:25 / 20:55 三个时段在已有实例
    存活时（**常态**，不是异常）每次预检多卡约 9 秒。

    ⛔ 这不是理论推演：2026-09-06 提交 ``83a63ef`` 就真的把 ``LK_LOCK`` 混进过
    HEAD（并发变异污染），已 amend 清除。

    实现要点：预检放进**守护线程**并限时 join —— 万一将来退化成 ``flock(LOCK_EX)``
    这类**永久**阻塞，本用例会**判红**，而不是把整个测试进程挂死。
    """
    import threading
    import time

    pid_file = tmp_path / "paper.pid"
    pid_file.write_text(f"{os.getpid()}:0")
    lock_offset = ltw_mod._engine_lock_offset()
    assert lock_offset is not None, "同源偏移必须可取（引擎模块不可加载则预检失效）"

    release = _hold_instance_lock(pid_file)  # 真正持住引擎同款字节锁
    try:
        box: dict = {}

        def _probe() -> None:
            t0 = time.perf_counter()
            # 显式传 offset：把「按文件路径加载引擎模块」的开销排除在计时窗口外，
            # 让这条断言只盯住**抢锁动作本身**
            box["ret"] = ltw_mod._engine_instance_running(
                str(pid_file), lock_offset=lock_offset
            )
            box["elapsed"] = time.perf_counter() - t0

        th = threading.Thread(target=_probe, daemon=True)
        th.start()
        th.join(30.0)
        assert not th.is_alive(), (
            "预检 30s 内没有返回——已退化成阻塞锁（如 flock(LOCK_EX) 不带 NB）。"
            "这不是「慢」，是**挂死**：三个时段会永久卡在预检上"
        )
        assert box.get("ret") is True, (
            f"锁被占用时必须返回 True，实际 {box.get('ret')!r}"
        )
        assert box["elapsed"] < 1.0, (
            f"锁被占用时预检必须**秒级**返回，实测 {box['elapsed']:.3f}s。"
            "≈9s 的典型成因是把 LK_NBLCK 换成了阻塞锁 LK_LOCK——返回值语义不变，"
            "但三个时段每次预检都要多等约 9 秒，且无任何告警、日志完全正常"
        )
    finally:
        release()


# ---------------------------------------------------------------------------
# ⑨ 2026-09-07「进程被外部回收」修复：创建标志降级链 + fail-open
# ---------------------------------------------------------------------------
# 背景：08:44 拉起的看门狗/引擎在 shell 会话结束时被一并清理（日志无 traceback、
# 看门狗无重启记录、心跳精确冻结在会话 teardown 时刻）。修复方向是叠加 Windows
# 脱离语义（DETACHED_PROCESS / CREATE_BREAKAWAY_FROM_JOB），但 BREAKAWAY 在
# Job 未授权时会让 CreateProcess **直接失败**——「加护栏」反而变成「拉不起来」，
# 属 R22 明令禁止的新增全停失效模式。故必须是**链**且必须 fail-open。
#
# ⛔ 这些断言清一色是 Windows 专属（引用 subprocess.DETACHED_PROCESS /
#    CREATE_BREAKAWAY_FROM_JOB，POSIX 上 AttributeError），必须带 _WIN_ONLY，
#    否则 CI ubuntu runner 会红（2026-09-06 已因此修过一轮）。
def test_creationflags_tiers_platform_shape():
    """跨平台形状断言（本条**不带** skipif，CI 也要跑）。

    - Windows：3 级、单调降级（每级是前一级的子集）、末级 == 2026-09-06 前的
      旧值（NPG|NO_WINDOW）——保证最坏情况退回到「已验证可拉起」的行为；
    - POSIX：必须是 ``[0]`` 且 ``_engine_creationflags() == 0``。CPython 在
      POSIX 分支上对 ``creationflags != 0`` **直接 raise ValueError**，不是忽略
      ——旧实现无条件返回 Windows 常量，Linux 上任何真实 spawn 都会当场抛错。
    """
    tiers = ltw_mod._engine_creationflags_tiers()
    assert tiers, "降级链不得为空（空链 = 永远拉不起子进程）"

    if sys.platform.startswith("win"):
        assert len(tiers) == 3, f"Windows 上应为 3 级降级链，实际 {[hex(t) for t in tiers]}"
        for i in range(len(tiers) - 1):
            assert tiers[i + 1] & ~tiers[i] == 0, (
                f"第 {i + 2} 级(0x{tiers[i + 1]:08X}) 必须是第 {i + 1} 级"
                f"(0x{tiers[i]:08X}) 的子集——降级链只许做减法"
            )
        assert tiers[-1] == (ltw_mod.CREATE_NEW_PROCESS_GROUP | ltw_mod.CREATE_NO_WINDOW), (
            "末级必须退回到 2026-09-06 前的旧标志（已验证可拉起），否则降级失去意义"
        )
    else:
        assert tiers == [0], (
            "POSIX 上 creationflags 非 0 会被 CPython 直接 ValueError，必须恒为 [0]"
        )
        assert ltw_mod._engine_creationflags() == 0, (
            "POSIX 上 _engine_creationflags() 必须返回 0（同上：非 0 会 ValueError）"
        )


@_WIN_ONLY
def test_engine_creationflags_include_detach_and_breakaway():
    """最强一组必须含 DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB，且不带可见窗口。"""
    flags = ltw_mod._engine_creationflags_tiers()[0]
    assert flags & subprocess.DETACHED_PROCESS, "缺少 DETACHED_PROCESS（脱离父控制台）"
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB, (
        "缺少 CREATE_BREAKAWAY_FROM_JOB（脱离父 Job Object）"
    )
    # 原有语义一个都不能丢
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not (flags & ltw_mod.CREATE_NEW_CONSOLE)


def _install_fake_popen_selective(monkeypatch, calls, reject):
    """安装会**按标志选择性拒绝**的假 Popen。

    ``reject(flags) -> bool``：返回 True 则本次 spawn 抛 OSError(5)（模拟
    Job 未授权 breakaway 时 CreateProcess 被拒）。
    """

    class _FakeProc:
        returncode = 0
        pid = 12345

        def __init__(self, cmd, **kw):
            flags = kw.get("creationflags", 0)
            calls.append(kw)
            if reject(flags):
                raise OSError(5, "拒绝访问（模拟 Job 未授权 breakaway）")

        def poll(self):
            return 0

    monkeypatch.setattr(ltw_mod.subprocess, "Popen", _FakeProc)


def _prepare_detached(monkeypatch, tmp_path):
    """``_prepare`` + 把单实例预检固定为「无存活实例」。

    ⛔ 为什么必须钉死：本组用例断言的是 **Popen 被尝试了几次**，而预检一旦报
    「有存活实例」，``main()`` 会在 spawn 之前 ``return 0``——真有引擎在跑时
    用例就会假红（且红得莫名其妙）。预检本身由其它用例专门覆盖，这里固定它
    以隔离变量。
    """
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: False)


@_WIN_ONLY
def test_main_spawns_with_full_detach_flags(tmp_path, monkeypatch):
    """正常路径：main() 必须用**最强一组**标志拉起（未降级）。"""
    calls = []
    _install_fake_popen_selective(monkeypatch, calls, lambda flags: False)
    _prepare_detached(monkeypatch, tmp_path)

    assert ltw_mod.main(argv=["--watchdog"]) == 0
    assert len(calls) == 1, "未被拒绝时不得出现多余的重试"
    flags = calls[0]["creationflags"]
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP


@_WIN_ONLY
def test_spawn_falls_back_when_breakaway_rejected(tmp_path, monkeypatch, capsys):
    """⛔ R22 fail-open：BREAKAWAY 被拒 → 必须**降级重试**，绝不让引擎拉不起来。

    这是本次修复的核心护栏：若没有降级，Job 未授权时 CreateProcess 直接失败，
    「加了护栏」反而导致系统永远起不来，且日志上只是一条 WinError 5。
    """
    calls = []
    _install_fake_popen_selective(
        monkeypatch, calls,
        lambda flags: bool(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB),
    )
    _prepare_detached(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0, "标志被拒必须降级重试成功，绝不能变成启动失败"
    assert len(calls) == 2, "应恰好重试一次"
    assert calls[0]["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    fallback = calls[1]["creationflags"]
    assert not (fallback & subprocess.CREATE_BREAKAWAY_FROM_JOB), "降级后必须去掉失败的那一位"
    assert fallback & subprocess.DETACHED_PROCESS, "只降级失败位，DETACHED 应保留"
    assert fallback & subprocess.CREATE_NO_WINDOW and fallback & subprocess.CREATE_NEW_PROCESS_GROUP
    assert "[LAUNCH][WARN]" in capsys.readouterr().out, "降级必须留痕，否则运维无从知晓"


@_WIN_ONLY
def test_spawn_falls_back_to_legacy_when_only_legacy_allowed(tmp_path, monkeypatch):
    """连 DETACHED 也被拒 → 必须一路降到**旧标志**（2026-09-06 前，已验证可拉起）。"""
    calls = []
    legacy = ltw_mod.CREATE_NEW_PROCESS_GROUP | ltw_mod.CREATE_NO_WINDOW
    _install_fake_popen_selective(monkeypatch, calls, lambda flags: flags != legacy)
    _prepare_detached(monkeypatch, tmp_path)

    rc = ltw_mod.main(argv=["--watchdog"])
    assert rc == 0
    assert len(calls) == 3, "两级被拒后应降到第 3 级"
    assert calls[-1]["creationflags"] == legacy


@_WIN_ONLY
def test_spawn_all_tiers_fail_raises_no_silent_success(tmp_path, monkeypatch):
    """⛔ 反向红线：全链失败必须**抛出**，绝不吞掉后返回成功。

    静默失败是最坏形态——自动化会回报「已拉起」而实际没有任何引擎在跑，
    且日志上无异常（这是 R22 最忌讳的、看不出问题的全停）。
    """
    calls = []
    _install_fake_popen_selective(monkeypatch, calls, lambda flags: True)
    _prepare_detached(monkeypatch, tmp_path)

    with pytest.raises(OSError):
        ltw_mod.main(argv=["--watchdog"])
    assert len(calls) == len(ltw_mod._engine_creationflags_tiers()), (
        "每一级都必须被尝试过，才能断定全链失败"
    )


def test_spawn_enoent_propagates_immediately(tmp_path, monkeypatch):
    """跨平台：ENOENT（解释器/脚本缺失）不是标志问题 → 立刻抛出，不做无谓重试。

    降级重试的前提是「换组标志有可能成功」。文件不存在时三组都会以同样方式失败，
    重试只会刷三条一模一样的告警、把真因淹没。
    """
    calls = []

    class _FakeProc:
        returncode = 0
        pid = 12345

        def __init__(self, cmd, **kw):
            calls.append(kw)
            raise OSError(errno.ENOENT, "没有那个文件或目录（模拟解释器缺失）")

        def poll(self):
            return 0

    monkeypatch.setattr(ltw_mod.subprocess, "Popen", _FakeProc)
    _prepare_detached(monkeypatch, tmp_path)

    with pytest.raises(OSError) as excinfo:
        ltw_mod.main(argv=["--watchdog"])
    assert excinfo.value.errno == errno.ENOENT
    assert len(calls) == 1, "ENOENT 属确定性失败，重试无意义（只应尝试一次）"


def test_slow_probe_does_not_force_launch_when_instance_running(tmp_path, monkeypatch, capsys):
    """P3-3 R22 的**另一半**：慢预检 + 预检报「有存活实例」→ 照旧跳过拉起。

    已有用例只钉了「慢告警不许拦下拉起」（``calls`` 非空）。本用例钉反向：
    慢告警同样**不许逼着拉起** —— 告警分支只许 print，两个方向都不能越界。
    （用假时钟而非真 sleep，避免给全量套件再加 2.5s。）
    """
    calls = []
    _install_fake_popen(monkeypatch, calls)
    _prepare(monkeypatch, tmp_path)

    ticks = iter([0.0, 5.0])  # 首次 0.0，第二次 5.0 → probe_elapsed=5.0 > 2.0

    def _fake_perf_counter() -> float:
        return next(ticks, 5.0)

    monkeypatch.setattr(ltw_mod.time, "perf_counter", _fake_perf_counter)
    monkeypatch.setattr(ltw_mod, "_engine_instance_running", lambda *a, **k: True)

    rc = ltw_mod.main(argv=["--watchdog"])

    assert rc == 0
    assert calls == [], "预检报有存活实例 → 慢预检告警绝不能变成强行拉起"
    out = capsys.readouterr().out
    assert "[LAUNCH][WARN] 单实例预检耗时" in out, "慢预检告警必须照常打印"
