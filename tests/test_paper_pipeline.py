"""T05 端到端离线冒烟：mock 行情 → 信号 → 风控 → 计划 → 撮合 → 日志 → 复盘（A1/A3/A5）。"""

from __future__ import annotations

import re
from datetime import date, datetime, time

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
from hexbroker.paper.types import NewsItem, Quote, SignalFrame

RB_PRICE = 3000.0
MON = date(2026, 8, 24)

_DAY = [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]

TRADE_RE = re.compile(
    r"^TRADE\|(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\|([a-z0-9]+)\|(LONG|SHORT|FLAT)\|"
    r"(-?\d+(?:\.\d+)?)\|(-?\d+(?:\.\d+)?)\|([-.\d]*)\|([-.\d]*)\|(-?\d+(?:\.\d+)?)\|(-?\d+(?:\.\d+)?)$"
)


def _strip_loguru(lines: list[str], marker: str) -> list[str]:
    """从 loguru 行中提取 marker 之后的内容（日志格式带时间戳前缀）。"""
    out: list[str] = []
    for line in lines:
        idx = line.find(marker)
        if idx >= 0:
            out.append(line[idx:])
    return out


class FakeQuotes:
    """离线 mock 行情：固定价 + 空 K 线。"""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls += 1
        now = datetime(2026, 8, 24, 10, 0)
        out: dict[str, Quote] = {}
        for s in symbols:
            price = RB_PRICE if s == "rb0" else (8000.0 if s == "ag0" else 2265.0)
            out[s] = Quote(symbol=s, ts=now, price=price, open=price * 0.997, high=price * 1.005, low=price * 0.995, pre_settle=price * 0.998)
        return out

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120):
        return pd.DataFrame()


class FakeSignals:
    """mock 信号引擎。"""

    def __init__(self, sig: SignalFrame | None) -> None:
        self._sig = sig
        self.calls = 0

    def latest_signal(self, symbol: str, asof):
        self.calls += 1
        return self._sig

    def technical_fallback(self, symbol: str, bars):
        return None

    def neutral_signal(self, symbol: str, asof=None):
        return SignalEngine.neutral_signal(symbol, asof)


def _paper_cfg(symbols: dict | None = None) -> OmegaConf:
    """最小 paper 配置（调度器只读字段）。"""
    syms = symbols or {
        "rb0": {
            "display": "SHFE.rb",
            "sina_code": "nf_RB0",
            "multiplier": 10,
            "min_tick": 1,
            "mode": "trade",
            "sessions": {"day": _DAY, "night": [["21:00", "23:00"]]},
        }
    }
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
            "symbols": syms,
            "holidays_2026": [],
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
        contracts={"ag0": {"multiplier": 15.0, "min_tick": 0.01}, "rb0": {"multiplier": 10.0, "min_tick": 1.0}, "c0": {"multiplier": 10.0, "min_tick": 1.0}},
    )


def _session() -> TradingSession:
    return TradingSession(
        symbol_sessions={
            "ag0": parse_sessions(_DAY, [["21:00", "02:30"]]),
            "rb0": parse_sessions(_DAY, [["21:00", "23:00"]]),
            "c0": parse_sessions(_DAY, []),
        },
        holidays=set(),
        night_boundary=time(21, 0),
    )


def _risk_gate() -> RiskGate:
    """放宽风控参数：确保 10 万+账户能开 1 手（冒烟用）。"""
    cfg = OmegaConf.create(
        {
            "risk": {
                "vol_target": 0.5,
                "kelly_cap": 1.0,
                "max_position_pct": 0.5,
                "atr_mult_high": 2.5,
                "atr_mult_mid": 2.0,
                "atr_mult_low": 1.5,
                "recovery_drawdown_r1": 0.05,
                "recovery_drawdown_r2": 0.10,
                "recovery_drawdown_r3": 0.15,
                "position_scalar_r1": 0.5,
                "position_scalar_r2": 0.0,
                "position_scalar_r3": 0.2,
                "position_scalar_r4": 1.0,
            }
        }
    )
    return RiskGate(cfg, hard_stop=0.20)


