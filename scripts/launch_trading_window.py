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

⛔ P1-1 单实例预检（2026-09-06，仅 --watchdog 路径）：引擎调度器没有「时段结束
即退出」，会活满全天；撞单实例锁时引擎 ``rc=1``，而看门狗把 rc≠0 且
runtime<stable_sec 判成崩溃 → 后两个时段再拉 --watchdog 会反复退避重启，
约 20 分钟后刷一条**假** ``[CRITICAL] 连续崩溃 10 次``，淹没真异常。故拉起前
先用**与引擎完全相同的 OS 字节锁**探测：已有存活实例则跳过本次拉起（rc=0，
非错误）；探测失败则 fail-open 照常拉起。不带 --watchdog 时不做预检，
默认路径行为零变更（撞锁的引擎直接退出，本来良性）。

⛔ 锁常量同源（2026-09-06，team-lead 点名）：预检要抢的字节偏移**不在本文件
另写字面量**，而是运行时引用引擎模块的 ``_LOCK_OFFSET``（见 ``_engine_lock_offset``）。
否则将来谁改了一边，预检只会**静默失效**，最坏退化成「把空闲误判成占用 →
系统永远拉不起来」的全停模式。锁定机制本身由互操作测试钉死。

仅做启动，不修改任何生产代码。
"""
import argparse
import errno
import io
import os
import subprocess
import sys
import time

# ⛔ 可移植性（CI 2026-09-06 实证修复）：ROOT 不得硬编码绝对路径——写死 Windows
# 盘符后，脚本被搬到任何其它位置（换盘/迁移/CI checkout）时 MAIN/WATCHDOG/
# CONSOLE_LOG/PID_FILE/PYTHONPATH/cwd 全部静默失效。改为从本文件位置推导
# （scripts/ 的上级 = 仓库根）。本机 E:\Workspace\HexBroker 部署时推导结果与旧
# 硬编码逐字符一致 → 生产行为零变更（test_pid_file_matches_engine_data_dir 以
# 真实 configs/paper.yaml 逐值兜底，本地绿即证明推导正确）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 引擎解释器：本机托管 venv 的 python。⚠️ 仍为本机绝对路径——launcher 只在本机
# 生产运行；迁移部署时需同步修改此行（CI 测试断言 cmd[0]==PY 自比照，不依赖具体值）。
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
# 单实例锁文件（与 paper_trading_main 的 ``data_dir / paper.pid`` 同一路径/同一语义）
# ⚠️ 必须与 configs/paper.yaml::paper.data_dir 保持一致，否则预检永远探测不到锁 →
# 静默退化回 P1-1（假 CRITICAL）。由 test_pid_file_matches_engine_data_dir 兜底。
PID_FILE = os.path.join(ROOT, "data", "paper", "paper.pid")
# ⛔ 锁区偏移**不在本文件另写一份字面量**——见 _engine_lock_offset()：运行时直接
# 引用引擎模块的 _LOCK_OFFSET，使"两边各写一份、将来漂移"物理上不可能发生。

# P3-3 慢预检告警阈值（秒）：非阻塞预检实测 ~0.01s，阻塞锁退化实测 ~9s，
# 差 3 个数量级 → 2.0s 留足余量又不会漏报。只用于打印 WARN，不影响控制流。
SLOW_PROBE_WARN_SEC = 2.0


def _engine_creationflags() -> int:
    """引擎子进程创建标志：独立进程组 + 无窗口。"""
    return CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _open_console_log() -> "io.TextIOWrapper":
    log_dir = os.path.dirname(CONSOLE_LOG)
    os.makedirs(log_dir, exist_ok=True)
    # 行缓冲，保证崩溃时也已落盘
    return open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)


def _load_engine_module():
    """按文件路径加载引擎模块 ``scripts/paper_trading_main.py``（同目录非包脚本）。

    ⛔ 为什么要 import 引擎、而不是在这里另抄一份锁常量：预检必须与引擎抢
    **同一个字节**。若两边各写一份 ``_LOCK_OFFSET``，将来谁改了一边，预检
    **不会报错，只会静默失效**——最坏情况是「把空闲误判成占用 → 系统永远
    拉不起来」，这正是项目铁律最忌讳的**全停**失效模式。同源引用让漂移
    不可能发生：引擎改了偏移，预检自动跟着走。

    加载失败（脚本被移动/重命名）→ 返回 None，调用方按 fail-open 处理。
    引擎模块顶层只有标准库导入，无副作用，加载开销可忽略。
    """
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_hb_engine_for_lock_probe", MAIN
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001
        return None


def _engine_lock_offset() -> "int | None":
    """取引擎侧单实例锁的字节偏移（**同源引用**，取不到返回 None）。

    返回 None 时调用方必须 **fail-open**——宁可不做预检，也绝不能自己猜一个
    字节偏移去加锁：猜错等于静默失效，比不做预检更危险。
    """
    mod = _load_engine_module()
    if mod is None:
        return None
    try:
        return int(mod._LOCK_OFFSET)
    except Exception:  # noqa: BLE001
        return None


def _lock_busy_errnos() -> "set[int]":
    """「锁被占用」对应的 errno 白名单——**实测**得出，不靠文档猜。

    实测（win32 / CPython 3.13，见 artifacts/_tmp/_R4_errno.txt）：
    - ``msvcrt.locking(LK_NBLCK)`` 撞已锁区 → ``PermissionError errno=13 (EACCES)``
      （**不是** EDEADLOCK——后者是 ``_LK_LOCK``/``_LK_RLCK`` 重试耗尽才给）；
    - 非占用类失败实测为 ``EBADF(9)``（坏 fd）与 ``EINVAL(22)``（参数非法），
      这两类**必须**走 fail-open，绝不能判成「有存活实例」。

    ⛔ 为什么必须窄化：``except Exception: return True`` 会把**所有**异常都判成
    「有存活实例」→ 启动器直接跳过拉起 → **系统永远起不来，而自动化只会安静
    回报「已有实例跳过」，日志上看不出任何异常**。这正是 R22 要防的形态——
    探测失败绝不能变成「永远拉不起」的新停摆模式。

    关于 EACCES 的两义性：它也可能是真实 ACL 拒绝。但本函数的 EACCES 来自
    ``msvcrt.locking``/``flock`` 调用，而此前 ``os.open(..., O_RDWR)`` 已经成功
    —— 既然本进程已持有可读可写句柄，此时的 EACCES 几乎只可能是锁冲突。
    ``os.open`` 自身的权限失败在上游已单独判为 fail-open（返回 None）。
    """

    def _collect(names: "tuple[str, ...]") -> "set[int]":
        out: set[int] = set()
        for name in names:
            code = getattr(errno, name, None)
            if isinstance(code, int):  # 某些平台没有该常量 → 安全跳过
                out.add(code)
        return out

    if sys.platform.startswith("win"):
        # Windows：锁冲突 = EACCES（实测）；EDEADLOCK/EDEADLK 是 MS 文档列出的
        # 另一种锁语义 errno，一并纳入只会把更多"真被占用"判对。
        return _collect(("EACCES", "EDEADLOCK", "EDEADLK"))
    # POSIX：flock(LOCK_EX|LOCK_NB) 撞已锁 → EWOULDBLOCK/EAGAIN；EACCES 兜底。
    return _collect(("EWOULDBLOCK", "EAGAIN", "EACCES"))


_LOCK_BUSY_ERRNOS = _lock_busy_errnos()


def _engine_instance_running(
    pid_path: str = PID_FILE, lock_offset: "int | None" = None
) -> "bool | None":
    """探测是否已有**存活**的交易引擎实例（复用引擎同款 OS 字节锁语义）。

    ``lock_offset`` 为 None 时自动从引擎模块取同源值（生产路径）；显式传入
    则用于测试注入。

    ⛔ 不读 PID 文本判活：P1-C 取证（2026-09-03）已证明内容指纹式锁有两处
    fail-open（``_is_pid_alive`` 把 OpenProcess ACCESS_DENIED 误判为死、锁路径
    异常降级放行）→ 长寿旧进程与新进程并存。互斥必须由 OS 句柄仲裁：进程死亡
    时 OS 自动释放句柄，天然无僵尸锁。故本探测与引擎用**完全相同的
    ``msvcrt.locking(LK_NBLCK)`` / ``fcntl.flock(LOCK_EX|LOCK_NB)`` 抢同一字节**。

    返回：
    - ``True``  → 锁被占用，有存活实例（本进程**必须让位**）。⛔ 仅当抢锁
      异常的 errno 落在 ``_LOCK_BUSY_ERRNOS``（锁语义 errno，实测标定）内
      才判 True；其余一律 None，见下面「异常窄化」；
    - ``False`` → 抢到锁，无存活实例（本函数已立即释放，可安全拉起）；
    - ``None``  → 探测本身失败，**调用方按 fail-open 处理**（照常拉起，交由
      引擎自身锁仲裁）——R22：探测失败绝不能变成「永远拉不起」的新停摆模式。

    耦合契约（team-lead 2026-09-06 点名确认）：
    - **字节偏移**已同源化（``_engine_lock_offset()`` 直接读引擎模块的
      ``_LOCK_OFFSET``），漂移不可能发生；
    - **锁定机制**（msvcrt.locking / fcntl.flock）无法直接复用引擎的实现
      （它内联在 ``_acquire_instance_lock`` 里，且该函数会写 PID 内容、并把
      「被占用」与「探测失败」都返回 None，复用它会踩「零写入」红线并把
      fail-open 变成 fail-closed），故由 ``test_lock_probe_interops_with_engine_lock``
      做**双向互操作**断言钉死——任一边改了偏移或换锁定方式，该测试当场红。

    ⚠️ 抢到锁后**必须**在返回前释放并关闭句柄：否则本函数自己就占住了锁，
    引擎将永远无法启动。此处用 finally 兜底。

    释放是**双重冗余，不是单点依赖**（措辞订正 2026-09-06，QA 实测）：
    Windows 上 ``os.close(fd)`` 本身就会释放该 fd 上的 msvcrt 字节锁，
    显式 ``LK_UNLCK`` 并非唯一救命稻草。两者的关系是——显式 unlock 让释放点
    **明确、且不依赖 GC / 解释器退出时机**，close 则兜住 unlock 抛错或提前
    return 的路径。任一条单独成立都足以放锁，两条都留着才对得起
    「探测绝不能把锁占住」这条硬约束。（早先写成「灾难性故障面」属措辞过强，
    会让后来者误以为 unlock 是唯一依赖，故订正。）

    ⚠️ 另一条同样容易被悄悄改掉的前提：抢锁必须**非阻塞**（``LK_NBLCK`` /
    ``LOCK_NB``）。换成阻塞锁后**返回值语义完全不变**——阻塞重试耗尽抛出的
    EDEADLOCK 恰好落在白名单里，照样 return True——但每次预检会卡约 9 秒，
    而「已有实例存活」在 08:55 / 13:25 / 20:55 是**常态**不是异常。该前提
    由 ``test_lock_probe_returns_promptly_when_lock_held`` 用**时延断言**钉死，
    因为纯返回值断言抓不到它。

    异常窄化（2026-09-06 收口，team-lead 点名）：
    本函数内**三处**可失败点必须口径一致——``_engine_lock_offset()`` 取不到、
    ``os.open`` 失败、抢锁抛异常，前两处本就返回 None（fail-open），抢锁这一处
    原来写成 ``except Exception: return True``，把**所有**异常都当成「有存活
    实例」——这是遗漏、不是取舍。已改为按 errno 白名单判定。
    """
    if not os.path.exists(pid_path):
        return False  # 锁文件都不存在 → 必然无存活实例（也避免无谓创建文件）
    if lock_offset is None:
        lock_offset = _engine_lock_offset()
    if lock_offset is None:
        # 拿不到引擎同源偏移 → **绝不猜一个字节去锁**（猜错 = 静默失效）
        return None  # fail-open：交由引擎自身锁仲裁
    fd = -1
    acquired = False
    try:
        try:
            fd = os.open(pid_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return None  # fail-open：开不了文件 → 交给引擎自身锁仲裁
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                os.lseek(fd, lock_offset, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False  # 抢到锁 → 无存活实例
        except OSError as exc:
            # ⛔ 窄化（2026-09-06 收口）：只有「锁被占用」这一类 errno 才能判 True；
            # 其余（EBADF 坏 fd / EINVAL 参数非法 / EPERM 等）一律返回 None →
            # fail-open 照常拉起，交由引擎自身锁仲裁。
            # 反例（未窄化时的故障形态）：权限异常被判成「有存活实例」→ 启动器
            # 永远跳过拉起，而自动化只安静回报「已有实例跳过」，日志无任何异常。
            if exc.errno in _LOCK_BUSY_ERRNOS:
                return True  # 锁被占用 → 有存活实例（本进程必须让位）
            return None  # 非占用类失败 → fail-open（R22：绝不全停）
        except Exception:  # noqa: BLE001
            # 非 OSError（原则上不该发生）→ 同样**绝不**判成「被占用」
            return None
    finally:
        if fd >= 0:
            if acquired:
                try:
                    if sys.platform.startswith("win"):
                        import msvcrt

                        os.lseek(fd, lock_offset, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


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

    # P1-1（QA 2026-09-06 实证）：仅 --watchdog 路径做单实例预检。
    # 引擎调度器没有「时段结束即退出」，理论上活满全天；而撞单实例锁时引擎 rc=1，
    # 看门狗把 rc≠0 且 runtime<stable_sec 判成崩溃 → 13:25/20:55 新拉起的看门狗会
    # 反复退避重启，约 20 分钟后刷一条**假** [CRITICAL] 连续崩溃 10 次，淹没真异常
    # （真引擎仍由第一个看门狗守护、不中断交易，但告警可信度被毁）。
    # 不带 --watchdog 时撞锁的引擎直接退出、无人重启，本来良性 → 默认路径零变更。
    if args.watchdog:
        # P3-3 运行时慢预检告警：非阻塞预检实测 ~0.01s，退化成阻塞锁（LK_NBLCK
        # 被换成 LK_LOCK）实测 ~9s 且**返回值语义完全不变**——时延护栏
        # （test_lock_probe_returns_promptly_when_lock_held）只在跑测试时生效，
        # 三个时段无人值守，这里补一条运行时信号。
        # ⛔ R22：本告警**只打印，绝不改变控制流**——不许影响 running 判定、
        # 不许 return/raise。宁可少一道护栏，不可全停。
        probe_t0 = time.perf_counter()
        running = _engine_instance_running(PID_FILE)
        probe_elapsed = time.perf_counter() - probe_t0
        if probe_elapsed > SLOW_PROBE_WARN_SEC:
            print(
                f"[LAUNCH][WARN] 单实例预检耗时 {probe_elapsed:.2f}s，超过阈值 "
                f"{SLOW_PROBE_WARN_SEC:.1f}s —— 典型成因是抢锁退化成阻塞锁"
                f"（LK_NBLCK 被换成 LK_LOCK，返回值语义不变），"
                f"请检查 _engine_instance_running() 的锁模式"
            )
        if running is True:
            print(
                f"[LAUNCH] 已有存活交易引擎实例（单实例锁 {PID_FILE}）——跳过本次拉起，"
                f"不重复挂看门狗（避免撞锁 rc=1 被误判崩溃 → 假 CRITICAL 告警）"
            )
            return 0
        if running is None:
            # 探测失败 → fail-open：照常拉起，由引擎自身锁仲裁
            print("[LAUNCH][WARN] 单实例预检失败（按无存活处理，交由引擎自身锁仲裁）")

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

    logf = None
    try:
        logf = _open_console_log()
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            creationflags=_engine_creationflags(),
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    finally:
        # P2-2 同款卫生：父进程这边的副本在 Popen 返回后即可关闭（句柄继承是
        # 复制语义，子进程持有自己的副本）。launcher 随即退出，OS 本来也会回收，
        # 但显式关掉让"不持有"成为契约而非侥幸。
        if logf is not None:
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass
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
