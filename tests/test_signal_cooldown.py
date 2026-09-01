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

import json
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
            # 与 configs/paper.yaml 对齐的生产取值：TTL 30min / 当日最多重开 2 次 / 持久化开
            "signal_cooldown": {
                "enabled": True,
                "p_up_tol": 0.01,
                "exp_ret_tol": 0.001,
                "ttl_minutes": 30,
                "max_reentries_per_day": 2,
                "persist": True,
            },
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
    # P0-1：冷却状态落盘路径必须指向 tmp，否则单测会写脏生产 data/paper/cooldown.json
    paper_cfg.cooldown_file = str(tmp_path / "cooldown.json")
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
    # P0-1：指纹升级为 CooldownRecord（首开 → 重开计数 0，TTL 起算点 = 开仓时刻）
    rec = sched._last_sig_fp["rb0"]
    assert rec.fp == (0.7, 0.5, "test")
    assert rec.opened_at == now
    assert rec.day == "2026-08-24"
    assert rec.reentries == 0


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
    # TTL=30min > 1min → 仍拦截；指纹未被覆盖（开仓时刻仍是第 1 轮）
    assert sched._last_sig_fp["rb0"].fp == (0.7, 0.5, "test")
    assert sched._last_sig_fp["rb0"].opened_at == now


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
    # exp_ret=0.3% → expected_pnl=91.1 > (4.6+20.0)×2=49.1，含滑点成本门禁仍放行
    ctx["signals"]._sig = _sig(p_up=0.55, exp_ret=0.3)
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now + timedelta(minutes=1)), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0
    assert sched._last_sig_fp["rb0"].fp == (0.55, 0.3, "test")


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
    assert sched._last_sig_fp["rb0"].fp == (0.7, 0.5, "technical")


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


# ---------------------------------------------------------------------------
# P0-1（2026-09-01）指纹生命周期：TTL / 当日重开预算 / 跨窗口持久化
#
# ⚠️ 时间选取：_DAY = [09:00-10:15, 10:30-11:30, 13:30-15:00]，TTL=30min。
#    所有 tick 必须落在交易时段内，否则 is_tradable 早返回 → 断言假通过。
# ---------------------------------------------------------------------------

def _tick_at(sched, when: datetime):
    return sched._process_symbol("rb0", when, _q(3038.0, when), {"rb0": 3038.0})


def test_p01_ttl_expiry_allows_reopen(tmp_path):
    """⑦ TTL 到期 → 放行重开，且重开计数 +1。

    这是 P0-1 的核心：修复前冷却「永不过期」，信号缓存日内恒定导致平仓后当日停摆
    （取证：rb0 08-31 停摆 1.94h / 09-01 停摆 2.36h）。
    """
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    _tick_at(sched, t0)
    assert abs(sched._broker.position("rb0")) > 0
    assert sched._last_sig_fp["rb0"].reentries == 0
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, t0), t0)

    # +31min > TTL 30min → 冷却到期；当日重开计数 0 < 上限 2 → 放行
    t1 = t0 + timedelta(minutes=31)
    _tick_at(sched, t1)
    assert abs(sched._broker.position("rb0")) > 0, "TTL 到期后应允许重开，否则退化为当日停摆"
    rec = sched._last_sig_fp["rb0"]
    assert rec.opened_at == t1
    assert rec.reentries == 1
    # 注意：直接调 _broker.execute_plan 的强制平仓不经过 _record_trade，
    # 故 _all_trades 只统计「经 _process_symbol 的开仓」= 2 条。
    assert len(sched._all_trades) == 2


def test_p01_daily_reentry_budget_blocks_after_cap(tmp_path):
    """⑧ 当日重开预算耗尽 → 拦截（reason=signal_cooldown_budget）。

    只加 TTL 会让 60s 开-平-开-平循环以 TTL 为周期复活；预算是该循环的硬顶。
    max_reentries_per_day=2 → 当日最多 3 次开仓（首开 + 2 次重开）。
    """
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    # 首开（reentries=0）→ 重开 1 → 重开 2，每次间隔 31min > TTL
    stamps = [t0, t0 + timedelta(minutes=31), t0 + timedelta(minutes=62)]
    for i, ts in enumerate(stamps):
        _tick_at(sched, ts)
        assert abs(sched._broker.position("rb0")) > 0, f"第 {i + 1} 次开仓应放行"
        assert sched._last_sig_fp["rb0"].reentries == i
        sched._broker.execute_plan(_flat_plan(), _q(3038.0, ts), ts)

    # 第 4 次：TTL 已到期（+31min），但当日重开预算 2/2 耗尽 → 拦截
    t3 = t0 + timedelta(minutes=93)
    _tick_at(sched, t3)
    assert sched._broker.position("rb0") == 0.0, "预算耗尽后不得再重开"
    assert len(sched._all_trades) == 3  # 3 次经 _process_symbol 的开仓
    # 拦截原因须与 TTL 未到期区分开（运维要能分辨「等一等」和「今天到此为止」）
    assert sched._block_reasons[date(2026, 8, 24)].get("signal_cooldown_budget") == 1


