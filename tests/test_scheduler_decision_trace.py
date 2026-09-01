"""P1-1 决策 trace 单测（2026-09-01 实证缺口补漏）。

背景（2026-09-01 上午复盘）：09:05:57 rb0 开多、09:06:58 即被平仓，``trades.log``
里只剩两行 ``TRADE|``，**完全看不出平仓令是谁下的**——``decision.reason`` 此前只在
「成本门禁告警」与「冷却拦截」两处被打印，风控驱动的平仓零留痕，离线复现又与实盘
矛盾 → 归因无解。P1-1 补的就是这块地基：每次风控决策的**状态跃迁**落一条结构化快照。

覆盖：
① 开仓决策留痕（含信号/账户/指标全上下文）；
② 稳态持仓静默（不刷屏）；
③ **风控驱动平仓可归因**（核心：S1 触发 → reason 落盘）；
④ 冷却拦截留痕 + 60s 稳态重复去重（127 次 → 1 条）；
⑤ payload 必须是严格合法 JSON（NaN/Infinity → null）；
⑥ NaN 指标归一为 null；
⑦ 仅风控路径（accumulate 有持仓）同样留痕；
⑧ reason 跃迁矩阵（默认路径不写 / 非默认路径必写 / 同指纹去重）。

测试纪律：全部断言走**生产实现** ``TradingScheduler._process_symbol`` /
``_risk_manage_only`` / ``_emit_decision_trace``，本文件不含任何决策逻辑副本。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time

import pandas as pd
from omegaconf import OmegaConf

from hexbroker.backtest.cost import CostModel
from hexbroker.paper import scheduler as scheduler_mod
from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.intel import IntelligenceService, StaticNewsProvider
from hexbroker.paper.logger import TradeLogger
from hexbroker.paper.planner import PlanManager
from hexbroker.paper.reporter import ReviewReporter
from hexbroker.paper.risk_gate import RiskGate
from hexbroker.paper.scheduler import TradingScheduler
from hexbroker.paper.sessions import TradingSession, parse_sessions
from hexbroker.paper.types import (
    EVT_DECISION_TRACE,
    AccountSnapshot,
    Plan,
    PositionCtx,
    Quote,
    SignalFrame,
)
from hexbroker.risk.types import RiskDecision

# 单一连续早盘时段（09:00–11:30）：便于连续 tick 不落进小节休息，
# 否则 127 轮稳态去重测试会有大量 tick 被 is_tradable 早返回，削弱断言强度。
_DAY = [["09:00", "11:30"], ["13:30", "15:00"]]
MA20 = 3037.0
PRICE = 3038.0
NOW = datetime(2026, 8, 24, 10, 0)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
class TraceCapture:
    """替换 ``scheduler.log_structured``：记录全部结构化事件（不改写生产逻辑）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def traces(self) -> list[dict]:
        return [p for e, p in self.events if e == EVT_DECISION_TRACE]

    def reasons(self) -> list[str]:
        return [str(t["reason"]) for t in self.traces()]


class Quotes:
    """固定行情 + 固定 K 线（close 恒定 → MA20 = MA20 常量，便于构造 S1）。"""

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {
            s: Quote(
                symbol=s, ts=NOW, price=PRICE,
                open=PRICE - 8, high=PRICE + 1, low=PRICE - 1, pre_settle=PRICE - 8,
            )
            for s in symbols
        }

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120) -> pd.DataFrame:
        n = 25
        idx = pd.date_range("2026-07-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "open": [MA20] * n,
                "high": [MA20 + 20.0] * n,
                "low": [MA20 - 20.0] * n,
                "close": [MA20] * n,
                "volume": [1000.0] * n,
            },
            index=idx,
        )


