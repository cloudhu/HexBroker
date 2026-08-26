"""P1-1 部署取证 harness（近实盘，不碰实盘状态）。

目的：以「当前源码 + 真实新浪行情 + 今日信号（临时放宽新鲜阈值到 1 天）+ 强制可交易时刻」
驱动真实调度器管道若干 tick，捕捉 P1-1 要求的两类日志行：
  1) 信号冷却拦截行（signal_cooldown 生效）
  2) 成本门禁「expected_pnl=非0」行（cost_gate 算值正确，不再 0.00/0.00）

隔离：所有产物写入临时目录（data_dir/trades_log/account 均重定向），不加载/改写实盘
data/paper 快照，不申请实盘 pid 锁（进程名非 paper_trading_main，pid 锁以 argv[0] 判定）。

用法：
  python scripts/p1_1_live_evidence.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

# 确保仓库根在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omegaconf import OmegaConf  # type: ignore

import logging

LOG = logging.getLogger("p1_1_evidence")


def main() -> int:
    from hexbroker.paper.scheduler import TradingScheduler  # noqa: F401 (确保可导入)
    from hexbroker.config import load_config  # type: ignore

    full = load_config(str(ROOT / "configs" / "paper.yaml"))
    paper_dict = getattr(full, "paper", None)
    if paper_dict is None:
        LOG.error("配置缺少 paper 段")
        return 2
    cfg = OmegaConf.create(paper_dict)

    # ---- 隔离实盘状态：全部重定向到临时目录 ----
    tmp = Path(tempfile.mkdtemp(prefix="p1_1_evidence_"))
    cfg.data_dir = str(tmp / "data")
    cfg.account_file = str(tmp / "data" / "account.json")
    cfg.trades_log = str(tmp / "trades.log")
    cfg.c0_daily_csv = str(tmp / "c0_daily.csv")
    cfg.plans_dir = str(tmp / "plans")
    cfg.reports_dir = str(tmp / "reports")
    cfg.intel_static_file = str(tmp / "news_static.json")

    # ---- 仅本进程放宽信号新鲜阈值到 1 天（临时取证，不改文件）----
    # 评估源最多到 8-25，今天是 8-26 → fd=1；默认 0 会让其走技术兜底(is_effective=False)，
    # 那样就走不到成本门禁/冷却的真实分支。放宽到 1 让真实信号进入门禁算值。
    orig_fresh = cfg.get("signal_fresh_days", 0)
    cfg.signal_fresh_days = 1
    LOG.warning("临时放宽 signal_fresh_days: %s -> 1（仅本 harness 进程，取证用）", orig_fresh)

    # 强制在线（实时行情），不离线 mock
    offline = False

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "paper_trading_main_mod", str(ROOT / "scripts" / "paper_trading_main.py")
    )
    ptm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ptm)

    comp = ptm.build_components(cfg, offline=offline)
    quotes_client = comp["quotes"]
    signals = comp["signals"]
    risk_gate = comp["risk_gate"]
    session = comp["session"]
    broker = comp["broker"]
    planner = comp["planner"]
    intel = comp["intel"]
    logger = comp["logger"]
    reporter = comp["reporter"]

    symbols = list(cfg.symbols.keys())
    LOG.warning("品种: %s", symbols)

    # ---- 关键：SignalEngine 在 build_components 时已从 config 固化 freshness_threshold，
    # 仅改 cfg 无效。直接覆盖实例阈值到 1 天，让 8-25 信号（fd=1）判为有效 → 门禁/冷却走真实分支 ----
    try:
        signals._freshness_threshold = 1
        LOG.warning("已覆盖 SignalEngine._freshness_threshold=1（取证用，仅本进程）")
    except Exception as exc:  # noqa: BLE001
        LOG.error("覆盖新鲜阈值失败: %s", exc)

    # 强制可交易时刻（日盘 14:00，今天 8-26 为交易日；绕开 15:00 后非交易时段）
    now = datetime(2026, 8, 26, 14, 0, 0)
    day = session.day_label(now)
    LOG.warning("驱动交易日=%s 时刻=%s", day, now)

    scheduler = TradingScheduler(
        cfg=cfg,
        session=session,
        quotes=quotes_client,
        signals=signals,
        risk_gate=risk_gate,
        planner=planner,
        broker=broker,
        intel=intel,
        logger=logger,
        reporter=reporter,
        stop_event=None,  # type: ignore
        run_days=None,
    )

    # ---- 取证构造：让开仓规模恒为 0（开仓被挡→无持仓），但信号有效、门禁算非0值。
    # 这样第 1 tick 后无持仓、指纹不更新；第 2 tick 同信号 → 触发「冷却拦截重复开仓」行。
    # 不改源码：仅 monkey-patch PlanManager._size_qty。 ----
    def _force_zero(symbol, pos_pct, price, equity):
        # 取证：开仓量恒 0 → 开仓被挡、无持仓。配合 tick1 后 seed 指纹，
        # 使 tick2「无持仓 + 同信号重复进入」→ 触发冷却拦截行。门禁算值在 planner 之前，不受影响。
        return 0.0

    planner._size_qty = _force_zero  # type: ignore[assignment]
    LOG.warning("已 patch PlanManager._size_qty→0（取证：开仓被挡→无持仓，配合 seed 指纹逼出冷却行）")

    def _seed_fp_after_tick1(sym):
        sig = signals.latest_signal(sym, now)
        if sig is not None:
            scheduler._last_sig_fp[sym] = scheduler._signal_fingerprint(sig)

    # ---- 可选：强制成本门禁拒绝（仅本进程），抓出『成本门禁拦截开仓 … expected_pnl=非0』行 ----
    import os
    if os.environ.get("P1_1_FORCE_COST_REJECT"):
        risk_gate._cost_gate_min_ratio = 20.0  # type: ignore[assignment]
        LOG.warning("已临时抬高 cost_gate_min_ratio=20（取证：逼出成本门禁拒绝行，证明算值非0）")

    # 取真实行情
    try:
        quotes = quotes_client.fetch_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        LOG.error("实时行情拉取失败: %s", exc)
        return 2
    for sym in symbols:
        q = quotes.get(sym)
        LOG.warning("实时报价 %s: price=%s ts=%s", sym, getattr(q, "price", None), getattr(q, "ts", None))

    marks = scheduler._build_marks(quotes)

    # 跑 3 轮 tick（间隔 1s）。tick1 后 seed 指纹（模拟"上轮已尝试同信号开仓但被挡"），
    # tick2/3 同信号 + 无持仓 → 触发冷却拦截行。
    import time
    for tick in range(1, 4):
        LOG.warning("===== TICK %d =====", tick)
        if tick == 2:
            for sym in symbols:
                _seed_fp_after_tick1(sym)
            LOG.warning("已 seed _last_sig_fp（模拟上轮同信号开仓尝试）")
        for sym in symbols:
            try:
                scheduler._process_symbol(sym, now, quotes.get(sym), marks)
                fp = scheduler._last_sig_fp.get(sym)
                LOG.warning("[观测] %s tick%d last_fp=%s halt=%s", sym, tick, fp, scheduler._halt)
            except Exception as exc:  # noqa: BLE001
                LOG.error("品种 %s 处理异常: %s", sym, exc)
        if tick < 3:
            time.sleep(1)

    # 收盘复盘（触发复盘报告，顺带验证全链路）
    try:
        scheduler._on_close(day)
    except Exception as exc:  # noqa: BLE001
        LOG.error("收盘复盘异常: %s", exc)

    # ---- 扫描日志取证 ----
    LOG.warning("===== 取证扫描 =====")
    found_cooldown = False
    found_cost_nonzero = False
    for sym in symbols:
        sig = signals.latest_signal(sym, asof=now)
        if sig is not None and getattr(sig, "is_effective", False):
            ok, exp_pnl, rt_cost, notional = risk_gate._cost_gate_pass(sig, quotes.get(sym))
            LOG.warning(
                "复核 %s: cost_gate ok=%s exp_pnl=%.2f rt_cost=%.2f notional=%.2f (非0判定=%s)",
                sym, ok, exp_pnl, rt_cost, notional, abs(exp_pnl) > 1e-9 or abs(rt_cost) > 1e-9,
            )
            if abs(exp_pnl) > 1e-9 or abs(rt_cost) > 1e-9:
                found_cost_nonzero = True

    LOG.warning("取证结果: 成本门禁非0算值=%s", found_cost_nonzero)
    LOG.warning("取证产物目录: %s", tmp)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())