def test_p01_reentry_budget_resets_on_new_trading_day(tmp_path):
    """⑨ 换交易日 → 重开计数归零（新交易日 = 新交易机会）。"""
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    _tick_at(sched, t0)
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, t0), t0)
    t1 = t0 + timedelta(minutes=31)
    _tick_at(sched, t1)
    assert sched._last_sig_fp["rb0"].reentries == 1
    assert sched._last_sig_fp["rb0"].day == "2026-08-24"
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, t1), t1)
    assert sched._broker.position("rb0") == 0.0

    # 次一交易日同信号 → 计数归零
    t2 = datetime(2026, 8, 25, 9, 10)
    _tick_at(sched, t2)
    rec = sched._last_sig_fp["rb0"]
    assert rec.day == "2026-08-25"
    assert rec.reentries == 0


def test_p01_state_survives_restart(tmp_path):
    """⑩ 跨窗口重启（08:55/13:25/20:55）不丢失冷却状态。

    修复前指纹仅存内存，一天三次重启 = 三次「免费重开」，TTL 形同虚设。
    """
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    _tick_at(sched, t0)
    assert abs(sched._broker.position("rb0")) > 0
    cooldown_file = tmp_path / "cooldown.json"
    assert cooldown_file.exists(), "开仓后必须立即原子落盘冷却状态"

    # 模拟重启：新进程从同一文件恢复
    sched2, _ = _scheduler(tmp_path, _sig())
    sched2._broker.execute_plan(_flat_plan(), _q(3038.0, t0), t0)  # 对齐：空仓
    rec = sched2._last_sig_fp["rb0"]
    assert rec.fp == (0.7, 0.5, "test")
    assert rec.day == "2026-08-24"
    assert rec.reentries == 0
    assert rec.opened_at == t0

    # TTL 起算点必须沿用恢复的开仓时刻（+10min < 30min → 仍拦截）
    _tick_at(sched2, t0 + timedelta(minutes=10))
    assert sched2._broker.position("rb0") == 0.0, "重启不得重置冷却"


def test_p01_corrupt_state_file_degrades_gracefully(tmp_path):
    """⑪ 冷却状态文件损坏 → 备份 + 降级为空状态，绝不启动失败。"""
    (tmp_path / "cooldown.json").write_text("{ 这不是 JSON", encoding="utf-8")
    sched, _ = _scheduler(tmp_path, _sig())  # 不得抛异常
    assert sched._last_sig_fp == {}
    backups = list(tmp_path.glob("cooldown.json.corrupt.*"))
    assert len(backups) == 1, "损坏文件须留证（与 broker 快照同款处理）"


def test_p01_cooldown_fields_in_decision_trace(tmp_path):
    """⑫ 决策 trace 携带冷却观测字段（回答「还要等多久 / 今天还能开几次」）。

    2026-09-01 rb0 停摆 2h23m，只有 reason=signal_cooldown 无法判断
    是「等 TTL」还是「已锁死」，故把剩余分钟与已重开次数一并落盘。
    """
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    _tick_at(sched, t0)
    sched._broker.execute_plan(_flat_plan(), _q(3038.0, t0), t0)

    t1 = t0 + timedelta(minutes=10)  # 仍在冷却中
    fields = sched._cooldown_trace_fields("rb0", t1, date(2026, 8, 24))
    assert fields["cooldown_remaining_min"] == pytest.approx(20.0, abs=0.01)
    assert fields["cooldown_reentries"] == 0

    # 冷却到期后剩余为 0（不出现负值，下游绘图/告警不炸）
    assert sched._cooldown_trace_fields("rb0", t0 + timedelta(hours=2), date(2026, 8, 24))[
        "cooldown_remaining_min"
    ] == 0.0
    # 无冷却记录的品种 → None（而非 0，语义区分「无冷却」与「已到期」）
    assert sched._cooldown_trace_fields("ag0", t1, date(2026, 8, 24)) == {
        "cooldown_remaining_min": None,
        "cooldown_reentries": None,
    }


def test_p01_persisted_payload_is_json_roundtrip(tmp_path):
    """⑬ 落盘格式可被本模块自己读回（schema 自洽，且 fp 三元组顺序不丢）。"""
    sched, _ = _scheduler(tmp_path, _sig())
    t0 = datetime(2026, 8, 24, 9, 10)
    _tick_at(sched, t0)
    raw = json.loads((tmp_path / "cooldown.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == "1.0"
    rec = raw["records"]["rb0"]
    assert rec["fp"] == [0.7, 0.5, "test"]
    assert rec["day"] == "2026-08-24"
    assert rec["reentries"] == 0
    assert datetime.fromisoformat(rec["opened_at"]) == t0
