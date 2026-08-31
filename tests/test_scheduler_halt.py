"""P0-2 行情缺失熔断（HALT）单测：price<=0 或行情连续失败 N 次 → 进入 HALT 态，
停止开仓并告警，禁止用缓存价撮合；行情恢复即解除。

覆盖：
① 行情拉取连续异常（getaddrinfo）→ 达阈值进入 HALT；
② 全部品种 price<=0（数据源返回全 0）→ 计为行情缺失，达阈值进入 HALT；
③ HALT 期间拦截新开仓（防御性），已有持仓风控平仓仍按有效价执行；
④ 任意一次成功取数（≥1 有效品种）→ 复位计数并解除 HALT；
⑤ 单次瞬时失败（随后恢复）→ 不进入 HALT；
⑥ HALT 告警周期化（约 5 分钟一条，不每个 tick 刷屏）；
⑦ ``halt_state()`` 暴露 (是否熔断, 原因, 连续失败计数)。

场景对齐 2026-08-24 夜盘审计：26 次 getaddrinfo 失败后系统用陈旧价继续交易（P1-3）。
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
_THRESHOLD = 3  # 测试用阈值（生产默认 5）


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

    def halt_warns(self) -> list[str]:
        return [m for m in self.warnings if "熔断" in m]


# ---------------------------------------------------------------------------
# 行情 mock
# ---------------------------------------------------------------------------
class FailingQuotes:
    """前 ``fail_times`` 次 fetch_quotes 抛异常（网络层失败），之后返回正常行情。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("getaddrinfo failed (simulated network outage)")
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


class AllInvalidQuotes:
    """拉取成功但全部品种 price<=0（数据源返回全 0 的失真场景）。"""

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime(2026, 8, 24, 10, 0)
        return {
            s: Quote(symbol=s, ts=now, price=0.0, open=0.0, high=0.0, low=0.0, pre_settle=0.0)
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


class ValidQuotes:
    """正常行情（price>0）。"""

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


class StaleSignals:
    """mock 信号引擎：返回指定主源信号，暴露 freshness_threshold（默认 0）。"""

    def __init__(self, sig: SignalFrame | None, threshold: int | None = 0) -> None:
        self.sig = sig
        if threshold is not None:
            self.freshness_threshold = threshold

    def latest_signal(self, symbol: str, asof=None):  # noqa: ANN001
        return self.sig

    def technical_fallback(self, symbol: str, bars):  # noqa: ANN001
        return None

    def neutral_signal(self, symbol: str, asof=None):  # noqa: ANN001
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
            "freshness_threshold_trading_days": 0,
            "quote_fail_halt_threshold": _THRESHOLD,
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


def _scheduler(tmp_path, signals: StaleSignals, quotes) -> TradingScheduler:
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
        quotes=quotes,
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
        ts=datetime(2026, 8, 24, 10, 0),
        p_up=0.7,
        exp_ret=0.5,
        is_effective=is_effective,
        source="engine_a",
        freshness_days=freshness_days,
    )


def _q(price: float, now: datetime) -> Quote:
    return Quote(symbol="rb0", ts=now, price=price, open=price, high=price + 1, low=price - 1, pre_settle=price)


# ---------------------------------------------------------------------------
# ① 行情拉取连续异常 → 达阈值进入 HALT
# ---------------------------------------------------------------------------
def test_halt_enters_after_threshold_consecutive_failures(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), FailingQuotes(fail_times=10))
    now = datetime(2026, 8, 24, 10, 0)
    # 阈值前不熔断
    for i in range(_THRESHOLD - 1):
        sched._fetch_quotes_with_halt(now + timedelta(minutes=i))
    assert sched._halt is False
    assert sched._consecutive_quote_fail == _THRESHOLD - 1
    # 第阈值次 → 熔断
    sched._fetch_quotes_with_halt(now + timedelta(minutes=_THRESHOLD))
    assert sched._halt is True
    assert sched._consecutive_quote_fail == _THRESHOLD
    assert "连续拉取失败" in sched._halt_reason
    # 进入 HALT 的告警已输出
    assert any("进入 HALT" in w for w in rec.halt_warns())


# ---------------------------------------------------------------------------
# ② 全部品种 price<=0 → 计为行情缺失 → 达阈值进入 HALT
# ---------------------------------------------------------------------------
def test_all_invalid_quotes_triggers_halt(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), AllInvalidQuotes())
    now = datetime(2026, 8, 24, 10, 0)
    for i in range(_THRESHOLD):
        sched._fetch_quotes_with_halt(now + timedelta(minutes=i))
    assert sched._halt is True
    assert "全部品种行情无效" in sched._halt_reason
    # 全部无效行情下不允许撮合（quote.valid()=False → 跳过，无开仓）
    assert sched._broker.position("rb0") == 0.0


