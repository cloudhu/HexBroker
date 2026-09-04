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

import json
import math
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.logging import get_logger, log_structured
from .trade_stats import analyze_trades_log, daily_stats_alerts, summarize_daily
from .types import (
    EVT_DECISION_TRACE,
    AccountSnapshot,
    CooldownRecord,
    NewsItem,
    PositionCtx,
    Quote,
    SignalFrame,
    TradeEvent,
)

log = get_logger("PAPER")


def _num(value: Any, digits: int = 6) -> Optional[float]:
    """浮点安全取值：None / 非数值 / NaN / Inf 一律转 ``None``。

    P1-1 trace 必须产出**合法 JSON**：``json.dumps`` 对 NaN/Infinity 会写出
    ``NaN``/``Infinity`` 字面量（非 JSON 标准），下游 ``json.loads`` 虽能容错
    但其它解析器（以及人工 grep 后的管道处理）会炸。故在此统一归一。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, digits)


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
        config_path: Optional[str] = None,       # P1-9：配置文件路径（用于「配置过期」监视）
    ) -> None:
        self._cfg = cfg
        # P1-9（2026-09-01）：进程只在启动时读一次配置/代码。记录启动时刻，
        # 供「配置被改但没重启」的告警比对。实测事故：进程 20:55:39 启动，
        # 当晚 4 个改动全在它之后 → P0-4 / D2-C 全部静默不生效。
        self._config_path = config_path
        self._proc_started_at = datetime.now()
        self._config_stale_warned = False
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
        self._seen_trade_ids: set[str] = set()  # 成交去重哨兵（按 trade_id，防御重入/重复 append 致聚合计数翻倍）
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
        # ---- P0-1（2026-09-01）指纹生命周期：TTL + 当日重开预算 + 跨窗口持久化 ----
        # 背景取证：08-31 夜盘 rb0 停摆 1.94h（117 次拦截）、09-01 日盘停摆 2.36h（127 次）。
        # 根因：信号缓存日内恒定 → 指纹 (p_up, exp_ret, source) 永不变化 → 一旦平仓，
        # 冷却变成「当日永不再开」。故冷却必须由**时间**而非仅「信号是否变化」解除。
        sc_cfg = cfg.get("signal_cooldown", {}) if hasattr(cfg, "get") else {}
        self._signal_cooldown_enabled = bool(sc_cfg.get("enabled", True))
        self._signal_cooldown_p_up_tol = float(sc_cfg.get("p_up_tol", 0.01))
        self._signal_cooldown_exp_ret_tol = float(sc_cfg.get("exp_ret_tol", 0.001))
        # ⛔ TTL 不可为 0：那等于退回 P0-2 之前的 60s 开-平-开-平循环。
        # 30 分钟 ≈ 日内 2~4 次重开机会（视剩余交易时长），足以跟上趋势又压住抖动。
        self._signal_cooldown_ttl_min = float(sc_cfg.get("ttl_minutes", 30.0))
        # 当日同一指纹最多重开次数（TTL 到期后的第二道闸，防无限循环）；0/负 = 不限制。
        self._signal_cooldown_max_reentries = int(sc_cfg.get("max_reentries_per_day", 3))
        self._signal_cooldown_persist = bool(sc_cfg.get("persist", True))
        # 每品种上一轮「实际开仓」的冷却记录（指纹 + 开仓时刻 + 交易日 + 重开计数）
        self._last_sig_fp: dict[str, CooldownRecord] = {}
        self._cooldown_file = Path(cfg.get("cooldown_file", "data/paper/cooldown.json"))
        # 三窗口重启（08:55 / 13:25 / 20:55）续跑：重启本身**不重置**冷却，否则
        # 一天三次重启 = 三次免费重开，TTL 形同虚设。
        self._load_cooldown_state()

        # ---- P1-1 决策 trace 去重指纹：(reason, target_qty, position) ----
        # 仅存内存，三窗口重启后自然重打基线（刻意行为，见 _emit_decision_trace 文档）。
        self._trace_fp: dict[str, tuple] = {}

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
            # P1-9：配置文件在进程启动后被改动 → 告警（只报一次，且绝不阻断主循环）
            try:
                self._warn_if_config_stale()
            except Exception:  # noqa: BLE001 —— 看门狗不得影响交易主循环
                pass
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

    def _warn_if_config_stale(self) -> None:
        """P1-9：配置文件在**本进程启动之后**被改动 → 告警一次。

        背景（2026-09-01 实测）：长跑进程只在启动时读一次配置。当晚
        ``configs/paper.yaml`` 在进程启动（20:55:39）之后被改了 4 次，
        日志里却一直打着旧值，差点被误判成「改动有 bug」。
        有了这条告警，「改了没生效」就从**隐性事实**变成**可见事件**。

        ⛔ 只报一次：避免每个 tick 刷屏。
        ⛔ 只监视**配置文件**：源码改动（.py）同样不会热加载，但扫描源码树
        代价高且噪音大；判「代码是否过期」请看 banner 里的进程启动时间。
        """
        if self._config_stale_warned or not self._config_path:
            return
        p = Path(self._config_path)
        if not p.exists():
            return
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
        if mtime <= self._proc_started_at:
            return
        self._config_stale_warned = True
        log.warning(
            "P1-9 配置已过期：{} 于 {} 被修改，晚于本进程启动时间 {} —— "
            "配置**只在启动时读取**，改动不会生效，需重启进程",
            self._config_path,
            mtime.strftime("%Y-%m-%d %H:%M:%S"),
            self._proc_started_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _print_banner(self) -> None:
        log.info("=" * 64)
        log.info("模拟盘交易系统已启动")
        # P1-9：显式打出进程启动时刻 —— 判「某改动是否在本进程生效」的唯一依据
        # （比对文件 mtime 与它即可）。不要靠日志里有没有「已恢复/已启动」字样判断。
        log.info("  进程启动: {}（配置与代码均在此刻读取，之后改动需重启）",
                 self._proc_started_at.strftime("%Y-%m-%d %H:%M:%S"))
        if self._config_path:
            _cp = Path(self._config_path)
            log.info("  配置: {} (mtime {})", self._config_path,
                     datetime.fromtimestamp(_cp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                     if _cp.exists() else "文件不存在")
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
    # P1-1 决策 trace（归因地基）
    # ------------------------------------------------------------------
    def _emit_decision_trace(
        self,
        symbol: str,
        now: datetime,
        day: date,
        sig: SignalFrame,
        quote: Quote,
        acct: AccountSnapshot,
        pos_ctx: PositionCtx,
        decision: Any,
        plan: Any,
        atr: float,
        ma_price: Optional[float],
        mode: str = "trade",
    ) -> None:
        """落一条结构化决策快照到审计流（与 TRADE/PLAN 同一 sink）。

        背景（2026-09-01 实证）：09:05:57 rb0 开多、09:06:58 即被平，日志里只有
        ``TRADE|`` 两行，**完全看不出是谁下的平仓令**——``decision.reason`` 此前
        只在「成本门禁告警」和「冷却拦截」两处被打印，风控驱动的平仓零留痕。
        离线复现又与实盘矛盾 → 归因无解。本方法即补这块地基。

        触发条件（满足其一即写）：
          ① ``decision.reason != "rl_intent"`` —— 走了非默认路径（signal_cooldown /
             halt_no_open / min_hold / cost_gate_reject / hard_stop / S1–S5 /
             回撤 R1–R4 / 预算 / no_intent …）；
          ② ``plan.target_qty`` 与当前持仓不一致 —— 本轮将产生真实委托。

        去重（稳态压缩）：指纹 = ``(reason, target_qty, position)``，与本品种上一条
        已写出的指纹相同则跳过。目的：60s tick 下的稳态重复（今日 127 次
        ``signal_cooldown`` 拦截）压缩成 1 条；而**任何跃迁都不会被吞**——指纹一变
        必写，所以「第一次出现某原因」的时刻永远在日志里。

        ⚠️ 指纹仅存内存：三窗口重启（08:55 / 13:25 / 20:55）后会各重打一条基线，
        这是刻意的——正好标记新进程起点，也便于对齐「重启丢失冷却指纹」类缺陷。
        """
        reason = str(getattr(decision, "reason", "") or "no_intent")
        target_qty = _num(getattr(plan, "target_qty", 0.0), 6) or 0.0
        position = _num(getattr(pos_ctx, "position", 0.0), 6) or 0.0
        changed = abs(target_qty - position) > 1e-9

        if reason == "rl_intent" and not changed:
            return  # 稳态默认路径：无信息量，不写

        fp = (reason, round(target_qty, 6), round(position, 6))
        if self._trace_fp.get(symbol) == fp:
            return

        # 故障隔离：trace 是**观测设施**，绝不允许拖垮交易 tick（与 _evaluate_degradation
        # 同款护栏）。失败按 ERROR 落一笔并继续撮合；且指纹在写出成功后才更新，
        # 故失败会**每轮重试**——故障显性，不会静默丢失一条决策。
        try:
            payload = self._build_trace_payload(
                symbol=symbol, now=now, day=day, sig=sig, quote=quote, acct=acct,
                pos_ctx=pos_ctx, decision=decision, plan=plan, atr=atr,
                ma_price=ma_price, mode=mode, reason=reason, target_qty=target_qty,
                position=position, changed=changed, halt=bool(self._halt),
            )
            # P0-1：冷却状态随 trace 一起落盘。只有 reason 时看不出「还要等多久」——
            # 今日 rb0 停摆 2h23m，光看 reason=signal_cooldown 无法判断是等 TTL 还是已锁死。
            payload.update(self._cooldown_trace_fields(symbol, now, day))
            log_structured(EVT_DECISION_TRACE, payload)
        except Exception:
            log.exception("决策 trace 落盘异常（已隔离，不影响交易 tick） symbol={}", symbol)
            return
        self._trace_fp[symbol] = fp

    @staticmethod
    def _build_trace_payload(
        *,
        symbol: str,
        now: datetime,
        day: date,
        sig: Any,
        quote: Any,
        acct: Any,
        pos_ctx: Any,
        decision: Any,
        plan: Any,
        atr: float,
        ma_price: Optional[float],
        mode: str,
        reason: str,
        target_qty: float,
        position: float,
        changed: bool,
        halt: bool,
    ) -> dict[str, Any]:
        """构造 trace payload（纯函数：不读 self，便于单测与故障隔离）。"""
        # ⚠️ 两个枚举基类不同，序列化方式必须分开：
        #   RecoveryStage 是 StrEnum（value="R0".."R4"，本身可读）→ 取 value；
        #   ATRTier 是 IntEnum（value=0/1/2，裸数字不可读）→ 取 name（"HIGH"/"MID"/"LOW"）。
        # ⚠️ 且**绝不可写 ``x or ""``**：ATRTier.HIGH.value == 0 是 falsy，会被吞成空串，
        #    恰好把最常见也最保守的那一档记录成空白（初版实现踩过）。
        stage_raw = getattr(decision, "stage", None)
        tier_raw = getattr(decision, "atr_tier", None)
        stage_val = getattr(stage_raw, "value", stage_raw)
        tier_val = getattr(tier_raw, "name", tier_raw)
        return {
            "ts": now.isoformat(timespec="seconds"),
            "symbol": symbol,
            "day": day.isoformat(),
            "mode": mode,
            # ---- 决策本体 ----
            "reason": reason,
            "liquidate": bool(getattr(decision, "liquidate", False)),
            "stage": "" if stage_val is None else str(stage_val),
            "atr_tier": "" if tier_val is None else str(tier_val),
            "sell_signals": [
                str(getattr(s, "value", s)) for s in (getattr(decision, "sell_signals", None) or [])
            ],
            "kelly_fraction": _num(getattr(decision, "kelly_fraction", 0.0), 6),
            # ---- 仓位 ----
            "target_position": _num(getattr(decision, "target_position", 0.0), 6),
            "target_qty": target_qty,
            "position": position,
            "position_changed": changed,
            "stop_plan": _num(getattr(plan, "stop_price", None), 4),
            "stop_decision": _num(getattr(decision, "stop_price", None), 4),
            "tp": _num(getattr(plan, "take_profit", None), 4),
            # ---- 价格 / 指标（口径实录，P0-2/P0-3 复权 vs 名义价取证用） ----
            "price": _num(getattr(quote, "price", None), 4),
            "entry_price": _num(getattr(pos_ctx, "entry_price", None), 4),
            "atr": _num(atr, 4),
            "ma_price": _num(ma_price, 4),
            "bars_in_position": int(getattr(pos_ctx, "bars_in_position", 0) or 0),
            # ---- 信号 ----
            "p_up": _num(getattr(sig, "p_up", None), 6),
            "exp_ret": _num(getattr(sig, "exp_ret", None), 6),
            "sig_source": str(getattr(sig, "source", "") or ""),
            "sig_effective": bool(getattr(sig, "is_effective", False)),
            # ---- 账户 ----
            "equity": _num(getattr(acct, "equity", None), 2),
            "drawdown": _num(getattr(acct, "drawdown", None), 6),
            "peak_equity": _num(getattr(acct, "peak_equity", None), 2),
            "cash": _num(getattr(acct, "cash", None), 2),
            # ---- 全局态 ----
            "halt": bool(halt),
        }

    def _cooldown_trace_fields(self, symbol: str, now: datetime, day: Optional[date]) -> dict[str, Any]:
        """P0-1：冷却状态的两位观测字段（供决策 trace 携带）。

        返回 ``cooldown_remaining_min``（距可重开还剩几分钟，0 = 已到期/不在冷却）
        与 ``cooldown_reentries``（当日该指纹已重开次数）。无冷却记录时两者均为 ``None``。

        属性一律 ``getattr`` 取默认值：本方法会被 ``_risk_manage_only`` 路径调用，
        而该路径存在用 ``SimpleNamespace`` 假 self 绑定的单测（不构造完整 Scheduler），
        硬属性访问会抛 AttributeError 并被 emitter 的故障隔离吞掉 → trace 静默丢失。
        """
        rec = getattr(self, "_last_sig_fp", {}).get(symbol)
        if rec is None:
            return {"cooldown_remaining_min": None, "cooldown_reentries": None}
        ttl = float(getattr(self, "_signal_cooldown_ttl_min", 0.0) or 0.0)
        remaining_min = (rec.opened_at + timedelta(minutes=ttl) - now).total_seconds() / 60.0
        same_day = (
            self._same_trading_day(rec.day, day)
            if hasattr(self, "_same_trading_day")
            else True
        )
        return {
            "cooldown_remaining_min": _num(max(0.0, remaining_min), 2),
            "cooldown_reentries": rec.reentries if same_day else 0,
        }

    # ------------------------------------------------------------------
    # 单品种管道：quote → signal → risk → plan → execute → log
    # ------------------------------------------------------------------
    def _process_symbol(self, symbol: str, now: datetime, quote: Optional[Quote], marks: dict[str, float]) -> None:
        if quote is None or not quote.valid():
            log.warning("品种 {} 无有效行情，跳过", symbol)
            return
        # P2-3 报价时效校验：单报价年龄 = now - quote.ts 超阈值视为过期，拒绝撮合（与 P0-2 HALT 互补）。
        #
        # ⛔ 阈值同源（2026-09-04 团队裁决）：本阈值（配置键 quote_max_age_sec，
        # configs/paper.yaml:188 = 180s）**必须**与 quotes.py 的
        # QUOTE_MAX_STALENESS_SEC 相等。二者是两个独立时效门：
        #   - 本处 → 决定**是否允许撮合**（超阈值直接 return，不成交）；
        #   - quotes 层 → 决定**盯市估值**是否判陈旧（退回成本价）。
        # 二者不等时，(min, max] 窗口会出现「允许成交但按成本价估值」的不一致态。
        # → 改任一侧必须同步改另一侧，并跑 tests/test_paper_quotes.py。
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

        # ---- P0-2 信号无变化冷却 + P0-1 指纹生命周期（消除 60s 开-平-开-平循环） ----
        # 仅拦截「当前无持仓 + 风控意图开仓 + 信号指纹与上一轮实际开仓相同」；
        # 已有持仓的风控动作（止损/止盈/S1-S5 平仓）永远正常走 evaluate，不受影响。
        #
        # P0-1 两道闸（缺一不可）：
        #   ① TTL：距上次开仓不足 ttl_minutes → 拦截。到期即放行，避免「信号日内恒定 →
        #      平仓后当日永不再开」的停摆（取证：rb0 08-31 停摆 1.94h / 09-01 停摆 2.36h）。
        #   ② 当日重开预算：TTL 到期但同交易日重开次数已达上限 → 拦截。
        #      只加 TTL 会让 60s 循环以 TTL 为周期复活，预算是该循环的硬顶。
        # ⛔ 不得改为「平仓即清指纹」：历史日志中 open→60s→close 反复出现
        #      （08-24 14:17/14:18、08-31 21:01/21:02、09-01 09:05/09:06），清指纹会直接复活该循环。
        if (
            self._signal_cooldown_enabled
            and abs(pos_ctx.position) < 1e-12
            and abs(decision.target_position) > 1e-9
        ):
            fp = self._signal_fingerprint(sig)
            prev = self._last_sig_fp.get(symbol)
            if prev is not None and self._signal_fp_same(prev.fp, fp):
                elapsed_min = (now - prev.opened_at).total_seconds() / 60.0
                reentries = prev.reentries if self._same_trading_day(prev.day, day) else 0
                if elapsed_min < self._signal_cooldown_ttl_min:
                    log.info(
                        "品种 {} 信号未变化（p_up={:.4f} exp_ret={:.4f} src={}），冷却拦截："
                        "距上次开仓 {:.1f}min < TTL {:.0f}min（剩余 {:.1f}min）",
                        symbol, sig.p_up, sig.exp_ret, sig.source,
                        elapsed_min, self._signal_cooldown_ttl_min,
                        self._signal_cooldown_ttl_min - elapsed_min,
                    )
                    decision.target_position = 0.0
                    decision.reason = "signal_cooldown"
                elif (
                    self._signal_cooldown_max_reentries > 0
                    and reentries >= self._signal_cooldown_max_reentries
                ):
                    log.info(
                        "品种 {} 冷却已到期但当日重开预算耗尽（{}/{}，交易日 {}），拦截重开",
                        symbol, reentries, self._signal_cooldown_max_reentries, day,
                    )
                    decision.target_position = 0.0
                    decision.reason = "signal_cooldown_budget"

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
        # P0-4：传入 atr —— 决策无 stop_price 时，风险预算法用它兜底定价单笔风险。
        plan = self._planner.update_from_signal(
            sig, decision, quote=quote, equity=acct.equity, atr=atr
        )

        # ---- D-5 补强（2026-09-02）：sizing 层归零也计入拦截原因 ----
        self._record_sizing_block(symbol, day, decision, plan, pos_ctx, quote, acct, atr)

        # ---- P1-1 决策 trace（在撮合前落盘，冻结"决策当下"的全部上下文）----
        # 位置刻意选在 update_from_signal 之后、execute_plan 之前：既含风控原始决策
        # （decision）也含换算后的手数（plan.target_qty），且不受撮合结果影响。
        self._emit_decision_trace(
            symbol, now, day, sig, quote, acct, pos_ctx,
            decision, plan, atr, ma_price, mode=mode,
        )

        # ---- 撮合 ----
        event = self._broker.execute_plan(plan, quote, now)
        if event is not None:
            self._record_trade(event, day)
            # P0-2：仅在实际开仓时更新指纹（预算拒绝/冷却拦截不更新 → 下次同信号仍可重试或继续拦截）
            # P0-1：指纹升级为 CooldownRecord —— 记录开仓时刻（TTL 起算点）、交易日标签、
            #       当日重开计数，并立即原子落盘（三窗口重启不丢状态）。
            if (
                self._signal_cooldown_enabled
                and event.is_open
                and abs(pos_ctx.position) < 1e-12
            ):
                prev = self._last_sig_fp.get(symbol)
                new_fp = self._signal_fingerprint(sig)
                # 重开计数：指纹**且**交易日都相同才累加；换信号或换交易日 → 归零（新交易机会）
                carry = (
                    prev is not None
                    and self._signal_fp_same(prev.fp, new_fp)
                    and self._same_trading_day(prev.day, day)
                )
                self._last_sig_fp[symbol] = CooldownRecord(
                    fp=new_fp,
                    opened_at=now,
                    day=day.isoformat() if day is not None else "",
                    reentries=(prev.reentries + 1) if carry else 0,
                )
                self._save_cooldown_state()

    # ------------------------------------------------------------------
    # D-5：sizing 层拦截归因（补 C2 的统计盲区）
    # ------------------------------------------------------------------
    def _record_sizing_block(
        self,
        symbol: str,
        day: Any,
        decision: Any,
        plan: Any,
        pos_ctx: Any,
        quote: Any,
        acct: Any,
        atr: Optional[float],
    ) -> None:
        """sizing 层归零也计入 ``_block_reasons``（2026-09-02，QA 验证的实测缺口）。

        补 C2 的统计盲区：``_process_symbol`` 里第一路统计只认「风控意图 = 0」
        （``abs(decision.target_position) < 1e-9``）；而 sizing 归零（risk_budget /
        margin_cap / min_lot_threshold）发生在 planner 层 —— 此时
        ``decision.target_position`` **非零**（风控想开仓），那路统计不成立，
        C2 的「0 开仓原因分布」永远看不到 risk_budget / margin_cap。
        09-01 实证：ag0 被 risk_budget 拦 14 次，C2 全库仅见 rl_intent /
        signal_cooldown。最坏场景 —— 所有品种都「风控想开、但手数算出 0」——
        ``_block_reasons`` 为空，``_warn_zero_open`` 直接 return，**彻底静默**；
        叠加 P1 把 ag0 的每轮 WARNING 降为每日 1 条 INFO 后，静默更彻底。

        仅在「无持仓 + 风控想开 + 手数算出 0」三条件同时成立时补记，
        归因取 ``size_metrics()["capped_by"]``（纯计算、无副作用，只在归零
        轮次多一次微秒级计算）。异常完全隔离，绝不影响交易主流程。
        """
        try:
            if not (
                abs(pos_ctx.position) < 1e-12
                and abs(float(getattr(decision, "target_position", 0.0))) > 1e-9
                and plan.target_qty < 1e-9
            ):
                return
            _m = self._planner.size_metrics(
                symbol,
                abs(float(getattr(decision, "target_position", 0.0))),
                quote.price,
                acct.equity,
                stop=getattr(decision, "stop_price", None),
                atr=atr,
            )
            _reason = str(_m.get("capped_by") or "sized_zero")
            _bucket = self._block_reasons.setdefault(day, {})
            _bucket[_reason] = _bucket.get(_reason, 0) + 1
        except Exception:
            log.exception("sizing 拦截原因统计失败（已隔离）symbol={}", symbol)

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
        """跟踪 c0 盘中 OHLC（收盘时落盘日线快照）。

        P0-1 连带修正：``quote.price`` 语义已由 field[2] 开盘价改为 field[8]
        最新价，故**首轮撮出的 open 不再是当日开盘价**（而是首个轮询时刻的
        最新价）。此处显式取 ``quote.open``（field[2] 真开盘价），保留原语义；
        ``or quote.price`` 仅为字段缺失时的兜底。
        """
        open_px = quote.open if quote.open > 0 else quote.price
        stats = self._c0_intraday.setdefault(
            day, {"open": open_px, "high": open_px, "low": open_px, "close": quote.price}
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
    def _record_trade(self, event: "TradeEvent", day: date) -> None:
        """记录成交到内存聚合器，按 ``trade_id`` 对称防御性去重。

        同时保护 ``_day_trades`` / ``_all_trades`` 与审计日志，避免同一事件被
        重复记录（潜在重入 / 重复调用导致评估期成交计数翻倍）。

        ⚠️ 边界：``trade_id`` 为单调序列（``T{n:06d}``，见 ``broker.py``），
        本守卫仅覆盖「同一 event 对象被重复记录」类；真正的重入执行
        （``execute_plan`` 产生新 trade_id）需由 ``execute_plan`` 幂等性保障，
        属独立设计项，不在本防御范围内。
        """
        if event.trade_id in self._seen_trade_ids:
            return
        self._seen_trade_ids.add(event.trade_id)
        self._day_trades.setdefault(day, []).append(event)
        self._all_trades.append(event)
        self._logger.trade(event)

    def _risk_manage_only(self, symbol: str, quote: Quote, marks: dict[str, float], day: date, now: datetime) -> None:
        sig = self._signals.neutral_signal(symbol, now)
        self._day_signals.setdefault(day, []).append(sig)
        acct = self._broker.snapshot(marks)
        atr = self._default_atr_pct * quote.price
        pos_ctx = self._broker.position_ctx(symbol, quote, atr)
        # ③ 加固：与 _process_symbol 对齐，显式透传 ma_price。
        # 否则 RiskState.ma_price=None → sell_engine S1（趋势破坏止损）在「仅风控」模式下被整体跳过。
        bars = self._cached_bars(symbol, day)
        _, _, ma_price = self._aux_from_bars(bars)
        decision = self._risk_gate.evaluate(sig, quote, acct, pos_ctx, ma_price=ma_price)
        # P0-4：同上，透传 atr 供风险预算法兜底定价。
        plan = self._planner.update_from_signal(
            sig, decision, quote=quote, equity=acct.equity, atr=atr
        )
        # P1-1 决策 trace：仅风控路径同样留痕（c0 accumulate 模式有持仓时走这里）。
        # 注意本路径 recent_returns/recent_volumes 为 None（S2 量价背离不参与）。
        self._emit_decision_trace(
            symbol, now, day, sig, quote, acct, pos_ctx,
            decision, plan, atr, ma_price, mode="risk_only",
        )
        event = self._broker.execute_plan(plan, quote, now)
        if event is not None:
            self._record_trade(event, day)

    # ------------------------------------------------------------------
    # 行情辅助
    # ------------------------------------------------------------------
    def _build_marks(self, quotes: dict[str, Quote]) -> dict[str, float]:
        """组合所有品种的最新价（无报价 / 报价陈旧时用持仓均价兜底）。

        P1-1：陈旧的报价（``stale=True``）**不得用于盯市**，fail-closed 退回成本价。
        P1-2：任何成本价兜底都必须告警 —— P0-1 事故中该兜底完全静默（实证：
        09-03 10:02:47 曾以开仓成本价 3137 盯市，日志无任何提示）。
        """
        marks: dict[str, float] = {}
        stale_syms: set[str] = set()
        for sym in self._symbols:
            q = quotes.get(sym)
            if q is None:
                continue
            if getattr(q, "stale", False):
                stale_syms.add(sym)
                continue
            if q.price > 0:
                marks[sym] = q.price
        for sym, pos in self._broker.broker.positions.items():
            if sym not in marks and abs(pos) > 1e-12:
                avg = self._broker.avg_entry(sym)
                if avg > 0:
                    marks[sym] = avg
                    log.warning(
                        "盯市兜底 symbol={} 无可用行情{}，改用开仓成本价 {:.4f}"
                        "（浮盈显示为 0，非真实市值）",
                        sym,
                        "（报价陈旧已剔除）" if sym in stale_syms else "",
                        avg,
                    )
        return marks

    @staticmethod
    def _quote_ts_summary(quotes: dict[str, Quote]) -> str:
        """逐品种「行情时间 + 陈旧标记」摘要（P1-3：让冻结在日志上直接可见）。"""
        if not quotes:
            return "无报价"
        parts: list[str] = []
        for sym in sorted(quotes):
            q = quotes[sym]
            flag = "陈旧" if getattr(q, "stale", False) else "新"
            ts = q.ts.strftime("%m-%d %H:%M:%S") if getattr(q, "ts", None) else "未知"
            parts.append(f"{sym}@{ts}({flag})")
        return " ".join(parts)

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
    # P0-1 冷却指纹生命周期（TTL / 当日重开预算 / 跨窗口持久化）
    # ------------------------------------------------------------------
    @staticmethod
    def _same_trading_day(stored_day: str, day: Optional[date]) -> bool:
        """持久化交易日标签与当前交易日标签是否同一交易日。

        ⚠️ 必须用**交易日标签**（``TradingSession.day_label``）而非自然日：夜盘 21:00 之后
        归属下一交易日，用自然日会把「夜盘 → 日盘」误判成两个交易日，导致当日重开预算被
        错误重置（08-31 21:01 与 09-01 09:05 在交易日口径下**同属 09-01**）。

        任一侧缺失（历史文件无 day 字段 / ``day_label`` 返回 None）时保守判为「同一日」，
        即**不**重置重开预算——宁可少开，不可多开。
        """
        cur = day.isoformat() if day is not None else ""
        if not stored_day or not cur:
            return True
        return stored_day == cur

    def _save_cooldown_state(self) -> None:
        """P0-1：原子落盘冷却状态（跨三窗口重启不丢失指纹 / 重开计数）。

        P1-B 合并写（2026-09-03）：逐品种按 ``opened_at`` **新者保留**。
        背景——三窗口切换存在 PID 锁缺口，长寿旧进程优雅退出时
        ``_shutdown → _save_cooldown_state`` 会用陈旧内存**整体覆写**磁盘
        （09-01 21:58:59 实测：08-24 ``--smoke`` 污染记录覆盖 14:30 真实开仓，
        次日 08:55 窗口恢复出 9 天前的陈旧冷却）。合并写双向往返防护：

        - 内存新、磁盘旧 → 正常落盘不被回退（P0-1 主路径不变）；
        - 内存旧、磁盘新 → 不把磁盘拖回过去（P1-B 主场景）。

        故障隔离：合并读取失败 → 退回整体写内存（fail-open）；落盘失败只告警，
        绝不中断交易 tick——冷却是节奏控制，不是账本，其丢失的最坏后果是
        可容忍的一次重复开仓，而中断 tick 的后果是当轮全品种停摆。
        """
        if not self._signal_cooldown_persist:
            return
        try:
            path = self._cooldown_file
            path.parent.mkdir(parents=True, exist_ok=True)

            # ---- P1-B 合并：磁盘上更新的记录优先保留（读失败退回内存态） ----
            merged: dict = dict(self._last_sig_fp)
            try:
                if path.exists():
                    disk = json.loads(path.read_text(encoding="utf-8"))
                    for sym, raw in (disk.get("records") or {}).items():
                        try:
                            fp_raw = raw["fp"]
                            disk_rec = CooldownRecord(
                                fp=(float(fp_raw[0]), float(fp_raw[1]), str(fp_raw[2])),
                                opened_at=datetime.fromisoformat(str(raw.get("opened_at"))),
                                day=str(raw.get("day", "") or ""),
                                reentries=int(raw.get("reentries", 0) or 0),
                            )
                        except Exception:
                            continue  # 单条损坏跳过，不拖垮整次落盘
                        cur = merged.get(str(sym))
                        # R26g QA 🟡1：naive/aware 混比较会抛 TypeError → 被外层
                        # except 吞掉 → 整次合并放弃 → fail-open 退回整体写内存，
                        # 恰好复刻 P1-B 要防的陈旧回滚。比较前统一归一化为 naive
                        # （系统自产 opened_at 恒 naive，aware 仅防御外来文件）。
                        cur_opened = cur.opened_at if cur is not None else None
                        if cur_opened is not None and cur_opened.tzinfo is not None:
                            cur_opened = cur_opened.replace(tzinfo=None)
                        disk_opened = disk_rec.opened_at
                        if disk_opened.tzinfo is not None:
                            disk_opened = disk_opened.replace(tzinfo=None)
                        if cur is None or disk_opened > cur_opened:
                            merged[str(sym)] = disk_rec
            except Exception:
                log.warning("冷却合并读取失败，退回整体写内存 path={}", path)

            payload = {
                "schema_version": "1.0",
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "records": {
                    sym: {
                        "fp": [rec.fp[0], rec.fp[1], rec.fp[2]],
                        "opened_at": rec.opened_at.isoformat(timespec="seconds"),
                        "day": rec.day,
                        "reentries": rec.reentries,
                    }
                    for sym, rec in merged.items()
                },
            }
            # G5 原子写：沙箱 safe-delete 钩子会拦截 unlink，直接覆盖会丢文件
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            log.exception(
                "冷却状态落盘失败（已隔离，不影响交易） path={}", self._cooldown_file
            )

    def _load_cooldown_state(self) -> None:
        """P0-1：启动时恢复冷却状态（三窗口重启续跑）。

        容错：文件不存在 / JSON 损坏 / 单条记录结构异常 → 跳过该记录或清空状态，
        绝不启动失败。语义上**重启不重置冷却**：TTL 起算点取持久化的 ``opened_at``。
        """
        if not self._signal_cooldown_persist:
            return
        path = self._cooldown_file
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"冷却状态顶层不是对象：{type(payload).__name__}")
            records = payload.get("records", {})
            if not isinstance(records, dict):
                raise ValueError(f"records 不是对象：{type(records).__name__}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            backup = path.with_name(f"{path.name}.corrupt.{datetime.now():%Y%m%d%H%M%S}")
            try:
                path.replace(backup)
            except OSError:
                pass
            log.exception("冷却状态文件损坏，已备份为 {} 并降级为空状态", backup.name)
            self._last_sig_fp = {}
            return

        restored: dict[str, CooldownRecord] = {}
        for sym, raw in records.items():
            try:
                if not isinstance(raw, dict):
                    raise ValueError(f"记录不是对象：{type(raw).__name__}")
                fp_raw = raw.get("fp")
                if not isinstance(fp_raw, (list, tuple)) or len(fp_raw) != 3:
                    raise ValueError(f"fp 非法：{fp_raw!r}")
                restored[str(sym)] = CooldownRecord(
                    fp=(float(fp_raw[0]), float(fp_raw[1]), str(fp_raw[2])),
                    opened_at=datetime.fromisoformat(str(raw.get("opened_at"))),
                    day=str(raw.get("day", "") or ""),
                    reentries=int(raw.get("reentries", 0) or 0),
                )
            except Exception:
                log.warning("冷却记录损坏已跳过 symbol={} raw={!r}", sym, raw)
        self._last_sig_fp = restored
        if restored:
            log.info(
                "冷却状态已恢复：{}",
                {k: f"{v.day}#{v.reentries}@{v.opened_at:%m-%d %H:%M}" for k, v in restored.items()},
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
                # P1-3：补打逐品种 mark 价与行情时间/陈旧标记，让「盯市冻结」
                # 在日志上直接可见（此前只打 equity/cash 两个数，冻结完全不可辨）。
                log.info(
                    "账户快照已保存 equity={:.2f} cash={:.2f} | marks={} | 行情={}",
                    acct.equity,
                    acct.cash,
                    {s: round(float(v), 4) for s, v in sorted(marks.items())} or "无持仓",
                    self._quote_ts_summary(quotes),
                )
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
        # P0-1：独立 try —— 快照失败不得连带丢掉冷却状态（重启后冷却被抹 = 免费重开）
        try:
            self._save_cooldown_state()
        except Exception:
            log.exception("停止时保存冷却状态失败")
        log.info("模拟盘已退出")