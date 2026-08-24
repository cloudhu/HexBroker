"""P0-2 信号无变化冷却单测（消除 60s 开-平-开-平循环）。

覆盖：
① 信号未变 + 无持仓 → 冷却拦截，不开仓；
② 信号未变 + 有持仓 → 风控动作（S1 平仓）照常执行，冷却不拦截；
③ 信号变化 → 解除冷却，开仓并更新指纹；
④ 初始无指纹 → 允许开仓并记录指纹；
⑤ 信号源变化（source 不同）→ 视为信号变化；
⑥ 容差内微变 → 仍视为未变化，继续拦截。

场景（8/24 rb0 取证口径）：价格 3038↔3036 交替、MA20=3037、60s 步长；
持仓根数按轮次递增（模拟审计口径，每 60s tick 计入持仓根数）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.backtest.cost import CostModel
from hexbroker.paper.broker import PaperBroker
from hexbroker.paper.intel import IntelligenceService, StaticNewsProvider
from hexbroker.paper.logger import TradeLogger
from hexbroker.paper.planner import PlanManager
from hexbroker.paper.reporter import ReviewReporter
from hexbroker.paper.risk_gate import RiskGate
from hexbroker.paper.scheduler import TradingScheduler
from hexbroker.paper.sessions import TradingSession, parse_sessions
from hexbroker.paper.signals import SignalEngine
from hexbroker.paper.types import Plan, Quote, SignalFrame

MON = date(2026, 8, 24)
_DAY = [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]
MA20 = 3037.0


def _bars() -> pd.DataFrame:
    """22 根日线：close 恒定 3037 → MA20=3037；high-low=40 → ATR=40。"""
    n = 22
    closes = [MA20] * n
    highs = [MA20 + 20.0] * n
    lows = [MA20 - 20.0] * n
    idx = pd.date_range("2026-07-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1000.0] * n},
        index=idx,
    )


class CooldownQuotes:
    """离线 mock 行情：固定价 + 固定 K 线（MA20=3037 / ATR≈40）。"""

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime(2026, 8, 24, 10, 0)
        out: dict[str, Quote] = {}
        for s in symbols:
            out[s] = Quote(symbol=s, ts=now, price=3038.0, open=3030, high=3040, low=3030, pre_settle=3030)
        return out

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120):
        return _bars()


class CooldownSignals:
    """mock 信号引擎（信号可在测试中替换以模拟变化）。"""

    def __init__(self, sig: SignalFrame | None) -> None:
        self._sig = sig

    def latest_signal(self, symbol: str, asof):
        return self._sig

    def technical_fallback(self, symbol: str, bars):
        return None

    def neutral_signal(self, symbol: str, asof=None):
        return SignalEngine.neutral_signal(symbol, asof)


def _paper_cfg() -> OmegaConf:
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
            "account_file": "data/paper/account.json",
            "c0_daily_csv": "data/paper/c0_daily.csv",
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


def _cost() -> CostModel:
    return CostModel(
        fee_open=0.00005,
        fee_close=0.00005,
        fee_close_today=0.00010,
        slippage_ticks=1.0,
        margin_rate=0.12,
        multiplier=10.0,
        min_tick=10.0,
        contracts={"rb0": {"multiplier": 10.0, "min_tick": 1.0}},
    )


def _session() -> TradingSession:
    return TradingSession(
        symbol_sessions={
            "rb0": parse_sessions(_DAY, [["21:00", "23:00"]]),
        },
        holidays=set(),
        night_boundary=time(21, 0),
    )


def _risk_gate() -> RiskGate:
    """S1 无缓冲无带宽：价格跌破 MA20 即平仓（用于验证冷却不拦截持仓风控）。"""
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


def _scheduler(tmp_path, sig: SignalFrame | None) -> tuple[TradingScheduler, dict]:
    paper_cfg = _paper_cfg()
    paper_cfg.account_file = str(tmp_path / "account.json")
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    quotes = CooldownQuotes()
    signals = CooldownSignals(sig)
    risk_gate = _risk_gate()
    broker = PaperBroker(_cost(), initial_capital=float(paper_cfg.initial_capital), budget_ratio=0.30, data_dir=str(tmp_path))
    planner = PlanManager(multipliers={"rb0": 10.0}, plans_dir=str(tmp_path / "plans"))
    intel = IntelligenceService(
        providers=[StaticNewsProvider(path=str(tmp_path / "news.json"))],
        keywords={"rb0": ["螺纹"]},
        risk_keywords=["风险", "下跌"],
    )
    logger = TradeLogger(log_file=str(tmp_path / "trades.log"))
    reporter = ReviewReporter(reports_dir=str(tmp_path / "reports"), symbols_display={"rb0": "SHFE.rb"})
    sched = TradingScheduler(
        cfg=paper_cfg,
        session=_session(),
        quotes=quotes,
        signals=signals,
        risk_gate=risk_gate,
        planner=planner,
        broker=broker,
        intel=intel,
        logger=logger,
        reporter=reporter,
    )
    # 持仓根数按轮次递增（模拟 8/24 审计口径：每 60s tick 计入持仓根数，
    # 使 S1/S4 等基于根数的风控在盘中可触发——真实 broker 按日历日计 1 根）
    original_position_ctx = broker.position_ctx
    counter = {"n": 0}

    def _round_position_ctx(symbol: str, quote: Quote, atr: float = 0.0) -> object:
        ctx = original_position_ctx(symbol, quote, atr)
        if abs(ctx.position) > 1e-12:
            counter["n"] += 1
            ctx.bars_in_position = counter["n"]
        else:
            counter["n"] = 0
        return ctx

    broker.position_ctx = _round_position_ctx
    return sched, {"quotes": quotes, "signals": signals}


def _sig(p_up: float = 0.7, exp_ret: float = 0.5, source: str = "test") -> SignalFrame:
    return SignalFrame(
        symbol="rb0", ts=datetime(2026, 8, 24, 9, 0), p_up=p_up, exp_ret=exp_ret,
        is_effective=True, source=source, freshness_days=1,
    )


def _q(price: float, now: datetime) -> Quote:
    return Quote(
        symbol="rb0", ts=now, price=price,
        open=price, high=price + 1, low=price - 1, pre_settle=price,
    )


def _flat_plan() -> Plan:
    return Plan(symbol="rb0", direction=0, target_qty=0.0, target_pos_pct=0.0)


# ---------------------------------------------------------------------------
# ④ 初始无指纹 → 允许开仓并记录指纹
# ---------------------------------------------------------------------------
def test_cooldown_allows_first_open(tmp_path):
    sched, _ = _scheduler(tmp_path, _sig())
    now = datetime(2026, 8, 24, 10, 0)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    assert len(sched._all_trades) == 1
    assert sched._last_sig_fp["rb0"] == (0.7, 0.5, "test")


# ---------------------------------------------------------------------------
# ① 信号未变 + 无持仓 → 冷却拦截，不开仓
# ---------------------------------------------------------------------------
def test_cooldown_blocks_reopen_on_unchanged_signal(tmp_path):
    sched, _ = _scheduler(tmp_path, _sig())
    now = datetime(2026, 8, 24, 10, 0)
    # 第 1 轮：初始无指纹 → 开仓（指纹记录）
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    assert len(sched._all_trades) == 1
    # 强制平仓（模拟止损/止盈后回到空仓）
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, now), now)
    assert sched._broker.position("rb0") == 0.0
    # 第 2 轮：信号未变化 + 无持仓 → 冷却拦截，不开仓
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now + timedelta(minutes=1)), {"rb0": 3038.0})
    assert sched._broker.position("rb0") == 0.0
    assert len(sched._all_trades) == 1  # 只有第 1 轮的开仓
    assert sched._last_sig_fp["rb0"] == (0.7, 0.5, "test")  # 指纹未被覆盖


# ---------------------------------------------------------------------------
# ② 信号未变 + 有持仓 → 风控动作（S1 平仓）照常执行
# ---------------------------------------------------------------------------
def test_cooldown_does_not_block_risk_actions_with_position(tmp_path):
    sched, _ = _scheduler(tmp_path, _sig())
    now = datetime(2026, 8, 24, 10, 0)
    # 开仓（3038 > MA20，无 S1）
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    # 价格跌破 MA20（3036 < 3037）→ S1 平仓；冷却不得拦截已有持仓的风控动作
    now2 = now + timedelta(minutes=1)
    sched._process_symbol("rb0", now2, _q(3036.0, now2), {"rb0": 3036.0})
    assert sched._broker.position("rb0") == 0.0  # 风控平仓照常执行
    assert len(sched._all_trades) == 2  # 开 + 平
    assert all(e.is_open for e in sched._all_trades[:1])
    assert not any(e.is_open for e in sched._all_trades[1:])


# ---------------------------------------------------------------------------
# ③ 信号变化 → 解除冷却，开仓并更新指纹
# ---------------------------------------------------------------------------
def test_cooldown_allows_open_on_signal_change(tmp_path):
    sched, ctx = _scheduler(tmp_path, _sig(p_up=0.7, exp_ret=0.5))
    now = datetime(2026, 8, 24, 10, 0)
    # 第 1 轮：开仓（指纹 A）
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, now), now)
    assert sched._broker.position("rb0") == 0.0
    # 第 2 轮：信号变化（p_up/exp_ret 均不同）→ 解除冷却，开仓并更新指纹
    ctx["signals"]._sig = _sig(p_up=0.55, exp_ret=0.1)
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now + timedelta(minutes=1)), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    assert sched._last_sig_fp["rb0"] == (0.55, 0.1, "test")


# ---------------------------------------------------------------------------
# ⑤ 信号源变化（source 不同）→ 视为信号变化
# ---------------------------------------------------------------------------
def test_cooldown_treats_source_change_as_change(tmp_path):
    sched, ctx = _scheduler(tmp_path, _sig(p_up=0.7, exp_ret=0.5, source="engine_a"))
    now = datetime(2026, 8, 24, 10, 0)
    # 第 1 轮：开仓（source=engine_a）
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, now), now)
    assert sched._broker.position("rb0") == 0.0
    # 第 2 轮：p_up/exp_ret 相同但 source=technical（技术兜底切换）→ 视为变化 → 开仓
    ctx["signals"]._sig = _sig(p_up=0.7, exp_ret=0.5, source="technical")
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now + timedelta(minutes=1)), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    assert sched._last_sig_fp["rb0"] == (0.7, 0.5, "technical")


# ---------------------------------------------------------------------------
# ⑥ 容差内微变 → 仍视为未变化，继续拦截
# ---------------------------------------------------------------------------
def test_cooldown_within_tolerance_still_blocks(tmp_path):
    sched, ctx = _scheduler(tmp_path, _sig(p_up=0.7, exp_ret=0.5))
    now = datetime(2026, 8, 24, 10, 0)
    # 第 1 轮：开仓
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, now), now)
    assert sched._broker.position("rb0") == 0.0
    # 第 2 轮：p_up 差 0.005 < 0.01、exp_ret 差 0.0005 < 0.001 → 仍视为未变化 → 拦截
    ctx["signals"]._sig = _sig(p_up=0.705, exp_ret=0.5005)
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now + timedelta(minutes=1)), {"rb0": 3038.0})
    assert sched._broker.position("rb0") == 0.0
    assert len(sched._all_trades) == 1
