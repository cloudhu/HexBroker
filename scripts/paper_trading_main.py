"""模拟盘交易系统进程入口（T05 完整组装）。

用法::

    python scripts/paper_trading_main.py [--config configs/paper.yaml] [--days N] [--offline]

- ``--days N``：运行满 N 个交易日自动退出（默认不退出，长跑）。
- ``--offline``：离线 mock 行情模式（冒烟/演示，不访问网络）。

启动校验（A1）：配置加载 / 信号缓存存在 / 品种配置合法 / 节假日表加载，
失败输出中文错误并 exit(1)。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _print_error(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)


def _try_acquire_pid_lock(pid_path: Path) -> bool:
    """尝试获取 PID 锁（启动互斥，根除多实例并发导致 trades.log 会话重放 3× 伪增）。

    返回 True：成功取得锁（已写入本进程 PID + 创建时间指纹），或锁文件不可写（降级，不阻塞启动）。
    返回 False：检测到「同一进程」仍存活的实例，调用方应拒绝启动（exit 1）。

    加固（PID 复用防御）：锁文件格式由纯 PID 升级为 ``PID:CREATION_TIME``（创建时间
    FILETIME，跨平台可比）。读锁时若 PID 存活但创建时间不符 → 判定为僵尸锁（PID 被
    无关进程复用）→ 覆盖而非拒启，避免误判存活导致模拟盘无法启动。旧格式（纯整型）
    维持原「存活即拒绝」语义，向后兼容。
    """
    from hexbroker.diagnostics.health_check import _is_pid_alive, _pid_creation_time

    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        if pid_path.exists():
            raw = pid_path.read_text(encoding="utf-8").strip()
            old_pid: Optional[int] = None
            old_ct: Optional[int] = None
            if ":" in raw:
                try:
                    _p, _c = raw.split(":", 1)
                    old_pid = int(_p)
                    old_ct = int(_c)
                except Exception:
                    old_pid, old_ct = None, None
            else:
                try:
                    old_pid = int(raw)
                except Exception:
                    old_pid = None
            if old_pid is not None and _is_pid_alive(old_pid):
                if old_ct is None:
                    # 旧格式（无时间指纹）：维持原始「存活即拒绝」行为，不引入新风险
                    return False
                cur_ct = _pid_creation_time(old_pid)
                if cur_ct is None:
                    # 无法读取创建时间 → 保守拒绝（与原始「存活即拒绝」一致，防双开）
                    return False
                if cur_ct == old_ct:
                    return False  # 同一进程仍存活 → 拒绝重复启动
                # 否则：PID 复用（僵尸锁）→ 落入覆盖分支
            # 僵尸 PID / PID 复用 / 无锁文件 → 覆盖
        my_ct = _pid_creation_time(os.getpid())
        pid_path.write_text(
            f"{os.getpid()}:{my_ct}" if my_ct is not None else str(os.getpid()),
            encoding="utf-8",
        )
        return True
    except Exception:
        # 锁文件不可写：降级（仅健康检查「已启动实例」检测缺失），不阻塞启动
        return True


def _max_position_pct(paper_cfg: Any) -> float:
    """从风控配置读 ``max_position_pct``（P0-4 名义敞口硬顶，**仅用于告警**）。

    放在模块级而非 lambda 内：OmegaConf 只在 ``_load_paper_config`` 局部导入，
    直接在 lambda 里引用会 NameError。读取失败时保守回退 0.30（与 risk 配置同默认）。
    """
    try:
        from omegaconf import OmegaConf

        path = ROOT / str(paper_cfg.get("risk_config", "configs/risk/v4_atr.yaml"))
        return float(OmegaConf.load(path).get("max_position_pct", 0.30))
    except Exception:
        return 0.30


def _load_paper_config(config_path: str) -> Any:
    """加载 paper.yaml（经 hexbroker.config.load_config 合并默认配置）。"""
    from omegaconf import OmegaConf

    from hexbroker.config import load_config

    if not Path(config_path).exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")
    full = load_config(config_path)
    paper_dict = getattr(full, "paper", None)
    if paper_dict is None:
        raise ValueError("配置缺少 paper 段（请检查 configs/paper.yaml）")
    return OmegaConf.create(paper_dict)


def _run_governance_selfcheck() -> None:
    """P2-4 启动期治理自检（防误开联锁落地）：仅 WARNING + 强制 SHADOW，零侵入 tick。

    fail-safe：任何异常（配置缺失/解析失败/校验不通过）均降级为 WARNING 日志，
    绝不抛异常、绝不阻断交易启动、绝不进入主循环。
    """
    try:
        from hexbroker.governance import ResolvedMode, SchemeRegistry, resolve_scheme_mode

        cfg_path = "configs/scheme_governance.yaml"
        if not Path(cfg_path).exists():
            print(f"[GOV] 治理配置缺失（{cfg_path}），跳过自检（不阻断启动）")
            return
        reg = SchemeRegistry.load(cfg_path)  # 内部 verify_all：PASS 须有校准记录
        live, shadow = [], []
        for sid in reg.ids():
            mode, reason = resolve_scheme_mode("live", sid, reg)
            if mode is ResolvedMode.LIVE:
                live.append(sid)
            else:
                shadow.append(sid)
                if reason:
                    print(f"[GOV][警告] 方案 {sid} 未过治理联锁({reason})，强制 SHADOW_ONLY")
        print(f"[GOV] 治理自检完成：可实盘={live or '无'}，强制影子={shadow or '无'}")
    except Exception as exc:  # noqa: BLE001 — fail-safe：自检失败不阻断交易
        print(f"[GOV][警告] 治理自检异常（已忽略，不阻断启动）：{exc}")


def _validate(paper_cfg: Any, symbols: list[str]) -> None:
    """启动期致命校验：信号缓存存在 / 品种配置合法 / 节假日表加载。"""
    # 多信号源级联（signal_caches 列表优先；兼容旧单键 signal_cache）
    caches = paper_cfg.get("signal_caches") or [paper_cfg.get("signal_cache", "artifacts/signals_cache18_grouped_v8.parquet")]
    for cache in caches:
        cache = Path(cache)
        if not cache.exists():
            raise FileNotFoundError(f"信号缓存缺失：{cache}（请确认 artifacts/ 完整）")
    if not symbols:
        raise ValueError("paper.symbols 为空，至少配置一个品种")
    for sym in symbols:
        cfg = paper_cfg.symbols[sym]
        if not cfg.get("sessions"):
            raise ValueError(f"品种 {sym} 缺少 sessions 时段配置")
        for key in ("day", "night"):
            for slot in (cfg.sessions.get(key) or []):
                if len(slot) != 2:
                    raise ValueError(f"品种 {sym} 时段格式错误：{slot}")
    holidays = list(paper_cfg.get("holidays_2026", []))
    print(f"[模拟盘] 生效节假日表：{sorted(holidays)}（共 {len(holidays)} 个，以交易所公告为准）")


def build_components_safe(paper_cfg: Any, offline: bool = False) -> dict[str, tuple[bool, Any]]:
    """逐项构建组件，互不阻断；返回 ``{name: (ok, component_or_exception)}``。

    供系统健康检查（diagnostics.health_check）做模块就位检测；同时被
    :func:`build_components` 复用，保证主流程行为不变。
    """
    from hexbroker.paper.broker import PaperBroker, build_cost_model
    from hexbroker.paper.intel import IntelligenceService
    from hexbroker.paper.logger import TradeLogger
    from hexbroker.paper.planner import PlanManager
    from hexbroker.paper.quotes import RealTimeQuoteClient
    from hexbroker.paper.reporter import ReviewReporter
    from hexbroker.paper.risk_gate import RiskGate
    from hexbroker.paper.sessions import TradingSession
    from hexbroker.paper.signals import SignalEngine

    symbols = list(paper_cfg.symbols.keys())

    def _build_broker() -> PaperBroker:
        cost = build_cost_model(paper_cfg)
        return PaperBroker(
            cost=cost,
            initial_capital=float(paper_cfg.get("initial_capital", 100_000.0)),
            budget_ratio=float(paper_cfg.get("budget_ratio", 0.30)),
            data_dir=paper_cfg.get("data_dir", "data/paper"),
        )

    builders: dict[str, Callable[[], Any]] = {
        "session": lambda: TradingSession.from_config(paper_cfg),
        "quotes": lambda: RealTimeQuoteClient(
            symbols={s: paper_cfg.symbols[s].sina_code for s in symbols},
            url=paper_cfg.get("quote_url", "https://hq.sinajs.cn/list="),
            timeout=float(paper_cfg.get("quote_timeout_sec", 10)),
            offline=offline,
        ),
        "signals": lambda: SignalEngine(
            cache_paths=paper_cfg.get("signal_caches") or [paper_cfg.get("signal_cache")],
            # P3-C：缺省 0 = 信号须覆盖最近一个已收盘交易日（标准 T+1），与 configs/paper.yaml 一致
            freshness_threshold_trading_days=int(
                paper_cfg.get("freshness_threshold_trading_days", 0)
            ),
            **dict(paper_cfg.get("technical", {}) or {}),
        ),
        "risk_gate": lambda: RiskGate(
            risk_config=paper_cfg.get("risk_config", "configs/risk/v4_atr.yaml"),
            hard_stop=float(paper_cfg.get("risk_hard_stop", 0.20)),
            overrides=dict(paper_cfg.get("risk_overrides", {}) or {}),
            default_intent=float(paper_cfg.get("risk_default_intent", 0.30)),
            default_vol=float(paper_cfg.get("risk_default_vol", 0.02)),
            vol_quantile=float(paper_cfg.get("risk_vol_quantile", 0.5)),
        ),
        "broker": _build_broker,
        "planner": lambda: PlanManager(
            multipliers={s: float(paper_cfg.symbols[s].multiplier) for s in symbols},
            risk_reward_ratio=float(paper_cfg.get("risk_reward_ratio", 1.5)),
            plans_dir=paper_cfg.get("plans_dir", "trade_plans"),
            # ---- P0-4（2026-09-01）仓位粒度放大防护 ----
            size_by_risk=bool(paper_cfg.get("size_by_risk", False)),
            risk_per_trade=float(paper_cfg.get("risk_per_trade", 0.01)),
            risk_stop_atr_mult=float(paper_cfg.get("risk_stop_atr_mult", 2.5)),
            max_position_pct=_max_position_pct(paper_cfg),
        ),
        "intel": lambda: IntelligenceService.from_config(paper_cfg),
        "logger": lambda: TradeLogger(log_file=paper_cfg.get("trades_log", "data/paper/trades.log")),
        "reporter": lambda: ReviewReporter(
            reports_dir=paper_cfg.get("reports_dir", "deliverables"),
            symbols_display={s: paper_cfg.symbols[s].display for s in symbols},
        ),
    }

    results: dict[str, tuple[bool, Any]] = {}
    for name, fn in builders.items():
        try:
            results[name] = (True, fn())
        except Exception as exc:  # noqa: BLE001
            results[name] = (False, exc)
    return results


def _check_signal_freshness(paper_cfg: Any) -> None:
    """B+C 防再发：启动期信号新鲜度硬告警 + 可选自动刷新（默认关，绝不阻断启动）。

    背景 2026-08-28 停摆事故：阈值=0 的隔夜过期门禁要求每天刷新缓存，漏跑则
    「系统照常运行但不交易」。本函数在启动期把该状态变成**醒目横幅**；仅当配置
    ``signal_refresh.auto_enabled=true`` 时才自动跑 p22_tail_ext.py --skip-eval。
    """
    try:
        from hexbroker.diagnostics.signal_refresh import (
            format_banner,
            maybe_auto_refresh,
            probe_files,
            resolve_cache_paths,
        )

        sect = paper_cfg.get("signal_refresh", {}) if hasattr(paper_cfg, "get") else {}
        threshold = int(sect.get("threshold", 0))
        auto_enabled = bool(sect.get("auto_enabled", False))
        timeout_sec = int(sect.get("timeout_sec", 900))
        # D2 修复：按**显式文件列表**探测（与 SignalEngine 同口径）。
        # cache_paths 留空 → 继承 paper.signal_caches；旧键 cache_dir 已废弃
        # （目录不存在时 probe 返回空列表 → 恒"通过"的假绿，见 2026-08-28 缺陷）。
        paths = list(sect.get("cache_paths") or []) or resolve_cache_paths(paper_cfg)

        probes = probe_files(paths, threshold=threshold)
        banner = format_banner(probes)
        if not banner:
            print(f"[模拟盘] 信号缓存新鲜度检查通过（fd<={threshold}，共 {len(probes)} 个缓存）")
            return
        print(banner)
        refreshed, msg = maybe_auto_refresh(
            probes, enabled=auto_enabled, timeout_sec=timeout_sec
        )
        print(f"[模拟盘] 信号刷新：{msg}")
        if refreshed and not format_banner(probe_files(paths, threshold=threshold)):
            print("[模拟盘] 刷新后信号新鲜度已达标（可正常交易）")
    except Exception:  # noqa: BLE001
        print("[模拟盘] 信号新鲜度检查异常（已隔离，不阻断启动）")


def build_components(paper_cfg: Any, offline: bool = False) -> dict[str, Any]:
    """组装全部组件（§4.1 组件工厂）。任一组件失败则抛出首个异常。"""
    safe = build_components_safe(paper_cfg, offline=offline)
    comp: dict[str, Any] = {}
    first_err: Optional[Exception] = None
    for name in ("session", "quotes", "signals", "risk_gate", "broker", "planner", "intel", "logger", "reporter"):
        ok, val = safe[name]
        if ok:
            comp[name] = val
        elif first_err is None:
            first_err = val
    if first_err is not None:
        raise first_err
    return comp


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘交易系统")
    parser.add_argument("--config", default="configs/paper.yaml", help="paper.yaml 路径")
    parser.add_argument("--days", type=int, default=None, help="运行 N 个交易日后退出（默认不退出）")
    parser.add_argument("--offline", action="store_true", help="离线 mock 行情模式（冒烟/演示）")
    parser.add_argument("--smoke", action="store_true", help="冒烟：单轮 tick + 收盘复盘后即退出")
    parser.add_argument("--health-check", action="store_true", help="仅运行系统健康检查并打印报告后退出（不启动交易）")
    args = parser.parse_args()

    try:
        paper_cfg = _load_paper_config(args.config)
        symbols = list(paper_cfg.symbols.keys())
    except Exception as exc:
        _print_error(f"配置加载失败：{exc}")
        return 1

    # 系统健康检查（独立只读探测，不依赖启动校验）
    if args.health_check:
        from hexbroker.diagnostics.health_check import run_health_check

        run_health_check(paper_cfg, offline=args.offline)
        return 0

    try:
        _validate(paper_cfg, symbols)
    except Exception as exc:
        _print_error(f"启动校验失败：{exc}")
        return 1

    if args.smoke:
        # 冒烟模式：输出全部重定向到临时目录，避免污染仓库 data/ 与 deliverables/
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="paper_smoke_"))
        paper_cfg.data_dir = str(tmp / "data")
        paper_cfg.account_file = str(tmp / "data" / "account.json")
        paper_cfg.trades_log = str(tmp / "trades.log")
        paper_cfg.c0_daily_csv = str(tmp / "c0_daily.csv")
        paper_cfg.plans_dir = str(tmp / "plans")
        paper_cfg.reports_dir = str(tmp / "reports")
        paper_cfg.intel_static_file = str(tmp / "news_static.json")
        print(f"[模拟盘] 冒烟模式：运行产物写入 {tmp}")

    try:
        comp = build_components(paper_cfg, offline=args.offline)
    except Exception as exc:
        _print_error(f"组件初始化失败：{exc}")
        return 1

    if args.smoke:
        # 冒烟模式：注入合成有效信号，确保走到「下单」环节（信号桩）
        comp["signals"] = _SmokeSignals(comp["signals"])

    # 恢复账户快照（无则初始资金）
    broker = comp["broker"]
    account_file = Path(paper_cfg.get("account_file", "data/paper/account.json"))
    try:
        restored = broker.load_snapshot(account_file)
        if not restored:
            print(
                f"[模拟盘] 首次启动：初始资金 {float(paper_cfg.get('initial_capital', 100_000.0)):.0f} 元，"
                f"品种 {', '.join(symbols)}"
            )
    except Exception as exc:
        _print_error(f"账户快照加载失败：{exc}")
        return 1

    # 评估天数：--days 显式给出时与之对齐
    evaluation_days = int(paper_cfg.get("evaluation_days", 20))
    if args.days is not None:
        evaluation_days = min(evaluation_days, args.days)
    paper_cfg.evaluation_days = evaluation_days

    stop_event = threading.Event()

    def _on_signal(*_args: Any) -> None:
        print("[模拟盘] 收到退出信号，正在保存状态并退出…")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # P2-D：运行时降级器（默认关；enabled=false → None，零行为变更）
    degrader = None
    degrade_signals = None
    try:
        from hexbroker.governance import CalibrationLedger, RuntimeDegrader

        _gov_ledger = CalibrationLedger.load("data/governance/calibration_ledger.json")
        degrader = RuntimeDegrader.load_from_config(
            "configs/scheme_degrade.yaml", ledger=_gov_ledger
        )
        if degrader is not None:
            import yaml as _yaml

            _dcfg = _yaml.safe_load(
                Path("configs/scheme_degrade.yaml").read_text(encoding="utf-8")
            ) or {}
            degrade_signals = (
                str(_dcfg.get("signals_file", "data/governance/scheme_signals.json")),
                int(_dcfg.get("signal_max_age_sec", 900)),
            )
            print("[模拟盘] 治理运行时降级器已启用（仅降不升，事件留痕 ledger history）")
    except Exception:
        degrader, degrade_signals = None, None
        print("[模拟盘] 治理运行时降级器初始化失败（按未启用处理）")

    from hexbroker.paper.scheduler import TradingScheduler

    scheduler = TradingScheduler(
        cfg=paper_cfg,
        session=comp["session"],
        quotes=comp["quotes"],
        signals=comp["signals"],
        risk_gate=comp["risk_gate"],
        planner=comp["planner"],
        broker=broker,
        intel=comp["intel"],
        logger=comp["logger"],
        reporter=comp["reporter"],
        stop_event=stop_event,
        run_days=args.days,
        degrader=degrader,
        degrade_signals=degrade_signals,
    )

    if args.smoke:
        # 冒烟模式：驱动合成交易时段（2026-08-24 周一 10:00）跑完整管道，确保走到下单环节
        from datetime import datetime as _dt

        smoke_now = _dt(2026, 8, 24, 10, 0)
        day = comp["session"].day_label(smoke_now)
        print(f"[模拟盘] 冒烟模式：驱动交易日 {day} 10:00 交易时段…")
        try:
            quotes = comp["quotes"].fetch_quotes(symbols)
            marks = scheduler._build_marks(quotes)
            for sym in symbols:
                try:
                    scheduler._process_symbol(sym, smoke_now, quotes.get(sym), marks)
                except Exception as exc:  # noqa: BLE001
                    print(f"[错误] 冒烟品种 {sym} 处理失败：{exc}")
                    return 1
            scheduler._on_close(day)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 冒烟收盘复盘失败：{exc}")
            return 1
        # 校验产物
        trades_log = Path(paper_cfg.trades_log)
        trade_lines = [l for l in trades_log.read_text(encoding="utf-8").splitlines() if "TRADE|" in l] if trades_log.exists() else []
        reports = sorted(Path(paper_cfg.reports_dir).glob("复盘_*.md"))
        print("[模拟盘] 冒烟通过：管道（报价→信号→风控→计划→撮合→日志→复盘）已跑通")
        print(f"[模拟盘]   成交日志 {len(trade_lines)} 行；复盘报告 {len(reports)} 份（{Path(paper_cfg.reports_dir)}）")
        return 0

    # PID 锁：启动互斥（根除多实例并发导致 trades.log 会话重放 3× 伪增）
    # 写 PID 前先检测既有存活实例；存活则拒绝第二个实例（exit 1），避免 append 叠加。
    pid_path = Path(paper_cfg.get("data_dir", "data/paper")) / "paper.pid"
    if not _try_acquire_pid_lock(pid_path):
        print(f"[模拟盘] 已有存活实例（PID 锁 {pid_path}），拒绝重复启动以避免 trades.log 会话重放叠加。")
        print("[模拟盘]   如需强制重启，请先结束该实例或删除 PID 锁文件后重试。")
        return 1

    # P2-4 启动期治理自检（防误开联锁落地）：仅 WARNING + 强制 SHADOW，零侵入 tick
    _run_governance_selfcheck()

    # B+C 防再发：信号新鲜度硬告警（陈旧→醒目横幅；自动刷新默认关）
    _check_signal_freshness(paper_cfg)

    try:
        scheduler.run()
    finally:
        if pid_path is not None and pid_path.exists():
            try:
                pid_path.unlink()
            except OSError:
                pass
    return 0


class _SmokeSignals:
    """冒烟信号桩：始终返回有效信号，保证走到下单环节。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def latest_signal(self, symbol: str, asof: Any = None):
        from hexbroker.paper.types import SignalFrame

        ts = asof or datetime.now()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=0.66,
            exp_ret=0.3,
            is_effective=True,
            source="smoke",
            freshness_days=0,
        )

    def technical_fallback(self, symbol: str, bars: Any):
        return None

    def neutral_signal(self, symbol: str, asof: Any = None):
        return self._inner.neutral_signal(symbol, asof)


if __name__ == "__main__":
    sys.exit(main())
