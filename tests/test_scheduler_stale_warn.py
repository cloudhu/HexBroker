"""P0-3 调度器运行时告警：主源信号陈旧 → 每品种每交易日仅告警一次（防刷屏）。

覆盖：
① 陈旧信号（fd=1 > 阈值 0）同品种同交易日多次 tick → 仅 1 条 ``[告警] 信号陈旧``；
② 跨交易日 → 重新告警（每日一次）；
③ 新鲜信号（fd=0）→ 不告警；
④ 信号引擎未暴露 ``freshness_threshold``（旧实现/mock）→ 静默跳过，不报错；
⑤ 陈旧主源 + 技术兜底缺失 + 无持仓 → 不开仓（禁开语义，§8.2）。

场景对齐 2026-08-24 夜盘审计：08-21(周五) 信号在 08-24(周一) 被使用。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

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
from hexbroker.paper.signals import SignalEngine
from hexbroker.paper.types import Quote, SignalFrame

_DAY = [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]
MA20 = 3037.0


class RecordingLog:
    """替换模块级 log，记录各级别消息（去重断言用）。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    @staticmethod
    def _fmt(message: str, *args: object) -> str:
        return message.format(*args) if args else message

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(self._fmt(message, *args))

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        self.infos.append(self._fmt(message, *args))

    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        pass

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(self._fmt(message, *args))

    def exception(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(self._fmt(message, *args))

    def stale_warns(self) -> list[str]:
        return [m for m in self.warnings if "信号陈旧" in m]


class StaleSignals:
    """mock 信号引擎：返回指定主源信号，暴露 freshness_threshold（默认 0）。"""

    def __init__(self, sig: SignalFrame | None, threshold: int | None = 0) -> None:
        self.sig = sig
        if threshold is not None:
            self.freshness_threshold = threshold  # 与 SignalEngine property 等价

    def latest_signal(self, symbol: str, asof=None):  # noqa: ANN001
        return self.sig

    def technical_fallback(self, symbol: str, bars):  # noqa: ANN001
        return None  # 兜底不可用 → 无持仓禁开

    def neutral_signal(self, symbol: str, asof=None):  # noqa: ANN001
        return SignalEngine.neutral_signal(symbol, asof)


class StaleQuotes:
    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime(2026, 8, 24, 10, 0)
        return {
            s: Quote(symbol=s, ts=now, price=3038.0, open=3030, high=3040, low=3030, pre_settle=3030)
            for s in symbols
        }

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120) -> pd.DataFrame:
        n = 22
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
            "freshness_threshold_trading_days": 0,
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


def _risk_gate() -> RiskGate:
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


def _scheduler(tmp_path, signals: StaleSignals) -> TradingScheduler:
    paper_cfg = _paper_cfg()
    paper_cfg.account_file = str(tmp_path / "account.json")
    paper_cfg.c0_daily_csv = str(tmp_path / "c0_daily.csv")
    session = TradingSession(
        symbol_sessions={"rb0": parse_sessions(_DAY, [["21:00", "23:00"]])},
        holidays=set(),
        night_boundary=time(21, 0),
    )
    broker = PaperBroker(
        _cost(), initial_capital=float(paper_cfg.initial_capital), budget_ratio=0.30, data_dir=str(tmp_path)
    )
    return TradingScheduler(
        cfg=paper_cfg,
        session=session,
        quotes=StaleQuotes(),
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


def _sig(freshness_days: int, is_effective: bool) -> SignalFrame:
    return SignalFrame(
        symbol="rb0",
        ts=datetime(2026, 8, 21, 15, 0),
        p_up=0.7,
        exp_ret=0.5,
        is_effective=is_effective,
        source="engine_a",
        freshness_days=freshness_days,
    )


def _q(price: float, now: datetime) -> Quote:
    return Quote(symbol="rb0", ts=now, price=price, open=price, high=price + 1, low=price - 1, pre_settle=price)


# ---------------------------------------------------------------------------
# ① 同品种同交易日多次 tick → 仅 1 条告警；⑤ 无持仓禁开
# ---------------------------------------------------------------------------
def test_stale_signal_warns_once_per_symbol_per_day(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(1, is_effective=False)))
    for i in range(3):
        now = datetime(2026, 8, 24, 10, 0) + timedelta(minutes=i)
        sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    stale = rec.stale_warns()
    assert len(stale) == 1
    assert "fd=1" in stale[0] and "品种=rb0" in stale[0]
    assert "技术兜底接手" in stale[0]
    # 陈旧主源 + 兜底缺失 + 无持仓 → 禁开（§8.2）
    assert sched._broker.position("rb0") == 0.0
    assert sched._all_trades == []
    assert sched._stale_warn[("rb0", date(2026, 8, 24))] is True


# ---------------------------------------------------------------------------
# ② 跨交易日 → 再次告警
# ---------------------------------------------------------------------------
def test_stale_signal_warns_again_next_trading_day(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(1, is_effective=False)))
    d1 = datetime(2026, 8, 24, 10, 0)
    sched._process_symbol("rb0", d1, _q(3038.0, d1), {"rb0": 3038.0})
    sched._process_symbol("rb0", d1 + timedelta(minutes=1), _q(3038.0, d1), {"rb0": 3038.0})
    assert len(rec.stale_warns()) == 1
    d2 = datetime(2026, 8, 25, 10, 0)
    sched._process_symbol("rb0", d2, _q(3038.0, d2), {"rb0": 3038.0})
    sched._process_symbol("rb0", d2 + timedelta(minutes=1), _q(3038.0, d2), {"rb0": 3038.0})
    assert len(rec.stale_warns()) == 2


# ---------------------------------------------------------------------------
# ③ 新鲜信号 → 不告警
# ---------------------------------------------------------------------------
def test_fresh_signal_does_not_warn(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, is_effective=True)))
    now = datetime(2026, 8, 24, 10, 0)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert rec.stale_warns() == []
    assert abs(sched._broker.position("rb0")) > 0  # 新鲜信号正常开仓


# ---------------------------------------------------------------------------
# ④ 引擎未暴露阈值（旧实现/mock）→ 静默跳过
# ---------------------------------------------------------------------------
def test_engine_without_threshold_property_is_silent(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(9, is_effective=False), threshold=None))
    now = datetime(2026, 8, 24, 10, 0)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert rec.stale_warns() == []


def test_warn_helper_handles_none_signal(tmp_path):
    sched = _scheduler(tmp_path, StaleSignals(None))
    assert sched._warn_stale_signal_once("rb0", None, date(2026, 8, 24)) is False
