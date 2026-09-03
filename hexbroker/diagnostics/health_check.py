"""系统健康检查模块（启动前运行，输出日志报告）。

报告三大块（对应需求）：

1. 数据源网络连接
   - 实时行情源（新浪 ``hq.sinajs.cn``）
   - K 线兜底源（新浪 ``stock2.finance.sina.com.cn``）
   - 信号缓存（本地 parquet，多源级联）
2. 各模块就位状态 + 已启动实例检测
   - 逐项构建组件（导入 + 实例化），互不阻断
   - 检测是否已有运行实例（PID 锁文件）
3. 系统生命周期参数
   - 引擎 Tick 轮询 / 情报(信号监测)间隔 / 账户快照间隔 / 开盘延迟 /
     收盘复盘缓冲 / 评估周期 / 技术兜底 K 线 / 品种模式 / 节假日 / 信号新鲜度

用法（由 ``paper_trading_main.py --health-check`` 调用，或由 bat 先跑）::

    from hexbroker.diagnostics.health_check import run_health_check
    report = run_health_check(paper_cfg, offline=False)
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------
# status 取值：OK / WARN / FAIL / SKIP / INFO
_STATUS_MARK = {
    "OK": "[ OK ]",
    "WARN": "[WARN]",
    "FAIL": "[FAIL]",
    "SKIP": "[SKIP]",
    "INFO": "[INFO]",
}


class CheckItem:
    """单条检查结果。"""

    def __init__(self, status: str, name: str, detail: str = "") -> None:
        self.status = status
        self.name = name
        self.detail = detail

    def as_line(self) -> str:
        mark = _STATUS_MARK.get(self.status, f"[{self.status}]")
        if self.detail:
            return f"  {mark} {self.name}\n         {self.detail}"
        return f"  {mark} {self.name}"


def _is_pid_alive(pid: int) -> bool:
    """跨平台检测进程是否存活。

    - POSIX：``os.kill(pid, 0)``。
    - Windows：``os.kill(pid, 0)`` 会抛 ``ValueError``，故改用 ctypes 调
      ``OpenProcess`` + ``GetExitCodeProcess``（STILL_ACTIVE=259 视为存活）。
    """
    if sys.platform.startswith("win"):
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_INFORMATION = 0x0400
    STILL_ACTIVE = 259
    process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        # P1-C 取证修复（2026-09-03）：打不开 ≠ 已死。ACCESS_DENIED（无权限，
        # 典型：不同 token/session 调起的进程）→ 保守按「无法排除存活」处理
        # （返回 True）。09-01 多进程并存事故的假阴性出口正是这里把无权限判死。
        # 其余错误码（如进程不存在的 ERROR_INVALID_PARAMETER）维持判死。
        return kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(process)


def _win_pid_creation_time(pid: int) -> Optional[int]:
    """返回进程创建时间（Windows FILETIME 64-bit）；进程不存在/不可访问返回 None。

    R26g（P1-C 方案①）后语义：内容指纹式互斥已退役，本函数现仅用于
    ``_acquire_instance_lock`` 落盘 ``PID:CREATION_TIME`` **取证内容**的生成
    （与诊断层消费），不再参与任何互斥判定——互斥由 OS 句柄锁仲裁。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_INFORMATION = 0x0400
    process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        return None
    try:
        ct = wintypes.FILETIME()
        et = wintypes.FILETIME()
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            process, ctypes.byref(ct), ctypes.byref(et),
            ctypes.byref(kt), ctypes.byref(ut),
        ):
            return None
        return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
    finally:
        kernel32.CloseHandle(process)


