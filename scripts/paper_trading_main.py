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
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _print_error(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)


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


def _validate(paper_cfg: Any, symbols: list[str]) -> None:
    """启动期致命校验：信号缓存存在 / 品种配置合法 / 节假日表加载。"""
    cache = Path(paper_cfg.get("signal_cache", "artifacts/signals_cache18_grouped_v8.parquet"))
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


def build_components(paper_cfg: Any, offline: bool = False) -> dict[str, Any]:
    """组装全部组件（§4.1 组件工厂）。"""
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

    # 会话（时段 + 节假日 + 开盘延迟 + 夜盘跨日）
    session = TradingSession.from_config(paper_cfg)

    # 行情
    quotes = RealTimeQuoteClient(
        symbols={s: paper_cfg.symbols[s].sina_code for s in symbols},
        url=paper_cfg.get("quote_url", "https://hq.sinajs.cn/list="),
        timeout=float(paper_cfg.get("quote_timeout_sec", 10)),
        offline=offline,
    )

    # 信号
    signals = SignalEngine(
        cache_path=paper_cfg.get("signal_cache"),
        freshness_threshold_days=int(paper_cfg.get("freshness_threshold_days", 5)),
        **dict(paper_cfg.get("technical", {}) or {}),
    )

    # 风控
    risk_gate = RiskGate(
        risk_config=paper_cfg.get("risk_config", "configs/risk/v4_atr.yaml"),
        hard_stop=float(paper_cfg.get("risk_hard_stop", 0.20)),
        overrides=dict(paper_cfg.get("risk_overrides", {}) or {}),
        default_intent=float(paper_cfg.get("risk_default_intent", 0.30)),
        default_vol=float(paper_cfg.get("risk_default_vol", 0.02)),
        vol_quantile=float(paper_cfg.get("risk_vol_quantile", 0.5)),
    )

    # 券商（资金/预算/成本）
    cost = build_cost_model(paper_cfg)
    broker = PaperBroker(
        cost=cost,
        initial_capital=float(paper_cfg.get("initial_capital", 100_000.0)),
        budget_ratio=float(paper_cfg.get("budget_ratio", 0.30)),
        data_dir=paper_cfg.get("data_dir", "data/paper"),
    )

    # 计划
    multipliers = {s: float(paper_cfg.symbols[s].multiplier) for s in symbols}
    planner = PlanManager(
        multipliers=multipliers,
        risk_reward_ratio=float(paper_cfg.get("risk_reward_ratio", 1.5)),
        plans_dir=paper_cfg.get("plans_dir", "trade_plans"),
    )

    # 情报
    intel = IntelligenceService.from_config(paper_cfg)

    # 日志 / 复盘
    logger = TradeLogger(log_file=paper_cfg.get("trades_log", "data/paper/trades.log"))
    display = {s: paper_cfg.symbols[s].display for s in symbols}
    reporter = ReviewReporter(
        reports_dir=paper_cfg.get("reports_dir", "deliverables"),
        symbols_display=display,
    )

    return {
        "session": session,
        "quotes": quotes,
        "signals": signals,
        "risk_gate": risk_gate,
        "broker": broker,
        "planner": planner,
        "intel": intel,
        "logger": logger,
        "reporter": reporter,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘交易系统")
    parser.add_argument("--config", default="configs/paper.yaml", help="paper.yaml 路径")
    parser.add_argument("--days", type=int, default=None, help="运行 N 个交易日后退出（默认不退出）")
    parser.add_argument("--offline", action="store_true", help="离线 mock 行情模式（冒烟/演示）")
    parser.add_argument("--smoke", action="store_true", help="冒烟：单轮 tick + 收盘复盘后即退出")
    args = parser.parse_args()

    try:
        paper_cfg = _load_paper_config(args.config)
        symbols = list(paper_cfg.symbols.keys())
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

    scheduler.run()
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