def _scheduler(tmp_path, paper_cfg=None, sig: SignalFrame | None = None) -> tuple[TradingScheduler, dict]:
    paper_cfg = paper_cfg or _paper_cfg()
    # 测试隔离：账户快照/c0 日线落盘到 tmp 目录，避免污染仓库 data/
    paper_cfg.account_file = str(tmp_path / "account.json")
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    quotes = FakeQuotes()
    signals = FakeSignals(sig)
    risk_gate = _risk_gate()
    broker = PaperBroker(_cost(), initial_capital=float(paper_cfg.initial_capital), budget_ratio=0.30, data_dir=str(tmp_path))
    planner = PlanManager(multipliers={"ag0": 15.0, "rb0": 10.0, "c0": 10.0}, plans_dir=str(tmp_path / "plans"))
    intel = IntelligenceService(
        providers=[StaticNewsProvider(path=str(tmp_path / "news.json"))],
        keywords={"rb0": ["螺纹"], "ag0": ["白银"], "c0": ["玉米"]},
        risk_keywords=["风险", "下跌"],
    )
    logger = TradeLogger(log_file=str(tmp_path / "trades.log"))
    reporter = ReviewReporter(reports_dir=str(tmp_path / "reports"), symbols_display={"rb0": "SHFE.rb", "ag0": "SHFE.ag", "c0": "DCE.c"})
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
    return sched, {"tmp": tmp_path, "quotes": quotes, "logger": logger}


def _eff_signal() -> SignalFrame:
    return SignalFrame(symbol="rb0", ts=datetime(2026, 8, 24, 9, 0), p_up=0.7, exp_ret=0.5, is_effective=True, source="test")


# ---------------------------------------------------------------------------
# 端到端管道：信号 → 风控 → 计划 → 撮合 → 日志（A3）
# ---------------------------------------------------------------------------
def test_pipeline_full_trade_and_log(tmp_path):
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())
    now = datetime(2026, 8, 24, 10, 0)
    marks = {"rb0": RB_PRICE}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)

    # 开仓成功：意图 0.3 × 300k / (3000×10) → 3 手 rb
    assert sched._broker.position("rb0") == pytest.approx(3.0)
    assert len(sched._day_trades.get(MON, [])) == 1

    # 日志 5 要素格式（A3）：TRADE|ts|symbol|dir|qty|entry|stop|tp|price|fee
    lines = (ctx["tmp"] / "trades.log").read_text(encoding="utf-8").splitlines()
    trade_lines = _strip_loguru(lines, "TRADE|")
    assert trade_lines, "应输出 TRADE 日志行"
    m = TRADE_RE.match(trade_lines[0])
    assert m, f"TRADE 行格式不合法: {trade_lines[0]}"
    assert m.group(2) == "rb0"
    assert m.group(3) == "LONG"
    assert float(m.group(4)) > 0          # qty
    assert float(m.group(8)) > 0          # price
    assert float(m.group(9)) > 0          # fee


# ---------------------------------------------------------------------------
# 收盘复盘（A5）：md 落盘 + 计划文件
# ---------------------------------------------------------------------------
def test_on_close_generates_review(tmp_path):
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())
    now = datetime(2026, 8, 24, 10, 0)
    marks = {"rb0": RB_PRICE}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)
    sched._on_close(MON)

    report = ctx["tmp"] / "reports" / "复盘_2026-08-24.md"
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "模拟盘复盘 2026-08-24" in content
    assert "当日交易明细" in content
    assert "SHFE.rb" in content
    # 计划文件落盘（R8）
    plan_file = ctx["tmp"] / "plans" / "2026-08-24_plan.json"
    assert plan_file.exists()
    # 交易日计数 +1（Q5 评估基础）
    assert sched._broker.trading_day_count == 1


# ---------------------------------------------------------------------------
# 无信号：禁止开新仓（A2 变体 / §8.2）
# ---------------------------------------------------------------------------
def test_no_signal_blocks_open(tmp_path):
    sched, _ = _scheduler(tmp_path, sig=None)  # latest_signal=None & technical=None
    now = datetime(2026, 8, 24, 10, 0)
    marks = {"rb0": RB_PRICE}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)
    assert sched._broker.position("rb0") == 0.0
    assert sched._day_trades.get(MON, []) == []


# ---------------------------------------------------------------------------
# 开盘延迟：延迟内 0 新开仓（Q6）
# ---------------------------------------------------------------------------
def test_open_delay_blocks_trade(tmp_path):
    sched, _ = _scheduler(tmp_path, sig=_eff_signal())
    now = datetime(2026, 8, 24, 9, 2)  # 开盘后 2 分钟 < 5 分钟延迟
    marks = {"rb0": RB_PRICE}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)
    assert sched._broker.position("rb0") == 0.0


