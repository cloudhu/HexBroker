#!/usr/bin/env python
# scripts/replay_0824_cost_cooldown.py
# 复跑 2026-08-24 上午「做多后 60s 秒平」场景，对比三档实现的开-平次数：
#   档 1：修复前（min_bars=1, band=0, 无成本门禁、无信号冷却）→ 预期复现 30开30平
#   档 2：仅 cb37333（min_bars=2, band=0.1×ATR）→ 预期 3开2平（S4 时间止损驱动）
#   档 3：cb37333 + P0-1 成本门禁 + P0-2 信号冷却 → 开/平进一步下降（信号恒定 → 冷却拦截重开）
#
# 场景（真实取证）：rb0 价格 3038↔3036 交替（60s 步长）、日线 MA20=3037（恰落其间）、
# 信号 p_up=0.733333 恒定、exp_ret=0.170757（%）、费率 0.00005/0.00010、乘数 10。
# 走真实调度器 _process_symbol 决策链（风控→冷却→计划→撮合），持仓根数按轮次递增
# （模拟审计口径：每 60s tick 计入持仓根数，使 S1/S4 盘中可触发）。
#
# 用法：
#   python scripts/replay_0824_cost_cooldown.py [--rounds 60]
import argparse
import sys
from datetime import date, datetime, timedelta, time
from pathlib import Path

import numpy as np
import pandas as pd
from omegaconf import OmegaConf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.backtest.cost import CostModel  # noqa: E402
from hexbroker.paper.broker import PaperBroker  # noqa: E402
from hexbroker.paper.intel import IntelligenceService, StaticNewsProvider  # noqa: E402
from hexbroker.paper.logger import TradeLogger  # noqa: E402
from hexbroker.paper.planner import PlanManager  # noqa: E402
from hexbroker.paper.reporter import ReviewReporter  # noqa: E402
from hexbroker.paper.risk_gate import RiskGate  # noqa: E402
from hexbroker.paper.scheduler import TradingScheduler  # noqa: E402
from hexbroker.paper.sessions import TradingSession, parse_sessions  # noqa: E402
from hexbroker.paper.types import Quote, SignalFrame  # noqa: E402

# ---- 场景参数（来自 8/24 取证） ----
SYMBOL = "rb0"
PRICES = [3038.0, 3036.0]     # 两值交替（真实成交价）
MA20 = 3037.0                 # 日线 MA20（落在两值之间）
P_UP = 0.733333               # 信号 p_up（恒定多头）
EXP_RET = 0.170757            # exp_ret（%，实测信号缓存 08-21 最新）
EQUITY = 100_000.0
INTENT = 0.30
DAY = date(2026, 8, 24)
_DAY_SESS = [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]


def _risk_cfg(s1_min_bars: int, s1_band_atr: float) -> OmegaConf:
    return OmegaConf.create({
        "risk": {
            "vol_target": 0.20,
            "kelly_cap": 0.25,
            "max_position_pct": 0.30,
            "recovery_drawdown_r1": 0.05,
            "recovery_drawdown_r2": 0.10,
            "recovery_drawdown_r3": 0.15,
            "position_scalar_r1": 0.5,
            "position_scalar_r2": 0.0,
            "position_scalar_r3": 0.2,
            "position_scalar_r4": 1.0,
            "vol_low_q": 0.2,
            "vol_high_q": 0.8,
            "sell_s1_min_bars": s1_min_bars,
            "sell_s1_band_atr": s1_band_atr,
        }
    })


def _bars() -> pd.DataFrame:
    """22 根日线：close 恒定 3037 → MA20=3037；high-low=40 → ATR≈40。"""
    n = 22
    closes = [MA20] * n
    highs = [MA20 + 20.0] * n
    lows = [MA20 - 20.0] * n
    idx = pd.date_range("2026-07-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1000.0] * n},
        index=idx,
    )


class ReplayQuotes:
    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime(2026, 8, 24, 10, 0)
        out: dict[str, Quote] = {}
        for s in symbols:
            out[s] = Quote(symbol=s, ts=now, price=PRICES[0], open=PRICES[0], high=PRICES[0] + 1, low=PRICES[0] - 1, pre_settle=PRICES[0])
        return out

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120):
        return _bars()


class ReplaySignals:
    def __init__(self) -> None:
        self.calls = 0

    def latest_signal(self, symbol: str, asof):
        self.calls += 1
        return SignalFrame(
            symbol=SYMBOL, ts=datetime(2026, 8, 24, 9, 0), p_up=P_UP, exp_ret=EXP_RET,
            is_effective=True, source="replay", freshness_days=1,
        )

    def technical_fallback(self, symbol: str, bars):
        return None

    def neutral_signal(self, symbol: str, asof=None):
        return SignalFrame(symbol=symbol, ts=datetime.now(), p_up=0.5, exp_ret=0.0,
                           is_effective=False, source="risk_only", freshness_days=0)


def _paper_cfg(cost_gate_enabled: bool, cooldown_enabled: bool) -> OmegaConf:
    return OmegaConf.create({
        "initial_capital": EQUITY,
        "budget_ratio": 0.40,
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
                "sessions": {"day": _DAY_SESS, "night": [["21:00", "23:00"]]},
            }
        },
        "holidays_2026": [],
        "risk_gate": {"cost_gate_enabled": cost_gate_enabled, "cost_gate_min_ratio": 2.0},
        "signal_cooldown": {"enabled": cooldown_enabled, "p_up_tol": 0.01, "exp_ret_tol": 0.001},
    })


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