# ---------------------------------------------------------------------------
# ③ HALT 期间拦截新开仓（防御性）
# ---------------------------------------------------------------------------
def test_halt_blocks_new_open_but_not_risk_close_path(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    # 先正常开仓（无 HALT）
    now = datetime(2026, 8, 24, 10, 0)
    sched._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert abs(sched._broker.position("rb0")) > 0  # 正常开仓成功
    # 强制进入 HALT，再次喂有效信号（应拦截新开仓，但已有持仓不被强制平）
    sched._enter_halt("测试：手动进入 HALT")
    before = sched._broker.position("rb0")
    # 用平仓场景验证：已有持仓 + 中性信号（risk_only）→ 风控管理仍走（不因 HALT 误平）
    sched._process_symbol("rb0", now + timedelta(minutes=1), _q(3038.0, now), {"rb0": 3038.0})
    after = sched._broker.position("rb0")
    # 新开仓被拦截：持仓手数未增加（此处仅验证 HALT 不影响已有仓位的风控执行链路）
    assert abs(after - before) < 1e-9 or after != 0.0  # 持仓未被 HALT 强制清零
    # 另测：无持仓 + 有效信号 + HALT → 不开仓
    sched2 = _scheduler(tmp_path, StaleSignals(_sig(0, True)), ValidQuotes())
    sched2._enter_halt("测试：无持仓 HALT")
    sched2._process_symbol("rb0", now, _q(3038.0, now), {"rb0": 3038.0})
    assert sched2._broker.position("rb0") == 0.0
    assert sched2._all_trades == []


# ---------------------------------------------------------------------------
# ④ 任意一次成功取数 → 复位计数并解除 HALT
# ---------------------------------------------------------------------------
def test_halt_exits_on_recovery(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), FailingQuotes(fail_times=_THRESHOLD))
    now = datetime(2026, 8, 24, 10, 0)
    for i in range(_THRESHOLD):
        sched._fetch_quotes_with_halt(now + timedelta(minutes=i))
    assert sched._halt is True  # 阈值次失败 → 熔断
    # 第阈值+1 次成功取数 → 解除
    sched._fetch_quotes_with_halt(now + timedelta(minutes=_THRESHOLD + 1))
    assert sched._halt is False
    assert sched._consecutive_quote_fail == 0
    assert sched._halt_reason == ""
    # 解除 HALT 的日志已输出
    assert any("解除 HALT" in m for m in rec.infos)


# ---------------------------------------------------------------------------
# ⑤ 单次瞬时失败（随后恢复）→ 不进入 HALT
# ---------------------------------------------------------------------------
def test_transient_single_failure_does_not_halt(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), FailingQuotes(fail_times=1))
    now = datetime(2026, 8, 24, 10, 0)
    # 第 1 次失败
    sched._fetch_quotes_with_halt(now)
    assert sched._consecutive_quote_fail == 1
    assert sched._halt is False
    # 第 2 次成功 → 复位
    sched._fetch_quotes_with_halt(now + timedelta(minutes=1))
    assert sched._halt is False
    assert sched._consecutive_quote_fail == 0


# ---------------------------------------------------------------------------
# ⑥ HALT 告警周期化（约 5 分钟一条，不每个 tick 刷屏）
# ---------------------------------------------------------------------------
def test_halt_warn_is_periodic(tmp_path, monkeypatch):
    rec = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec)
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), FailingQuotes(fail_times=10))
    now = datetime(2026, 8, 24, 10, 0)
    for i in range(_THRESHOLD):
        sched._fetch_quotes_with_halt(now + timedelta(minutes=i))
    assert sched._halt is True
    # 进入时 1 条；随后 4 分钟内周期提醒不重复
    rec2 = RecordingLog()
    monkeypatch.setattr(scheduler_mod, "log", rec2)
    t = now + timedelta(minutes=_THRESHOLD)
    # 将周期提醒基准对齐到合成时间（_enter_halt 内部用 datetime.now() 真实时间）
    sched._halt_last_warn_ts = t
    sched._halt_warn(t)
    sched._halt_warn(t + timedelta(seconds=60))
    sched._halt_warn(t + timedelta(seconds=120))
    assert len(rec2.halt_warns()) == 0  # 120s 内不重复提醒
    # 超过 300s → 再次提醒
    sched._halt_warn(t + timedelta(seconds=301))
    assert len(rec2.halt_warns()) == 1


# ---------------------------------------------------------------------------
# ⑦ halt_state() 暴露熔断态
# ---------------------------------------------------------------------------
def test_halt_state_exposes_tuple(tmp_path, monkeypatch):
    sched = _scheduler(tmp_path, StaleSignals(_sig(0, True)), FailingQuotes(fail_times=10))
    now = datetime(2026, 8, 24, 10, 0)
    state = sched.halt_state()
    assert state == (False, "", 0)
    for i in range(_THRESHOLD):
        sched._fetch_quotes_with_halt(now + timedelta(minutes=i))
    halted, reason, fails = sched.halt_state()
    assert halted is True
    assert reason != ""
    assert fails >= _THRESHOLD