class Signals:
    """可切换的主源信号（.sig 可运行时替换，用于构造方向反转）。"""

    def __init__(self, sig: SignalFrame) -> None:
        self.sig = sig
        self.freshness_threshold = 0

    def latest_signal(self, symbol: str, asof=None):  # noqa: ANN001
        return self.sig

    def technical_fallback(self, symbol: str, bars):  # noqa: ANN001
        return None

    def neutral_signal(self, symbol: str, asof=None):  # noqa: ANN001
        from hexbroker.paper.signals import SignalEngine

        return SignalEngine.neutral_signal(symbol, asof)


def _sig(p_up: float, exp_ret: float = 0.5) -> SignalFrame:
    """构造主源信号。

    ⚠️ ``exp_ret`` 必须与 ``p_up`` 推出的方向**同号**：``p_up < 0.5 → direction = -1``，
    此时 ``exp_ret`` 必须为负，否则 ``RiskGate._cost_gate_pass`` 的「方向一致性」
    校验（risk_gate.py L223-226）会直接拦截，开仓根本不会发生（`expected_pnl` 取 0）。
    """
    return SignalFrame(
        symbol="rb0", ts=NOW, p_up=p_up, exp_ret=exp_ret,
        is_effective=True, source="engine_a", freshness_days=0,
    )


def _cfg() -> OmegaConf:
    return OmegaConf.create(
        {
            "initial_capital": 300_000.0,
            "budget_ratio": 0.30,
            "poll_interval_sec": 60,
            "intel_interval_sec": 1800,
            "snapshot_interval_sec": 300,
            "open_delay_min": 5,
            "evaluation_days": 20,
            "default_atr_pct": 0.01,
            "freshness_threshold_trading_days": 0,
            "quote_fail_halt_threshold": 3,
            "symbols": {
                "rb0": {
                    "display": "SHFE.rb",
                    "sina_code": "nf_RB0",
                    "multiplier": 10,
                    "min_tick": 1,
                    "mode": "trade",
                    "sessions": {"day": _DAY, "night": [["21:00", "23:00"]]},
                }
            },
            "holidays_2026": [],
            "risk_gate": {"cost_gate_enabled": True, "cost_gate_min_ratio": 2.0},
            "signal_cooldown": {"enabled": True, "p_up_tol": 0.01, "exp_ret_tol": 0.001},
        }
    )


def _risk_gate() -> RiskGate:
    """S1 门槛压到最宽松（min_bars=1 / band=0），便于确定性触发趋势破坏。"""
    cfg = OmegaConf.create(
        {
            "risk": {
                "vol_target": 0.5,
                "kelly_cap": 1.0,
                "max_position_pct": 0.5,
                "recovery_drawdown_r1": 0.05,
                "recovery_drawdown_r2": 0.10,
                "recovery_drawdown_r3": 0.15,
                "position_scalar_r1": 0.5,
                "position_scalar_r2": 0.0,
                "position_scalar_r3": 0.2,
                "position_scalar_r4": 1.0,
                "vol_low_q": 0.2,
                "vol_high_q": 0.8,
                "sell_s1_min_bars": 1,
                "sell_s1_band_atr": 0.0,
            }
        }
    )
    return RiskGate(cfg, hard_stop=0.20)


def _scheduler(tmp_path, signals: Signals) -> TradingScheduler:
    paper_cfg = _cfg()
    paper_cfg.account_file = str(tmp_path / "account.json")
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    # P0-1：冷却状态落盘路径指向 tmp，避免单测写脏生产 data/paper/cooldown.json
    paper_cfg.cooldown_file = str(tmp_path / "cooldown.json")
    session = TradingSession(
        symbol_sessions={"rb0": parse_sessions(_DAY, [["21:00", "23:00"]])},
        holidays=set(),
        night_boundary=time(21, 0),
    )
    cost = CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={"rb0": {"multiplier": 10.0, "min_tick": 1.0}},
    )
    broker = PaperBroker(cost, initial_capital=float(paper_cfg.initial_capital), budget_ratio=0.30, data_dir=str(tmp_path))
    return TradingScheduler(
        cfg=paper_cfg,
        session=session,
        quotes=Quotes(),
        signals=signals,
        risk_gate=_risk_gate(),
        planner=PlanManager(multipliers={"rb0": 10.0}, plans_dir=str(tmp_path / "plans")),
        broker=broker,
        intel=IntelligenceService(
            providers=[StaticNewsProvider(path=str(tmp_path / "news.json"))],
            keywords={"rb0": ["螺纹"]},
            risk_keywords=["风险"],
        ),
        logger=TradeLogger(log_file=str(tmp_path / "trades.log")),
        reporter=ReviewReporter(reports_dir=str(tmp_path / "reports"), symbols_display={"rb0": "SHFE.rb"}),
    )