# ---------------------------------------------------------------------------
# accumulate 模式：仅跟踪，不开新仓（c0 决策，§8.1）
# ---------------------------------------------------------------------------
def test_accumulate_mode_tracks_but_no_open(tmp_path):
    syms = {
        "rb0": {"display": "SHFE.rb", "sina_code": "nf_RB0", "multiplier": 10, "min_tick": 1, "mode": "trade", "sessions": {"day": _DAY, "night": [["21:00", "23:00"]]}},
        "c0": {"display": "DCE.c", "sina_code": "nf_C0", "multiplier": 10, "min_tick": 1, "mode": "accumulate", "accumulate_days": 30, "sessions": {"day": _DAY, "night": []}},
    }
    paper_cfg = _paper_cfg(syms)
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    sched, _ = _scheduler(tmp_path, paper_cfg=paper_cfg, sig=_eff_signal())
    now = datetime(2026, 8, 24, 10, 0)
    c0_quote = Quote(symbol="c0", ts=now, price=2265.0, open=2260, high=2275, low=2255, pre_settle=2260)
    marks = {"c0": 2265.0}
    sched._process_symbol("c0", now, c0_quote, marks)

    assert sched._broker.position("c0") == 0.0  # accumulate 不开新仓
    # 盘中 OHLC 被跟踪
    assert sched._c0_intraday.get(MON, {}).get("close") == pytest.approx(2265.0)

    # 收盘 → c0 日线快照落盘（open/high/low/close/settle）
    sched._append_c0_daily(MON)
    csv = tmp_path / "c0_daily.csv"
    assert csv.exists()
    rows = csv.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "date,open,high,low,close,settle"
    assert rows[1].startswith("2026-08-24,")
    assert sched._effective_mode("c0") == "accumulate"  # 1 行 < 30 → 仍 accumulate

    # 模拟积累满 30 天 → 自动切换 trade
    with open(csv, "a", encoding="utf-8") as f:
        for i in range(29):
            f.write(f"2026-08-{24 + i:02d},2260,2275,2255,2265,2265\n")
    assert sched._c0_daily_row_count() >= 30
    assert sched._effective_mode("c0") == "trade"


# ---------------------------------------------------------------------------
# 计划手数（P1-2：10 万账户 ag 可开 1 手）
# ---------------------------------------------------------------------------
def test_size_qty_ag_one_lot_at_100k():
    """P1-2：10 万账户 ag 在合理信号强度（意图 0.30）下可开 1 手（raw=0.119 >= 0.10）。"""
    planner = PlanManager(multipliers={"ag0": 15.0, "rb0": 10.0, "c0": 10.0})
    # ag0：0.30×100000/(16756×15) = 0.1194 → 至少 1 手
    assert planner._size_qty("ag0", 0.30, 16756.0, 100_000.0) == 1.0
    # rb0：0.30×100000/(3600×10) = 0.833 → 1 手
    assert planner._size_qty("rb0", 0.30, 3600.0, 100_000.0) == 1.0
    # c0：0.30×100000/(2718×10) = 1.104 → 1 手
    assert planner._size_qty("c0", 0.30, 2718.0, 100_000.0) == 1.0
    # 信号强度过低（raw < 0.10）→ 0 手
    assert planner._size_qty("ag0", 0.05, 16756.0, 100_000.0) == 0.0
    # >= 1 手向下取整
    assert planner._size_qty("c0", 0.60, 2718.0, 100_000.0) == 2.0


# ---------------------------------------------------------------------------
# 收盘触发（P1-3：15:10 触发复盘，不等到 21:00 夜盘翻转；幂等）
# ---------------------------------------------------------------------------
def test_tick_triggers_close_after_day_close(monkeypatch, tmp_path):
    """P1-3：8/24 15:10（周一）应触发复盘；当日已复盘不重复。"""
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())

    class _FakeDT(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 24, 15, 10)

    monkeypatch.setattr("hexbroker.paper.scheduler.datetime", _FakeDT)
    sched._tick()
    report = ctx["tmp"] / "reports" / "复盘_2026-08-24.md"
    assert report.exists()  # 15:10 即触发复盘（不等到 21:00 夜盘翻转）
    assert sched._broker.trading_day_count == 1

    # 再次 tick → 当日已复盘不重复
    sched._tick()
    assert sched._broker.trading_day_count == 1
    assert len(list((ctx["tmp"] / "reports").glob("复盘_*.md"))) == 1