def _pid_creation_time(pid: int) -> Optional[int]:
    """跨平台进程创建时间指纹（R26g 后仅作取证内容生成，不参与互斥判定）。

    - Windows：``GetProcessTimes`` 的 creation FILETIME。
    - POSIX：``/proc/<pid>/stat`` 第 22 字段（starttime，单位时钟滴答，单调可比）。
    """
    if sys.platform.startswith("win"):
        return _win_pid_creation_time(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            parts = f.read().split()
        return int(parts[21])  # starttime 字段（0-based index 21）
    except Exception:
        return None


def _read_pid_file(pid_path: Path) -> Optional[int]:
    try:
        if not pid_path.exists():
            return None
        text = pid_path.read_text(encoding="utf-8").strip()
        # 兼容新格式 ``PID:CREATION_TIME``（见 paper_trading_main._acquire_instance_lock）
        pid = int(text.split(":")[0])
    except Exception:
        return None
    if _is_pid_alive(pid):
        return pid
    # P1-C（2026-09-03）：锁文件改为**常驻**（句柄锁语义，内容=最后持有者诊断
    # 记录），死亡 PID 不再清理删除——「删除→重建」空窗是句柄锁 TOCTOU 回归点。
    return None


# ---------------------------------------------------------------------------
# 1. 数据源网络连接
# ---------------------------------------------------------------------------
def _probe_http(url: str, headers: dict[str, str], timeout: float) -> tuple[bool, str]:
    """轻量 HTTP 探测：返回 (可达, 描述)。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read(4096)  # 只读少量，确认连通
            ms = (time.time() - t0) * 1000.0
            return True, f"HTTP {resp.status}, 延迟 {ms:.0f}ms"
    except Exception as exc:  # 网络/超时/DNS
        ms = (time.time() - t0) * 1000.0
        return False, f"{type(exc).__name__}: {exc} (耗时 {ms:.0f}ms)"


def signal_freshness_days(latest_ts: Any, asof: Any = None) -> Optional[int]:
    """信号缓存最新时间戳距 ``asof``（默认今日）的**交易日历 lag**（P0-3 新鲜度自检）。

    与 ``hexbroker.paper.signals._trading_lag`` 同口径（P3-C，2026-08-31）：
    ``lag = idx(上一交易日) - idx(信号日)``，``0`` = 覆盖最近一个已收盘交易日（标准 T+1）。
    直接复用该实现以避免两处逻辑漂移。

    ⛔ 口径两度变更：P0-3 用工作日差、P3-B 改用自然日差（误伤周一/假期后首日 21.36%），
    现为交易日历 lag。详情见 ``scripts/verify_freshness_caliber_options.py --full-history``。

    ⚠️ ``asof`` 缺省取 ``datetime.now()``（**含时刻**）而非 ``date.today()``：
    时刻决定「当日日盘是否已收盘」，进而决定 ref 取当日还是前一交易日。
    夜盘自检若用纯日期会被判为未收盘，从而漏报「20:30 刷新没跑」这种落后一整个
    已收盘交易日的情形（P0-3 事故口径）。测试可注入纯 ``date`` 以固定语义。

    无法解析 / 日历不可用 → 返回 ``None``（调用方跳过检查，不阻断启动）。
    """
    if latest_ts is None:
        return None
    try:
        from ..paper.signals import (
            _to_date,
            _trading_lag,
            augment_calendar,
            load_trading_calendar,
        )

        sig_day = _to_date(latest_ts)
        if sig_day is None:
            return None
        ref = asof if asof is not None else datetime.now()
        # ⛔ 必须增补日历：主湖日线次日才补数，夜盘时刻主湖恒无当日 → R 无法命中
        #    当日 → lag=None（无法判定）。「今天是不是交易日」是独立知识，
        #    不该由「数据有没有补进来」回答。增补后给出可执行的确定值（落后 N 日）。
        cal = augment_calendar(load_trading_calendar(), _to_date(ref))
        lag = _trading_lag(sig_day, ref, cal)
        return None if lag is None else int(lag)
    except Exception:
        return None


def check_data_sources(
    paper_cfg: Any,
    offline: bool = False,
    timeout: float = 8.0,
    asof: Any = None,
) -> list[CheckItem]:
    """数据源连通性 + 信号缓存存在性/新鲜度检查（``asof`` 仅供测试注入基准日）。"""
    items: list[CheckItem] = []

    # 1a. 实时行情源（hq.sinajs.cn）
    quote_url = str(paper_cfg.get("quote_url", "https://hq.sinajs.cn/list="))
    if offline:
        items.append(CheckItem("SKIP", "实时行情源 (hq.sinajs.cn)", "离线模式，跳过网络探测"))
    else:
        url = f"{quote_url}nf_AG0"
        ok, detail = _probe_http(
            url,
            headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        items.append(
            CheckItem("OK" if ok else "FAIL", "实时行情源 (hq.sinajs.cn)", detail)
        )

    # 1b. K 线兜底源（stock2.finance.sina.com.cn）
    if offline:
        items.append(CheckItem("SKIP", "K线兜底源 (stock2.finance.sina.com.cn)", "离线模式，跳过网络探测"))
    else:
        bars_url = (
            "http://stock2.finance.sina.com.cn/futures/api/json.php/"
            "IndexService.getInnerFuturesDailyKLine?symbol=AG0"
            "&startDate=2026-08-01&endDate=2026-08-24"
        )
        ok, detail = _probe_http(
            bars_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            timeout=timeout,
        )
        items.append(
            CheckItem("OK" if ok else "FAIL", "K线兜底源 (stock2.finance.sina.com.cn)", detail)
        )

    # 1c. 信号缓存（本地 parquet，多源级联）+ P0-3 新鲜度防护（陈旧 → WARN，不阻断）
    caches = paper_cfg.get("signal_caches") or [paper_cfg.get("signal_cache")]
    # P3-C：口径为「交易日 lag」（非自然日差），与 SignalEngine 同源
    threshold = int(paper_cfg.get("freshness_threshold_trading_days", 0) or 0)
    if not caches:
        items.append(CheckItem("WARN", "信号缓存", "未配置 signal_caches / signal_cache"))
    for cache in caches:
        if cache is None:
            continue
        path = Path(cache)
        if not path.exists():
            items.append(CheckItem("FAIL", f"信号缓存: {path.name}", f"文件缺失：{path}"))
            continue
        try:
            import pandas as pd

            df = pd.read_parquet(path)
            n = len(df)
            latest = None
            latest_ts = None
            if "ts" in df.columns and len(df):
                latest_ts = pd.to_datetime(df["ts"]).max()
                latest = latest_ts.strftime("%Y-%m-%d %H:%M")
            syms = sorted(set(df["symbol"].tolist())) if "symbol" in df.columns else []
            fd = signal_freshness_days(latest_ts, asof)
            if fd is not None and fd > threshold:
                # 陈旧缓存（交易日 lag > 阈值）→ 主源信号不驱动开仓（技术兜底接手）
                items.append(
                    CheckItem(
                        "WARN",
                        f"信号缓存: {path.name}",
                        f"信号陈旧 fd={fd}>阈值{threshold}，最新{latest}，"
                        f"建议开盘前刷新 (p22_tail_ext.py --skip-eval)",
                    )
                )
            else:
                fd_text = f", 新鲜度 fd={fd}<=阈值{threshold}" if fd is not None else ""
                items.append(
                    CheckItem(
                        "OK",
                        f"信号缓存: {path.name}",
                        f"存在, {n} 行, 品种 {syms}, 最新 {latest}{fd_text}",
                    )
                )
        except Exception as exc:
            items.append(CheckItem("FAIL", f"信号缓存: {path.name}", f"读取失败：{exc}"))

    return items


# ---------------------------------------------------------------------------
# 2. 各模块就位 + 已启动实例
# ---------------------------------------------------------------------------
# 组件键 -> 展示名
_MODULE_DISPLAY = [
    ("session", "会话/时段管理 (TradingSession)"),
    ("quotes", "实时行情客户端 (RealTimeQuoteClient)"),
    ("signals", "信号引擎 (SignalEngine)"),
    ("risk_gate", "风控闸门 (RiskGate)"),
    ("broker", "券商/资金账户 (PaperBroker)"),
    ("planner", "交易计划管理 (PlanManager)"),
    ("intel", "情报服务 (IntelligenceService)"),
    ("logger", "交易日志 (TradeLogger)"),
    ("reporter", "复盘报告 (ReviewReporter)"),
]


def check_modules(paper_cfg: Any, offline: bool = False) -> tuple[list[CheckItem], Optional[int]]:
    """逐项构建组件检测就位状态；返回 (检查项, 运行实例PID)。"""
    from paper_trading_main import build_components_safe

    safe = build_components_safe(paper_cfg, offline=offline)
    items: list[CheckItem] = []
    for key, display in _MODULE_DISPLAY:
        ok, val = safe.get(key, (False, RuntimeError("未知组件")))
        if ok:
            items.append(CheckItem("OK", display, ""))
        else:
            items.append(CheckItem("FAIL", display, f"{type(val).__name__}: {val}"))

    # 运行实例检测（PID 锁）
    pid_path = Path(paper_cfg.get("data_dir", "data/paper")) / "paper.pid"
    running_pid = _read_pid_file(pid_path)
    if running_pid is not None:
        items.append(
            CheckItem(
                "INFO",
                "运行实例",
                f"检测到 PID {running_pid}，调度器与全部模块已在运行（健康检查为独立只读探测）",
            )
        )
    else:
        started = " / ".join(d.split(" (")[0] for k, d in _MODULE_DISPLAY)
        items.append(
            CheckItem("INFO", "运行实例", f"无运行实例（本次为健康检查；启动后将拉起：{started}）")
        )

    return items, running_pid


# ---------------------------------------------------------------------------
# 3. 系统生命周期参数
# ---------------------------------------------------------------------------
def check_lifecycle(paper_cfg: Any) -> list[CheckItem]:
    items: list[CheckItem] = []

    poll = paper_cfg.get("poll_interval_sec", 60)
    intel = paper_cfg.get("intel_interval_sec", 1800)
    snap = paper_cfg.get("snapshot_interval_sec", 300)
    open_delay = paper_cfg.get("open_delay_min", 5)
    close_buf = paper_cfg.get("close_buffer_min", 10)
    eval_days = paper_cfg.get("evaluation_days", 20)
    fresh = paper_cfg.get("freshness_threshold_trading_days", 0)
    bar_freq = paper_cfg.get("bar_freq", "1d")
    bar_days = paper_cfg.get("bar_days", 120)

    items.append(CheckItem("INFO", "引擎 Tick 轮询间隔", f"{poll}s（主循环每次 tick 走完整管道）"))
    items.append(CheckItem("INFO", "情报/信号监测间隔", f"{intel}s（约 {intel/60:.0f}min 轮询情报+风险提示）"))
    items.append(CheckItem("INFO", "账户快照落盘间隔", f"{snap}s（约 {snap/60:.0f}min 一次）"))
    items.append(CheckItem("INFO", "开盘延迟", f"{open_delay}min（跳过集合竞价，Q6）"))
    items.append(CheckItem("INFO", "收盘复盘缓冲", f"{close_buf}min（日盘收盘 + 缓冲后触发复盘，P1-3）"))
    items.append(CheckItem("INFO", "评估周期", f"满 {eval_days} 个交易日自动输出评估摘要（Q5）"))
    # P3-C（2026-08-31）：口径为「交易日 lag」，阈值 0 = 标准 T+1（正常值），≥1 = 放宽。
    # ⛔ 注意与两代旧口径的语义相反：P0-3 的 0 是病态值（自然日差下盘中不可达 → 永不主源开仓），
    #    本口径的 0 才是正确生产值。文案须明确区分，避免后人照旧注释误改。
    fresh_val = int(fresh or 0)
    if fresh_val <= 0:
        fresh_extra = "，0=信号须覆盖最近一个已收盘交易日（标准 T+1，周一用周五信号/假期后首日用节前信号均放行）"
    else:
        fresh_extra = f"，{fresh_val}=放宽至落后 {fresh_val} 个交易日仍放行"
    items.append(
        CheckItem(
            "INFO",
            "信号新鲜度阈值",
            f"lag<={fresh_val} 交易日（过期→技术兜底/禁开+告警，§8.2）{fresh_extra}",
        )
    )
    items.append(CheckItem("INFO", "技术兜底 K线", f"{bar_freq} / 近 {bar_days} 天（信号缺口时双均线+ATR 通道）"))

    # 品种与模式
    symbols = list(paper_cfg.symbols.keys())
    for sym in symbols:
        cfg = paper_cfg.symbols[sym]
        mode = cfg.get("mode", "trade")
        extra = ""
        if mode == "accumulate":
            acc = cfg.get("accumulate_days", 30)
            extra = f"（accumulate→满 {acc} 交易日切 trade）"
        items.append(CheckItem("INFO", f"品种 {sym} 模式", f"{mode}{extra}"))

    # 节假日
    holidays = list(paper_cfg.get("holidays_2026", []))
    items.append(CheckItem("INFO", "2026 节假日表", f"{len(holidays)} 个休市日（以交易所公告为准）"))

    return items


# ---------------------------------------------------------------------------
# 汇总与输出
# ---------------------------------------------------------------------------
def _format_report(
    data_items: list[CheckItem],
    module_items: list[CheckItem],
    lifecycle_items: list[CheckItem],
) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("HexBroker 模拟盘 · 系统健康检查报告")
    lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 72)

    lines.append("")
    lines.append("【一】数据源网络连接")
    for it in data_items:
        lines.append(it.as_line())

    lines.append("")
    lines.append("【二】各模块就位状态 / 已启动实例")
    for it in module_items:
        lines.append(it.as_line())

    lines.append("")
    lines.append("【三】系统生命周期参数")
    for it in lifecycle_items:
        lines.append(it.as_line())

    # 汇总计数
    def _count(items: list[CheckItem], status: str) -> int:
        return sum(1 for it in items if it.status == status)

    lines.append("")
    lines.append("-" * 72)
    lines.append(
        f"汇总: 数据源 {_count(data_items,'OK')}OK/{_count(data_items,'FAIL')}FAIL/"
        f"{_count(data_items,'SKIP')}SKIP | "
        f"模块 {_count(module_items,'OK')}OK/{_count(module_items,'FAIL')}FAIL"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


def run_health_check(paper_cfg: Any, offline: bool = False, timeout: float = 8.0) -> dict:
    """运行全部健康检查，打印并落盘日志报告，返回结构化结果。"""
    data_items = check_data_sources(paper_cfg, offline=offline, timeout=timeout)
    module_items, running_pid = check_modules(paper_cfg, offline=offline)
    lifecycle_items = check_lifecycle(paper_cfg)

    report_text = _format_report(data_items, module_items, lifecycle_items)
    print(report_text)

    # 落盘到 logs/health_check.log（覆盖式，便于留存报告）
    try:
        log_path = Path("logs") / "health_check.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(report_text + "\n", encoding="utf-8")
    except Exception:
        pass

    return {
        "data_sources": [(it.status, it.name, it.detail) for it in data_items],
        "modules": [(it.status, it.name, it.detail) for it in module_items],
        "lifecycle": [(it.status, it.name, it.detail) for it in lifecycle_items],
        "running_pid": running_pid,
    }