def _tick(sched: TradingScheduler, now: datetime) -> None:
    """驱动一轮单品种管道（生产入口，无逻辑副本）。"""
    sched._process_symbol("rb0", now, Quotes().fetch_quotes(["rb0"])["rb0"], {"rb0": PRICE})


# ---------------------------------------------------------------------------
# ① 开仓决策留痕
# ---------------------------------------------------------------------------
def test_open_emits_trace_with_full_context(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.7)))

    _tick(sched, NOW)

    assert len(cap.traces()) == 1, f"期望 1 条 trace，实际 {cap.reasons()}"
    t = cap.traces()[0]
    # 身份与决策
    assert t["symbol"] == "rb0"
    assert t["day"] == "2026-08-24"
    assert t["reason"] == "rl_intent"
    assert t["position_changed"] is True
    assert t["target_qty"] > 0 and t["position"] == 0.0
    # 上下文齐全（缺一项归因就断链）
    for key in ("p_up", "exp_ret", "sig_source", "sig_effective", "equity",
                "drawdown", "price", "atr", "ma_price", "bars_in_position",
                "stop_plan", "stop_decision", "tp", "target_position", "liquidate"):
        assert key in t, f"trace 缺字段：{key}"
    assert t["p_up"] == 0.7 and t["sig_effective"] is True
    assert t["ma_price"] == MA20  # _aux_from_bars 的 20 日均（复权口径，P0-2 取证锚点）
    # 枚举序列化防回归：ATRTier 是 IntEnum 且 HIGH.value == 0（falsy）。
    # 初版写 `getattr(tier, "value", tier) or ""`，把最常见的高波动档吞成了空串。
    assert t["stage"] == "R0"
    assert t["atr_tier"] == "HIGH", f"atr_tier 被 falsy 吞掉：{t['atr_tier']!r}"


# ---------------------------------------------------------------------------
# ② 稳态持仓静默（不刷屏）
# ---------------------------------------------------------------------------
def test_steady_hold_is_silent(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.7)))

    for i in range(10):
        _tick(sched, NOW.replace(minute=i))  # 多头：价格 3038 > MA 3037 → 不触发 S1

    # 只有开仓那一条（持仓稳态既不改 target 也不是非默认 reason）
    assert len(cap.traces()) == 1, f"稳态刷屏了：{cap.reasons()}"
    assert cap.reasons() == ["rl_intent"]


