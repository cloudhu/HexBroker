"""模拟盘调度器（§3.1 TradingScheduler / §4 调用链）。

主循环每 tick 固定走「报价 → 信号 → 风控 → 计划 → 撮合 → 日志」管道，
任何一步失败不阻断整轮（隔离异常、记审计）；并负责：

- 按品种时段 + 节假日 + 开盘延迟调度（A2：非交易时段 0 新开仓）；
- 情报轮询（30-60min）→ 计划备注/风险提示（Q3）；
- 收盘自动复盘 md + stdout 摘要（A5）；
- 满 N 交易日（默认 20）自动输出评估摘要（Q5）；``--days`` 控制退出。
- c0 日线积累渐进启用（accumulate → 满 accumulate_days 自动切 trade）。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from .trade_stats import analyze_trades_log, daily_stats_alerts, summarize_daily
from .types import NewsItem, Quote, SignalFrame, TradeEvent

log = get_logger("PAPER")


class TradingScheduler:
    """主循环调度器（组装全部组件）。"""

    def __init__(
        self,
        cfg: Any,               # paper 配置段（OmegaConf）
        session: Any,
        quotes: Any,
        signals: Any,
        risk_gate: Any,
        planner: Any,
        broker: Any,
        intel: Any,
        logger: Any,
        reporter: Any,
        stop_event: Optional[threading.Event] = None,
        run_days: Optional[int] = None,
        degrader: Optional[Any] = None,          # P2-D 运行时降级器（默认 None=零行为变更）
        degrade_signals: Optional[tuple] = None,  # (signals_file, max_age_sec)
    ) -> None:
        self._cfg = cfg
        self._session = session
        self._quotes = quotes
        self._signals = signals
        self._risk_gate = risk_gate
        self._planner = planner
        self._broker = broker
        self._intel = intel
        self._logger = logger
        self._reporter = reporter

        self._symbols = list(cfg.symbols.keys())
        self._open_delay_min = int(cfg.get("open_delay_min", 5))
        self._close_buffer_min = int(cfg.get("close_buffer_min", 10))  # P1-3：收盘复盘触发缓冲
        self._poll_interval = float(cfg.get("poll_interval_sec", 60))
        self._intel_interval = float(cfg.get("intel_interval_sec", 1800))
        self._snapshot_interval = float(cfg.get("snapshot_interval_sec", 300))
        self._evaluation_days = int(cfg.get("evaluation_days", 20))
        self._bar_freq = str(cfg.get("bar_freq", "1d"))
        self._bar_days = int(cfg.get("bar_days", 120))
        self._default_atr_pct = float(cfg.get("default_atr_pct", 0.01))
        self._account_file = Path(cfg.get("account_file", "data/paper/account.json"))
        self._trades_log = str(cfg.get("trades_log", "data/paper/trades.log"))
        self._c0_daily_csv = Path(cfg.get("c0_daily_csv", "data/paper/c0_daily.csv"))
        self._c0_symbol = "c0" if "c0" in self._symbols else None

        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._run_days = run_days
        self._degrader = degrader
        self._degrade_signals = degrade_signals
        self._block_reasons: dict[Any, dict[str, int]] = {}   # C2：day → {拦截原因: 次数}
        self._last_zero_open_alert: Optional[tuple] = None    # C2：0 开仓汇总去重指纹

        # 运行状态
        self._current_day: Optional[date] = None
        self._active_days: set[date] = set()
        self._day_start: dict[date, datetime] = {}
        self._day_signals: dict[date, list[SignalFrame]] = {}
        self._day_news: dict[date, list[NewsItem]] = {}
        self._day_trades: dict[date, list[TradeEvent]] = {}
        self._all_trades: list[TradeEvent] = []
        self._c0_intraday: dict[date, dict[str, float]] = {}
        self._bars_cache: dict[str, tuple[date, pd.DataFrame]] = {}

        self._last_intel_poll: Optional[datetime] = None
        self._last_snapshot_ts: Optional[datetime] = None
        self._last_stats_alert: Optional[tuple] = None  # 盘中统计告警指纹（状态变化才提醒）
        self._evaluation_done = False
        self._closed_days: set[date] = set()  # P1-3：当日已复盘标记（幂等）

        # ---- P0-1 开仓成本门禁（配置 + 注入 broker.cost） ----
        rg_cfg = cfg.get("risk_gate", {}) if hasattr(cfg, "get") else {}
        self._cost_gate_enabled = bool(rg_cfg.get("cost_gate_enabled", True))
        self._cost_gate_min_ratio = float(rg_cfg.get("cost_gate_min_ratio", 2.0))
        # R3：往返成本口径是否纳入滑点（默认 true）
        self._slippage_in_cost = bool(rg_cfg.get("slippage_in_cost", True))
        if self._risk_gate is not None and hasattr(self._risk_gate, "set_cost"):
            if getattr(self._risk_gate, "_cost", None) is None:
                broker_cost = getattr(self._broker, "cost", None)
                if broker_cost is not None:
                    self._risk_gate.set_cost(
                        broker_cost,
                        cost_gate_enabled=self._cost_gate_enabled,
                        cost_gate_min_ratio=self._cost_gate_min_ratio,
                        slippage_in_cost=self._slippage_in_cost,
                    )

        # ---- P0-2 信号无变化冷却（消除 60s 开-平-开-平循环） ----
        sc_cfg = cfg.get("signal_cooldown", {}) if hasattr(cfg, "get") else {}
        self._signal_cooldown_enabled = bool(sc_cfg.get("enabled", True))
        self._signal_cooldown_p_up_tol = float(sc_cfg.get("p_up_tol", 0.01))
        self._signal_cooldown_exp_ret_tol = float(sc_cfg.get("exp_ret_tol", 0.001))
        # 每品种上一轮「实际开仓」的信号指纹 (p_up, exp_ret, source)；与 day 无关
        self._last_sig_fp: dict[str, tuple[float, float, str]] = {}

        # ---- P0-3 主源信号陈旧运行时告警（每品种每交易日仅一次，避免 60s 刷屏） ----
        self._stale_warn: dict[tuple[str, Optional[date]], bool] = {}

        # ---- P0-2 行情缺失熔断（HALT） ----
        # 行情拉取连续失败（网络异常 getaddressinfo / 全部品种 price<=0 或无报价）达到阈值
        # → 进入 HALT 态：停止开仓 + 告警 + 禁止用缓存价撮合；任意一次成功取数（≥1 有效品种）即复位解除。
        self._halt = False
        self._halt_reason = ""
        self._consecutive_quote_fail = 0
        self._quote_fail_halt_threshold = int(cfg.get("quote_fail_halt_threshold", 5))
        # P2-3 报价时效阈值（秒）：单报价年龄 = now - quote.ts 超此值视为过期，拒绝撮合（与 P0-2 HALT 互补）。
        self._quote_max_age_sec = float(cfg.get("quote_max_age_sec", 3.0 * self._poll_interval))
        self._halt_entered_at: Optional[datetime] = None
        self._halt_last_warn_ts: Optional[datetime] = None
        # P2-2 最小持仓时长（分钟）：持仓不足 N 分钟且为今平（当日新开）时拦截平今，从节奏降今平频率；0=关闭。
        self._min_hold_minutes = int(cfg.get("min_hold_minutes", 0))

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> None:
        """进入主循环，直到 stop_event / --days 达到。"""
        self._print_banner()
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("tick 异常（已隔离，继续下一轮）")
            if self._should_exit():
                log.info("已达到运行目标（--days={}），优雅退出", self._run_days)
                break
            self._stop_event.wait(self._poll_interval)
        self._shutdown()

    def stop(self) -> None:
        """外部请求停止（信号处理）。"""
        self._stop_event.set()

    def _print_banner(self) -> None:
        log.info("=" * 64)
        log.info("模拟盘交易系统已启动")
        log.info("  品种: {}", ", ".join(self._symbols))
        log.info("  轮询: {}s | 情报: {}s | 快照: {}s | 开盘延迟: {}min", 
                 self._poll_interval, self._intel_interval, self._snapshot_interval, self._open_delay_min)
        log.info("  评估: 满 {} 个交易日 | 当前已运行: {} 个交易日", 
                 self._evaluation_days, self._broker.trading_day_count)
        log.info("  生效节假日: {}", sorted(str(d) for d in self._session.holidays))
        log.info("  等待交易时段…（非交易时段不开新仓）")
        log.info("=" * 64)

    # ------------------------------------------------------------------
    # 单轮 tick
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        now = datetime.now()
        day = self._session.day_label(now)
        if day is None:
            return

        # 收盘检测（P1-3）：独立于夜盘翻转——当前时刻已过当日全部品种收盘时刻 + 缓冲
        # 且当日未复盘 → 触发 _on_close（如 8/24 15:10 即复盘，不等到 21:00 夜盘翻转）。
        # 周末/节假日 day_label 归属下一交易日 → day_closed 返回 None，不误触发。
        closed_day = self._session.day_closed(now, self._close_buffer_min)
        if closed_day is not None and closed_day not in self._closed_days:
            self._on_close(closed_day)

        tradable_now = any(self._session.is_tradable(s, now) for s in self._symbols)
        if tradable_now:
            if self._current_day is None:
                self._current_day = day
            if day not in self._active_days:
                self._active_days.add(day)
                self._day_start[day] = now
            # ---- P0-2 行情熔断：拉取失败计数 + HALT 判定 ----
            quotes = self._fetch_quotes_with_halt(now)
            if self._halt:
                self._halt_warn(now)
            marks = self._build_marks(quotes)
            for symbol in self._symbols:
                try:
                    self._process_symbol(symbol, now, quotes.get(symbol), marks)
                except Exception:
                    log.exception("品种 {} 处理异常（已隔离）", symbol)

        self._maybe_poll_intel(now)
        self._maybe_snapshot(now)
        self._maybe_degrade(now)   # P2-D：治理运行时降级评估（默认关，节流+异常隔离）

    def _maybe_degrade(self, now: datetime) -> None:
        """P2-D 运行时降级：读信号文件→规则评估（触发=LIVE 方案降 SHADOW 并留痕）。

        degrader 未启用（None）时直接返回；节流/新鲜度/异常全部在此隔离，绝不影响交易 tick。
        """
        if self._degrader is None:
            return
        try:
            if not self._degrader.should_evaluate(now):
                return
            if self._degrade_signals is None:
                return
            from hexbroker.governance import read_signals_file  # 懒加载（红线纪律）

            path, max_age = self._degrade_signals
            signals = read_signals_file(path, max_age)
            self._degrader.observe_and_evaluate(signals)
        except Exception:
            log.exception("治理降级评估异常（已隔离，不影响交易 tick）")

    # ------------------------------------------------------------------
    # 单品种管道：quote → signal → risk → plan → execute → log
    # ------------------------------------------------------------------
    def _process_symbol(self, symbol: str, now: datetime, quote: Optional[Quote], marks: dict[str, float]) -> None:
        if quote is None or not quote.valid():
            log.warning("品种 {} 无有效行情，跳过", symbol)
            return
        # P2-3 报价时效校验：单报价年龄 = now - quote.ts 超阈值视为过期，拒绝撮合（与 P0-2 HALT 互补）。
        age = (now - quote.ts).total_seconds()
        if age > self._quote_max_age_sec:
            log.warning(
                "品种 {} 行情过期（age={:.0f}s > max={:.0f}s），跳过撮合",
                symbol, age, self._quote_max_age_sec,
            )
            return
        if not self._session.is_tradable(symbol, now):
            return
        if not self._session.is_open_delay_passed(symbol, now, self._open_delay_min):
            return  # 开盘延迟中（跳过集合竞价，Q6）

        day = self._session.day_label(now)
        mode = self._effective_mode(symbol)

        # ---- accumulate 模式：仅跟踪（不开新仓）；已有持仓仅风控 ----
        if mode == "accumulate":
            self._track_accumulate(symbol, quote, day)
            if abs(self._broker.position(symbol)) > 1e-12:
                self._risk_manage_only(symbol, quote, marks, day, now)
            return

        # ---- 信号 ----
        sig = self._signals.latest_signal(symbol, now)
        # P0-3：主源信号陈旧（fd > 阈值）→ 告警一次（每品种每交易日），随后走技术兜底/禁开
        self._warn_stale_signal_once(symbol, sig, day)
        bars = self._cached_bars(symbol, day)
        if sig is None or not sig.is_effective:
            # 主源过期/缺失 → 技术兜底仅作降级方向提示（is_effective=False，不构成 edge）。
            # 配合 P0-3「无持仓禁开」硬约束：RiskGate._intent 对 is_effective=False 返回 0，
            # 不会触发成本门禁、不会新开仓（审计 P1-1/P1-2 闭合）；有持仓则仅风控管理。
            sig = self._signals.technical_fallback(symbol, bars)
        if sig is None:
            if abs(self._broker.position(symbol)) > 1e-12:
                sig = self._signals.neutral_signal(symbol, now)
                log.warning("信号缺失 symbol={} 有持仓，仅风控管理", symbol)
            else:
                log.warning("信号缺失 symbol={} 且无持仓，禁止开新仓", symbol)
                return
        self._day_signals.setdefault(day, []).append(sig)

        # ---- 风控 ----
        acct = self._broker.snapshot(marks)
        atr = self._atr_from_bars(symbol, bars, quote)
        pos_ctx = self._broker.position_ctx(symbol, quote, atr)
        returns, volumes, ma_price = self._aux_from_bars(bars)
        decision = self._risk_gate.evaluate(sig, quote, acct, pos_ctx, returns, volumes, ma_price)

        # ---- P0-2 信号无变化冷却（消除 60s 开-平-开-平循环） ----
        # 仅拦截「当前无持仓 + 风控意图开仓 + 信号指纹与上一轮实际开仓相同」；
        # 已有持仓的风控动作（止损/止盈/S1-S5 平仓）永远正常走 evaluate，不受影响。
        if (
            self._signal_cooldown_enabled
            and abs(pos_ctx.position) < 1e-12
            and abs(decision.target_position) > 1e-9
        ):
            fp = self._signal_fingerprint(sig)
            prev = self._last_sig_fp.get(symbol)
            if prev is not None and self._signal_fp_same(prev, fp):
                log.info(
                    "品种 {} 信号未变化（p_up={:.4f} exp_ret={:.4f} src={}），冷却拦截重复开仓",
                    symbol, sig.p_up, sig.exp_ret, sig.source,
                )
                decision.target_position = 0.0
                decision.reason = "signal_cooldown"

        # ---- P0-2 熔断态：停止开仓（防御性） ----
        # HALT 期间禁止新开仓；已有持仓的风控平仓（止损/止盈/S1-S5）仍按有效报价执行，
        # 但若行情缺失则本品种 quote 无效 → _process_symbol 早返回，天然禁止用缓存价撮合。
        if self._halt and abs(pos_ctx.position) < 1e-12 and abs(decision.target_position) > 1e-9:
            log.info(
                "HALT 态：拦截新开仓 symbol={}（原因：{}）", symbol, self._halt_reason
            )
            decision.target_position = 0.0
            decision.reason = "halt_no_open"

        # ---- P2-2 最小持仓时长（今平节奏门，不拦风控强平） ----
        cur = pos_ctx.position
        new_pos = decision.target_position
        if (
            self._min_hold_minutes > 0
            and abs(cur) > 1e-12
            and not getattr(decision, "liquidate", False)
            and abs(new_pos) < abs(cur)                     # 减仓/平仓/反手（缩小持仓=今平动作）
            and pos_ctx.open_ts is not None
            and pos_ctx.open_ts.date() == now.date()         # 今开
            and (now - pos_ctx.open_ts).total_seconds() / 60.0 < self._min_hold_minutes
        ):
            log.info(
                "品种 {} 持仓不足 {} 分钟（今平），min_hold 拦截平今",
                symbol, self._min_hold_minutes,
            )
            # P2-2 修正：PositionCtx.position 为「手数」，decision.target_position 为「仓位比例
            # [-1,1]」（PlanManager._size_qty 按 比例=手数×价×乘数/权益 换算）。若直接令
            # target_position=cur（手数）会被当作 100% 满仓比例→计划放大到多手，违背「维持持仓」
            # 本意。故换算回维持当前持仓手数对应的比例（即 _size_qty 的逆），保持仓位不变。
            multiplier = float(self._broker.cost._multiplier(symbol))
            maintain_ratio = (abs(cur) * quote.price * multiplier / acct.equity) if acct.equity > 0 else 0.0
            decision.target_position = maintain_ratio if cur >= 0 else -maintain_ratio
            decision.reason = "min_hold"

        # ---- C2 可观测性：无持仓且未开仓 → 累计拦截原因（供「0 开仓」显性汇总）----
        if abs(pos_ctx.position) < 1e-12 and abs(decision.target_position) < 1e-9:
            _reason = str(getattr(decision, "reason", "") or "no_intent")
            _bucket = self._block_reasons.setdefault(day, {})
            _bucket[_reason] = _bucket.get(_reason, 0) + 1

        # ---- 计划 ----
        plan = self._planner.update_from_signal(sig, decision, quote=quote, equity=acct.equity)

        # ---- 撮合 ----
        event = self._broker.execute_plan(plan, quote, now)
        if event is not None:
            self._day_trades.setdefault(day, []).append(event)
            self._all_trades.append(event)
            self._logger.trade(event)
            # P0-2：仅在实际开仓时更新指纹（预算拒绝/冷却拦截不更新 → 下次同信号仍可重试或继续拦截）
            if (
                self._signal_cooldown_enabled
                and event.is_open
                and abs(pos_ctx.position) < 1e-12
            ):
                self._last_sig_fp[symbol] = self._signal_fingerprint(sig)

    # ------------------------------------------------------------------
    # accumulate 模式（c0 决策，§8.1）
    # ------------------------------------------------------------------
    def _effective_mode(self, symbol: str) -> str:
        sym_cfg = self._cfg.symbols[symbol]
        mode = str(sym_cfg.get("mode", "trade"))
        if mode == "accumulate":
            need = int(sym_cfg.get("accumulate_days", 30))
            if self._c0_daily_row_count() >= need:
                log.info("c0 日线积累满 {} 个交易日，自动切换为 trade 模式", need)
                return "trade"
        return mode

    def _c0_daily_row_count(self) -> int:
        if not self._c0_daily_csv.exists():
            return 0
        try:
            lines = self._c0_daily_csv.read_text(encoding="utf-8").splitlines()
            return max(0, len([l for l in lines if l.strip() and not l.startswith("date,")]))
        except Exception:
            return 0

    def _track_accumulate(self, symbol: str, quote: Quote, day: date) -> None:
        """跟踪 c0 盘中 OHLC（收盘时落盘日线快照）。"""
        stats = self._c0_intraday.setdefault(
            day, {"open": quote.price, "high": quote.price, "low": quote.price, "close": quote.price}
        )
        stats["high"] = max(stats["high"], quote.high or quote.price)
        stats["low"] = min(stats["low"], quote.low or quote.price)
        stats["close"] = quote.price

    def _append_c0_daily(self, day: date) -> None:
        """收盘将 c0 主力日线快照（open/high/low/close/settle）追加到 CSV。

        P2-6：读-写-替换原子写（tmp + os.replace），避免追加中断产生半行。
        """
        if self._c0_symbol is None:
            return
        stats = self._c0_intraday.get(day)
        if stats is None:
            log.info("当日无 c0 行情（{}），跳过日线积累", day)
            return
        existing = ""
        if self._c0_daily_csv.exists():
            existing = self._c0_daily_csv.read_text(encoding="utf-8")
        self._c0_daily_csv.parent.mkdir(parents=True, exist_ok=True)
        header = "date,open,high,low,close,settle\n"
        row = (
            f"{day.isoformat()},{stats['open']:g},{stats['high']:g},"
            f"{stats['low']:g},{stats['close']:g},{stats['close']:g}\n"
        )
        body = existing if existing.strip() else header
        if not body.endswith("\n"):
            body += "\n"
        tmp = self._c0_daily_csv.with_name(self._c0_daily_csv.name + ".tmp")
        tmp.write_text(body + row, encoding="utf-8")
        tmp.replace(self._c0_daily_csv)
        log.info("c0 日线快照已追加 day={} close={}", day, stats["close"])

    # ------------------------------------------------------------------
    # 风控专用（有持仓仅风控，不开新仓）
    # ------------------------------------------------------------------
    def _risk_manage_only(self, symbol: str, quote: Quote, marks: dict[str, float], day: date, now: datetime) -> None:
        sig = self._signals.neutral_signal(symbol, now)
        self._day_signals.setdefault(day, []).append(sig)
        acct = self._broker.snapshot(marks)
        atr = self._default_atr_pct * quote.price
        pos_ctx = self._broker.position_ctx(symbol, quote, atr)
        decision = self._risk_gate.evaluate(sig, quote, acct, pos_ctx)
        plan = self._planner.update_from_signal(sig, decision, quote=quote, equity=acct.equity)
        event = self._broker.execute_plan(plan, quote, now)
        if event is not None:
            self._day_trades.setdefault(day, []).append(event)
            self._all_trades.append(event)
            self._logger.trade(event)

    # ------------------------------------------------------------------
    # 行情辅助
    # ------------------------------------------------------------------
    def _build_marks(self, quotes: dict[str, Quote]) -> dict[str, float]:
        """组合所有品种的最新价（无报价品种用持仓均价兜底）。"""
        marks: dict[str, float] = {}
        for sym in self._symbols:
            q = quotes.get(sym)
            if q is not None and q.price > 0:
                marks[sym] = q.price
        for sym, pos in self._broker.broker.positions.items():
            if sym not in marks and abs(pos) > 1e-12:
                avg = self._broker.avg_entry(sym)
                if avg > 0:
                    marks[sym] = avg
        return marks

    def _cached_bars(self, symbol: str, day: date) -> pd.DataFrame:
        """当日缓存 K 线（避免每 tick 网络拉取）。"""
        cached_day, df = self._bars_cache.get(symbol, (None, pd.DataFrame()))
        if cached_day == day and df is not None and not df.empty:
            return df
        df = self._quotes.fetch_bars(symbol, self._bar_freq, self._bar_days)
        self._bars_cache[symbol] = (day, df)
        return df

    def _atr_from_bars(self, symbol: str, bars: pd.DataFrame, quote: Quote) -> float:
        if bars is not None and not bars.empty and "close" in bars.columns:
            try:
                close = pd.to_numeric(bars["close"], errors="coerce").dropna()
                if len(close) >= 3 and "high" in bars.columns and "low" in bars.columns:
                    high = pd.to_numeric(bars["high"], errors="coerce")
                    low = pd.to_numeric(bars["low"], errors="coerce")
                    tr = pd.concat(
                        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1
                    ).max(axis=1)
                    atr = float(tr.rolling(14).mean().iloc[-1])
                    if atr > 0:
                        return atr
            except Exception:
                pass
        return self._default_atr_pct * quote.price

    def _aux_from_bars(self, bars: pd.DataFrame) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[float]]:
        """从 K 线提取 S1/S2/S5 所需的 returns/volumes/ma_price。"""
        if bars is None or bars.empty or "close" not in bars.columns:
            return None, None, None
        try:
            close = pd.to_numeric(bars["close"], errors="coerce").dropna()
            if len(close) < 2:
                return None, None, None
            returns = close.pct_change().dropna().to_numpy(dtype=float)
            volumes = None
            if "volume" in bars.columns:
                volumes = pd.to_numeric(bars["volume"], errors="coerce").to_numpy(dtype=float)
            ma_price = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
            return returns, volumes, ma_price
        except Exception:
            return None, None, None

    # ------------------------------------------------------------------
    # P0-3 信号陈旧运行时告警（每品种每交易日一次）
    # ------------------------------------------------------------------
    def _warn_stale_signal_once(
        self, symbol: str, sig: Optional[SignalFrame], day: Optional[date]
    ) -> bool:
        """主源信号陈旧告警（去重：同品种同交易日仅告警一次，避免 60s tick 刷屏）。

        Args:
            symbol: 品种代码。
            sig: 主源信号帧（``latest_signal`` 结果，可为 None）。
            day: 当前交易日标签（``session.day_label``）。

        Returns:
            本次是否实际输出了告警（已去重则为 False）。
        """
        if sig is None:
            return False
        threshold = getattr(self._signals, "freshness_threshold", None)
        if threshold is None:
            return False  # 信号引擎未暴露阈值（如 mock/旧实现）→ 静默跳过
        try:
            fd = int(sig.freshness_days)
            limit = int(threshold)
        except (TypeError, ValueError):
            return False
        if fd <= limit:
            return False
        key = (symbol, day)
        if self._stale_warn.get(key):
            return False
        self._stale_warn[key] = True
        log.warning(
            "[告警] 信号陈旧 fd={} 品种={}，主源过期，已降级/禁开（技术兜底接手）", fd, symbol
        )
        return True

    # ------------------------------------------------------------------
    # P0-2 行情缺失熔断（HALT）
    # ------------------------------------------------------------------
    def _fetch_quotes_with_halt(self, now: datetime) -> dict[str, "Quote"]:
        """拉取行情并在连续失败时进入 HALT 态。

        行为（P0-2 / P1-3 修复：26 次 getaddrinfo 失败后系统用陈旧价继续交易）：
        - 拉取抛异常（网络层失败）→ 连续失败 +1；达阈值且未熔断 → 进入 HALT。
        - 拉取成功但**全部品种行情无效**（price<=0 或无报价）→ 同样计为行情缺失 +1；
          达阈值且未熔断 → 进入 HALT（覆盖数据源返回全 0 的失真场景）。
        - 至少一个有效品种 → 连续失败清零；若此前在 HALT → 解除（行情恢复）。
        - 返回 quotes 字典；异常/全无效时仍返回原始结果（``_process_symbol`` 按 ``valid()`` 跳过）。
        """
        try:
            quotes = self._quotes.fetch_quotes(self._symbols)
        except Exception:
            self._consecutive_quote_fail += 1
            log.exception(
                "行情拉取异常（连续第 {} 次 / 阈值 {}）",
                self._consecutive_quote_fail, self._quote_fail_halt_threshold,
            )
            if self._consecutive_quote_fail >= self._quote_fail_halt_threshold and not self._halt:
                self._enter_halt(
                    "行情连续拉取失败 {} 次（阈值 {}）".format(
                        self._consecutive_quote_fail, self._quote_fail_halt_threshold
                    )
                )
            return {}
        # 拉取成功：统计有效品种
        valid_count = sum(1 for q in quotes.values() if q is not None and q.valid())
        if valid_count == 0 and len(self._symbols) > 0:
            self._consecutive_quote_fail += 1
            log.warning(
                "行情拉取成功但全部品种无效（price<=0 或无报价），计为行情缺失（连续第 {} 次 / 阈值 {}）",
                self._consecutive_quote_fail, self._quote_fail_halt_threshold,
            )
            if self._consecutive_quote_fail >= self._quote_fail_halt_threshold and not self._halt:
                self._enter_halt(
                    "全部品种行情无效（连续 {} 次）".format(self._consecutive_quote_fail)
                )
            return quotes  # 含无效行情，_process_symbol 会按 valid() 跳过（禁止缓存价撮合）
        # 至少一个有效品种 → 复位 + 解除 HALT
        self._consecutive_quote_fail = 0
        if self._halt:
            self._exit_halt("行情恢复（有效品种数 {}）".format(valid_count))
        return quotes

    def _enter_halt(self, reason: str) -> None:
        """进入 HALT 态：停止开仓 + 告警 + 禁止用缓存价撮合。"""
        self._halt = True
        self._halt_reason = reason
        self._halt_entered_at = datetime.now()
        self._halt_last_warn_ts = self._halt_entered_at
        log.warning(
            "[熔断] 进入 HALT 态：停止开仓，禁止用缓存价撮合。原因：{}", reason
        )

    def _exit_halt(self, reason: str) -> None:
        """解除 HALT 态（行情恢复）。"""
        was = self._halt
        self._halt = False
        self._halt_reason = ""
        self._halt_entered_at = None
        if was:
            log.info("[熔断] 解除 HALT 态：{}", reason)

    def _halt_warn(self, now: datetime) -> None:
        """熔断态周期提醒（避免每 tick 刷屏，约每 5 分钟一条）。"""
        if self._halt_last_warn_ts is None:
            self._halt_last_warn_ts = now
            return
        if (now - self._halt_last_warn_ts).total_seconds() >= 300:
            self._halt_last_warn_ts = now
            duration = int((now - (self._halt_entered_at or now)).total_seconds())
            log.warning(
                "[熔断] 仍处于 HALT 态：停止开仓，禁止用缓存价撮合。原因：{}（已持续 {} 秒）",
                self._halt_reason, duration,
            )

    def halt_state(self) -> tuple[bool, str, int]:
        """外部监测接口：返回 ``(是否熔断, 原因, 连续失败计数)``。"""
        return (self._halt, self._halt_reason, self._consecutive_quote_fail)

    # ------------------------------------------------------------------
    # P0-2 信号指纹（无变化冷却）
    # ------------------------------------------------------------------
    @staticmethod
    def _signal_fingerprint(sig: SignalFrame) -> tuple[float, float, str]:
        """有效信号指纹 (p_up, exp_ret, source)；source 不同即视为信号变化。"""
        return (float(sig.p_up), float(sig.exp_ret), str(sig.source))

    def _signal_fp_same(self, a: tuple[float, float, str], b: tuple[float, float, str]) -> bool:
        """指纹相同：p_up/exp_ret 均在容差内且信号源一致。"""
        return (
            a[2] == b[2]
            and abs(a[0] - b[0]) < self._signal_cooldown_p_up_tol
            and abs(a[1] - b[1]) < self._signal_cooldown_exp_ret_tol
        )

    # ------------------------------------------------------------------
    # 情报轮询（Q3：仅备注/风险提示）
    # ------------------------------------------------------------------
    def _maybe_poll_intel(self, now: datetime) -> None:
        if self._last_intel_poll is None or (now - self._last_intel_poll).total_seconds() >= self._intel_interval:
            self._last_intel_poll = now
            since = now - timedelta(hours=1)
            items = self._intel.poll(self._symbols, since)
            if not items:
                return
            day = self._session.day_label(now)
            self._day_news.setdefault(day, []).extend(items)
            changes = self._planner.apply_news(items)
            for c in changes:
                self._logger.plan_change(c)

    # ------------------------------------------------------------------
    # 定期快照
    # ------------------------------------------------------------------
    def _maybe_snapshot(self, now: datetime) -> None:
        if self._last_snapshot_ts is None or (now - self._last_snapshot_ts).total_seconds() >= self._snapshot_interval:
            self._last_snapshot_ts = now
            try:
                quotes = self._quotes.fetch_quotes(self._symbols)
                marks = self._build_marks(quotes)
                acct = self._broker.snapshot(marks)
                self._broker.save_snapshot(self._account_file)
                log.info("账户快照已保存 equity={:.2f} cash={:.2f}", acct.equity, acct.cash)
                day = self._session.day_label(now)
                if day is not None:
                    self._emit_daily_stats(day)
            except Exception:
                log.exception("快照落盘失败（已隔离）")

    def _emit_daily_stats(self, day: date) -> None:
        """盘中定时输出当日去重成交统计（随快照节奏，约 snapshot_interval 一次）。

        从审计日志按 trade_id 去重统计当日成交（规避会话重放 3× 伪增），
        输出一行 ``[统计]`` 摘要；当日无成交时不打扰（静默）。

        同时运行告警检查（``daily_stats_alerts``，如闭合偏差 ≠0、副本字段不一致），
        异常时输出 ``[告警]`` 行；同一告警指纹只在状态变化时提醒一次，避免每 5 分钟刷屏。
        """
        try:
            result = analyze_trades_log(
                self._trades_log, day, account_json_path=str(self._account_file)
            )
            if not result["filtered_count"]:
                self._warn_zero_open(day)   # C2：无成交时明确说明「为什么不交易」
                return
            log.info("[统计] 盘中当日统计：{}", summarize_daily(result))
            alerts = daily_stats_alerts(result)
            if alerts:
                fingerprint = (day.isoformat(), tuple(alerts))
                if fingerprint != self._last_stats_alert:
                    for alert in alerts:
                        log.warning("[告警] {}", alert)
                    self._last_stats_alert = fingerprint
            else:
                self._last_stats_alert = None  # 状态恢复，下次异常可再次提醒
        except Exception:
            log.exception("当日统计输出失败（已隔离）")

    def _warn_zero_open(self, day: Any) -> None:
        """C2：当日 0 开仓时输出拦截原因分布（让"静默停摆"显性化，去重防刷屏）。"""
        try:
            reasons = self._block_reasons.get(day) or {}
            if not reasons:
                return
            ranked = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)
            text = "、".join(f"{k}={v}" for k, v in ranked[:5])
            fingerprint = (day, tuple(ranked))
            if fingerprint == self._last_zero_open_alert:
                return
            self._last_zero_open_alert = fingerprint
            hint = ""
            if any(k in ("no_intent", "cost_gate_reject") for k in reasons):
                hint = "（no_intent 主因通常为主源信号过期→技术兜底禁开，请检查信号新鲜度 fd）"
            log.warning("[统计] 今日开仓 0 笔，未开仓原因分布：{} {}", text, hint)
        except Exception:
            log.exception("0 开仓原因汇总失败（已隔离）")

    # ------------------------------------------------------------------
    # 收盘复盘（A5）
    # ------------------------------------------------------------------
    def _on_close(self, day: date) -> None:
        """收盘复盘（A5）。P1-3：幂等——当日已复盘不重复处理。"""
        if day in self._closed_days:
            log.info("交易日 {} 已复盘，跳过重复处理", day)
            return
        self._closed_days.add(day)
        log.info("交易日 {} 已收盘，开始复盘", day)
        try:
            self._append_c0_daily(day)
        except Exception:
            log.exception("c0 日线积累失败（已隔离）")

        try:
            quotes = self._quotes.fetch_quotes(self._symbols)
            marks = self._build_marks(quotes)
        except Exception:
            marks = {}
        acct = self._broker.snapshot(marks)
        trades = self._day_trades.get(day, [])
        day_signals = self._day_signals.get(day, [])
        news = self._day_news.get(day, [])
        since = self._day_start.get(day)
        changes = self._planner.recent_changes(since=since)
        plans = self._planner.get_all_plans()
        signals_by_symbol: dict[str, list[SignalFrame]] = {}
        for s in day_signals:
            signals_by_symbol.setdefault(s.symbol, []).append(s)

        try:
            self._reporter.generate(
                day, acct, trades, plans, signals_by_symbol, news, changes,
                trades_log_path=self._cfg.get("trades_log", "data/paper/trades.log"),
            )
        except Exception:
            log.exception("复盘报告生成失败（已隔离）")
        try:
            self._planner.save_plan_file(day)
        except Exception:
            log.exception("交易计划落盘失败（已隔离）")
        self._logger.daily_summary(day, trades, acct)

        try:
            self._broker.record_trading_day(day)
            self._broker.save_snapshot(self._account_file)
        except Exception:
            log.exception("交易日登记/快照保存失败（已隔离）")

        self._maybe_evaluation(day)

        # 清理当日状态
        self._day_trades.pop(day, None)
        self._day_signals.pop(day, None)
        self._day_news.pop(day, None)
        self._c0_intraday.pop(day, None)

    # ------------------------------------------------------------------
    # 首轮评估（Q5）
    # ------------------------------------------------------------------
    def _maybe_evaluation(self, day: date) -> None:
        if self._evaluation_done or self._broker.trading_day_count < self._evaluation_days:
            return
        self._evaluation_done = True
        try:
            quotes = self._quotes.fetch_quotes(self._symbols)
            marks = self._build_marks(quotes)
        except Exception:
            marks = {}
        acct = self._broker.snapshot(marks)
        self._reporter.generate_evaluation(
            day,
            acct,
            list(self._all_trades),
            self._broker.trading_day_count,
            initial_capital=self._broker.broker.initial_capital,
            trades_log_path=self._trades_log,
        )
        log.info(
            "★★★★★ 已运行满 {} 个交易日，评估摘要已生成（权益 {:.2f} / 已实现 {:.2f} / 成交 {} 笔）★★★★★",
            self._broker.trading_day_count,
            acct.equity,
            sum(acct.realized.values()),
            len(self._all_trades),
        )

    def _should_exit(self) -> bool:
        """--days N：满 N 个交易日后退出。"""
        if self._run_days is not None and self._broker.trading_day_count >= self._run_days:
            return True
        return False

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def _shutdown(self) -> None:
        log.info("模拟盘停止，保存账户快照…")
        try:
            quotes = self._quotes.fetch_quotes(self._symbols)
            marks = self._build_marks(quotes)
            acct = self._broker.snapshot(marks)
            self._broker.save_snapshot(self._account_file)
            self._logger.daily_summary(datetime.now().date(), [], acct)
        except Exception:
            log.exception("停止时保存快照失败")
        log.info("模拟盘已退出")