def _build_scheduler(tmp_path: Path, s1_min_bars: int, s1_band_atr: float,
                     cost_gate_enabled: bool, cooldown_enabled: bool) -> TradingScheduler:
    paper_cfg = _paper_cfg(cost_gate_enabled, cooldown_enabled)
    paper_cfg.account_file = str(tmp_path / "account.json")
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    risk_gate = RiskGate(_risk_cfg(s1_min_bars, s1_band_atr), default_intent=INTENT)
    broker = PaperBroker(_cost(), initial_capital=EQUITY, budget_ratio=0.40, data_dir=str(tmp_path))
    planner = PlanManager(multipliers={"rb0": 10.0}, plans_dir=str(tmp_path / "plans"))
    session = TradingSession(
        symbol_sessions={"rb0": parse_sessions(_DAY_SESS, [["21:00", "23:00"]])},
        holidays=set(), night_boundary=time(21, 0),
    )
    intel = IntelligenceService(
        providers=[StaticNewsProvider(path=str(tmp_path / "news.json"))],
        keywords={"rb0": ["螺纹"]}, risk_keywords=["风险", "下跌"],
    )
    logger = TradeLogger(log_file=str(tmp_path / "trades.log"))
    reporter = ReviewReporter(reports_dir=str(tmp_path / "reports"), symbols_display={"rb0": "SHFE.rb"})
    sched = TradingScheduler(
        cfg=paper_cfg, session=session, quotes=ReplayQuotes(), signals=ReplaySignals(),
        risk_gate=risk_gate, planner=planner, broker=broker, intel=intel, logger=logger,
        reporter=reporter,
    )
    # 持仓根数按轮次递增（模拟审计口径：每 60s tick 计入持仓根数）
    original_position_ctx = broker.position_ctx
    counter = {"n": 0}

    def _round_position_ctx(symbol: str, quote: Quote, atr: float = 0.0):
        ctx = original_position_ctx(symbol, quote, atr)
        if abs(ctx.position) > 1e-12:
            counter["n"] += 1
            ctx.bars_in_position = counter["n"]
        else:
            counter["n"] = 0
        return ctx

    broker.position_ctx = _round_position_ctx
    return sched


def replay(rounds: int, s1_min_bars: int, s1_band_atr: float,
           cost_gate_enabled: bool, cooldown_enabled: bool, tmp_path: Path) -> dict:
    """驱动真实调度器 _process_symbol（60s 步长），统计开/平次数。"""
    sched = _build_scheduler(tmp_path, s1_min_bars, s1_band_atr, cost_gate_enabled, cooldown_enabled)
    t0 = datetime(2026, 8, 24, 9, 5, 0)
    for i in range(rounds):
        price = PRICES[i % 2]
        now = t0 + timedelta(minutes=i)
        quote = Quote(symbol=SYMBOL, ts=now, price=price, open=price, high=price + 1, low=price - 1, pre_settle=price)
        sched._process_symbol(SYMBOL, now, quote, {SYMBOL: price})
    opens = sum(1 for e in sched._all_trades if e.is_open)
    closes = sum(1 for e in sched._all_trades if not e.is_open)
    return {
        "rounds": rounds,
        "opens": opens,
        "closes": closes,
        "end_position": sched._broker.position(SYMBOL),
        "last_fp": sched._last_sig_fp.get(SYMBOL),
    }


def main() -> None:
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=60)
    args = ap.parse_args()

    print(f"场景复现（rb0，MA20={MA20}，价格 {PRICES[0]}↔{PRICES[1]} 交替，"
          f"p_up={P_UP}，exp_ret={EXP_RET}%，ATR≈40，{args.rounds} 轮/60s 步长，费率 0.00005/0.00010，乘数 10）")
    print("=" * 76)

    with tempfile.TemporaryDirectory(prefix="replay_0824_", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        # 档 1：修复前
        r1 = replay(args.rounds, s1_min_bars=1, s1_band_atr=0.0,
                    cost_gate_enabled=False, cooldown_enabled=False, tmp_path=tmp_path / "t1")
        print(f"[档1 修复前]              min_bars=1 band=0.0 : 开仓 {r1['opens']:>3} 次 | 平仓 {r1['closes']:>3} 次 | 末态持仓 {r1['end_position']}")
        # 档 2：仅 cb37333
        r2 = replay(args.rounds, s1_min_bars=2, s1_band_atr=0.1,
                    cost_gate_enabled=False, cooldown_enabled=False, tmp_path=tmp_path / "t2")
        print(f"[档2 仅cb37333]           min_bars=2 band=0.1 : 开仓 {r2['opens']:>3} 次 | 平仓 {r2['closes']:>3} 次 | 末态持仓 {r2['end_position']}")
        # 档 3：cb37333 + P0-1 + P0-2
        r3 = replay(args.rounds, s1_min_bars=2, s1_band_atr=0.1,
                    cost_gate_enabled=True, cooldown_enabled=True, tmp_path=tmp_path / "t3")
        print(f"[档3 cb37333+P0-1+P0-2]  min_bars=2 band=0.1 : 开仓 {r3['opens']:>3} 次 | 平仓 {r3['closes']:>3} 次 | 末态持仓 {r3['end_position']}")

    print("=" * 76)
    print("说明：档2 平仓由 S4 时间止损（持仓 20 根未盈利）驱动；档3 信号恒定（p_up/exp_ret 不变）")
    print("      → 冷却拦截每次平仓后的重复开仓，开/平次数进一步下降。")


if __name__ == "__main__":
    main()