# ---------------------------------------------------------------------------
# ③ 核心：风控驱动平仓可归因（2026-09-01 缺的就是这条）
# ---------------------------------------------------------------------------
def test_risk_driven_close_is_attributable(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    # p_up<0.5 → 方向 -1（空头）；价格 3038 > MA 3037 + band(0) → S1 趋势破坏必触发
    sched = _scheduler(tmp_path, Signals(_sig(0.3, -0.5)))

    _tick(sched, NOW)                        # ① 开空
    _tick(sched, NOW.replace(minute=1))      # ② S1 强平

    reasons = cap.reasons()
    assert reasons[0] == "rl_intent", reasons
    assert "S1" in reasons, f"风控平仓未留痕：{reasons}"
    close_trace = cap.traces()[-1]
    assert close_trace["reason"] == "S1"
    assert close_trace["liquidate"] is True
    assert close_trace["position"] < 0 and close_trace["target_qty"] == 0.0
    assert close_trace["position_changed"] is True
    assert close_trace["sell_signals"] == ["S1"]


# ---------------------------------------------------------------------------
# ④ 冷却拦截留痕 + 稳态重复去重（今日 127 次 → 1 条）
# ---------------------------------------------------------------------------
def test_cooldown_trace_deduped_across_127_ticks(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.3, -0.5)))  # 空头 → 开后被 S1 平

    _tick(sched, NOW)                        # 开空（写指纹）
    _tick(sched, NOW.replace(minute=1))      # S1 平仓
    before = len(cap.traces())

    # 平仓后维持同一信号：应命中 signal_cooldown 并留一条痕
    _tick(sched, NOW.replace(minute=2))
    assert cap.reasons()[-1] == "signal_cooldown", cap.reasons()
    after_first_cooldown = len(cap.traces())

    # 再跑 127 轮同一稳态 → 不得再新增（否则 60s tick 下刷屏）
    for i in range(3, 130):
        _tick(sched, NOW.replace(minute=i % 60))  # 全部落在 10:xx（连续早盘时段内）

    assert len(cap.traces()) == after_first_cooldown, (
        f"稳态去重失效：{after_first_cooldown} → {len(cap.traces())}"
    )
    assert before + 1 == after_first_cooldown


# ---------------------------------------------------------------------------
# ⑤ payload 必须是严格合法 JSON（历史坑：NaN 写成 NaN 字面量）
# ---------------------------------------------------------------------------
def test_payload_is_strict_json(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.3, -0.5)))

    _tick(sched, NOW)
    _tick(sched, NOW.replace(minute=1))
    _tick(sched, NOW.replace(minute=2))

    assert len(cap.traces()) >= 3
    for t in cap.traces():
        # allow_nan=False：任何 NaN/Infinity 都会抛 ValueError
        raw = json.dumps(t, ensure_ascii=False, allow_nan=False)
        assert json.loads(raw)["symbol"] == "rb0"


# ---------------------------------------------------------------------------
# ⑥ NaN / Inf 指标归一为 null
# ---------------------------------------------------------------------------
def test_non_finite_metrics_become_null(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.7)))
    quote = Quotes().fetch_quotes(["rb0"])["rb0"]
    acct = AccountSnapshot(ts=NOW, equity=300_000.0, cash=300_000.0, margin_used=0.0)
    pos_ctx = PositionCtx(symbol="rb0", position=0.0, entry_price=0.0, atr=30.0)

    sched._emit_decision_trace(
        symbol="rb0", now=NOW, day=date(2026, 8, 24),
        sig=_sig(0.7), quote=quote, acct=acct, pos_ctx=pos_ctx,
        decision=RiskDecision(target_position=0.15, reason="hard_stop"),
        plan=Plan(symbol="rb0", direction=1, target_qty=1.0, target_pos_pct=0.15),
        atr=float("nan"), ma_price=float("inf"), mode="trade",
    )

    t = cap.traces()[-1]
    assert t["atr"] is None and t["ma_price"] is None, t
    assert t["reason"] == "hard_stop"


# ---------------------------------------------------------------------------
# ⑦ 仅风控路径（accumulate 有持仓）同样留痕
# ---------------------------------------------------------------------------
def test_risk_only_path_emits_trace(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.3, -0.5)))  # 空头 → 开空后被 S1 平

    _tick(sched, NOW)  # 先建仓，确保有持仓
    assert abs(sched._broker.position("rb0")) > 0

    sched._risk_manage_only("rb0", Quotes().fetch_quotes(["rb0"])["rb0"], {"rb0": PRICE}, date(2026, 8, 24), NOW)

    risk_only = [t for t in cap.traces() if t["mode"] == "risk_only"]
    assert risk_only, f"仅风控路径未留痕：{cap.reasons()}"
    assert risk_only[-1]["reason"] == "S1"
    assert risk_only[-1]["liquidate"] is True