def test_on_close_idempotent(tmp_path):
    """P1-3：_on_close 幂等——直接重复调用不重复处理。"""
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())
    sched._on_close(MON)
    sched._on_close(MON)
    assert sched._broker.trading_day_count == 1
    assert len(list((ctx["tmp"] / "reports").glob("复盘_*.md"))) == 1


# ---------------------------------------------------------------------------
# 原子写（P2-6：计划/复盘/c0 日线无残留 tmp 半文件）
# ---------------------------------------------------------------------------
def test_atomic_write_no_tmp_leftovers(tmp_path):
    """P2-6：计划落盘/复盘报告/c0 日线追加原子写，无残留 tmp 文件。"""
    syms = {
        "rb0": {"display": "SHFE.rb", "sina_code": "nf_RB0", "multiplier": 10, "min_tick": 1, "mode": "trade", "sessions": {"day": _DAY, "night": [["21:00", "23:00"]]}},
        "c0": {"display": "DCE.c", "sina_code": "nf_C0", "multiplier": 10, "min_tick": 1, "mode": "accumulate", "accumulate_days": 30, "sessions": {"day": _DAY, "night": []}},
    }
    paper_cfg = _paper_cfg(syms)
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    sched, ctx = _scheduler(tmp_path, paper_cfg=paper_cfg, sig=_eff_signal())
    now = datetime(2026, 8, 24, 10, 0)
    marks = {"rb0": RB_PRICE, "c0": 2265.0}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)
    sched._process_symbol("c0", now, Quote(symbol="c0", ts=now, price=2265.0, open=2260, high=2275, low=2255, pre_settle=2260), marks)
    sched._on_close(MON)

    assert (ctx["tmp"] / "plans" / "2026-08-24_plan.json").exists()
    assert (ctx["tmp"] / "reports" / "复盘_2026-08-24.md").exists()
    assert (tmp_path / "c0_daily.csv").exists()
    assert not list((ctx["tmp"] / "plans").glob("*.tmp"))
    assert not list((ctx["tmp"] / "reports").glob("*.tmp"))
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 情报：风险提示/备注 → 计划变更 + PLAN 日志（Q3/R7/R8）
# ---------------------------------------------------------------------------
def test_intel_news_plan_note_and_log(tmp_path):
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())
    # 先出计划
    now = datetime(2026, 8, 24, 10, 0)
    marks = {"rb0": RB_PRICE}
    sched._process_symbol("rb0", now, Quote(symbol="rb0", ts=now, price=RB_PRICE, open=2990, high=3010, low=2990, pre_settle=2990), marks)
    plan = sched._planner.get_plan("rb0")
    assert plan is not None

    # 情报事件：命中风险词 → risk_hint
    items = [
        NewsItem(ts=now, symbols=["rb0"], title="螺纹钢夜盘大幅下跌，风险上升", summary="", tags=[], source="test"),
        NewsItem(ts=now, symbols=["rb0"], title="行业会议纪要", summary="", tags=[], source="test"),
    ]
    filtered = sched._intel.filter_by_keywords(items)
    changes = sched._planner.apply_news(filtered)
    assert len(changes) >= 1
    assert any(c.change_type == "risk_hint" for c in changes)
    plan = sched._planner.get_plan("rb0")
    assert "下跌" in plan.risk_flag or "风险" in plan.risk_flag

    # 模拟调度器：计划变更写日志（PLAN| 行，§7.3）
    for c in changes:
        sched._logger.plan_change(c)
    lines = (ctx["tmp"] / "trades.log").read_text(encoding="utf-8").splitlines()
    plan_lines = _strip_loguru(lines, "PLAN|")
    assert plan_lines


# ---------------------------------------------------------------------------
# 单轮 tick + 收盘（最接近真实主循环的冒烟）
# ---------------------------------------------------------------------------
def test_tick_and_close_roundtrip(tmp_path):
    sched, ctx = _scheduler(tmp_path, sig=_eff_signal())
    # 直接驱动一次完整 tick（内部会用 FakeQuotes 提供全部行情）
    sched._tick()
    day = sched._session.day_label(datetime(2026, 8, 24, 10, 0))
    assert day == MON
    # 收盘复盘
    sched._on_close(MON)
    report = ctx["tmp"] / "reports" / "复盘_2026-08-24.md"
    assert report.exists()