# ---------------------------------------------------------------------------
# ⑧ reason 跃迁矩阵（默认路径不写 / 非默认路径必写 / 同指纹去重）
# ---------------------------------------------------------------------------
def test_reason_transition_matrix(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.7)))
    quote = Quotes().fetch_quotes(["rb0"])["rb0"]
    acct = AccountSnapshot(ts=NOW, equity=300_000.0, cash=300_000.0, margin_used=0.0)

    def emit(reason: str, target_qty: float, position: float) -> None:
        sched._emit_decision_trace(
            symbol="rb0", now=NOW, day=date(2026, 8, 24),
            sig=_sig(0.7), quote=quote, acct=acct,
            pos_ctx=PositionCtx(symbol="rb0", position=position, entry_price=PRICE, atr=30.0),
            decision=RiskDecision(target_position=0.15, reason=reason),
            plan=Plan(symbol="rb0", direction=1, target_qty=target_qty, target_pos_pct=0.15),
            atr=30.0, ma_price=MA20, mode="trade",
        )

    # ① 默认路径 + 无仓位变化 → 静默（60s tick 下这是绝大多数轮次）
    emit("rl_intent", target_qty=0.0, position=0.0)
    assert cap.traces() == []

    # ② 非默认路径 → 必写（这正是归因所需）
    emit("signal_cooldown", target_qty=0.0, position=0.0)
    emit("cost_gate_reject", target_qty=0.0, position=0.0)
    emit("min_hold", target_qty=0.0, position=0.0)
    emit("halt_no_open", target_qty=0.0, position=0.0)
    assert cap.reasons() == ["signal_cooldown", "cost_gate_reject", "min_hold", "halt_no_open"]

    # ③ 同指纹重复 → 去重（稳态压缩的核心）
    emit("halt_no_open", target_qty=0.0, position=0.0)
    emit("halt_no_open", target_qty=0.0, position=0.0)
    assert cap.reasons() == ["signal_cooldown", "cost_gate_reject", "min_hold", "halt_no_open"]

    # ④ 指纹任一分量变化 → 必写（跃迁不会被吞）
    emit("halt_no_open", target_qty=1.0, position=0.0)   # target 变
    emit("halt_no_open", target_qty=1.0, position=1.0)   # position 变
    assert cap.reasons()[-2:] == ["halt_no_open", "halt_no_open"]
    assert len(cap.traces()) == 6


# ---------------------------------------------------------------------------
# ⑨ 故障隔离：观测设施绝不能拖垮交易 tick
# ---------------------------------------------------------------------------
def test_trace_failure_does_not_break_tick(tmp_path, monkeypatch):
    cap = TraceCapture()
    monkeypatch.setattr(scheduler_mod, "log_structured", cap)
    sched = _scheduler(tmp_path, Signals(_sig(0.7)))

    def boom(**_kwargs):  # noqa: ANN001
        raise RuntimeError("simulated trace serialization failure")

    monkeypatch.setattr(type(sched), "_build_trace_payload", staticmethod(boom))

    _tick(sched, NOW)  # 不得抛出

    # ① 交易照常撮合（trace 是观测设施，失败不得中止 tick）
    assert abs(sched._broker.position("rb0")) > 0, "trace 异常拖垮了交易 tick"
    # 注：TRADE 审计走 logger.py 自己的 log_structured 引用，monkeypatch scheduler 抓不到，
    # 故改验成交记账（_record_trade 在 _emit_decision_trace 之后执行）。
    assert len(sched._all_trades) == 1, f"成交未记账：{len(sched._all_trades)}"
    # ② 指纹不更新 → 下一轮重试：故障显性，不会静默吞掉一条决策
    assert sched._trace_fp.get("rb0") is None
