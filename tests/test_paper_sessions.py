"""T02 时段判定测试：按品种日盘/夜盘 + 节假日 + 开盘延迟 + 夜盘跨日归属（A2）。"""

from __future__ import annotations

from datetime import date, datetime, time

from omegaconf import OmegaConf

from hexbroker.paper.sessions import TradingSession, parse_sessions

# 关键日期（2026）：08-21 周五 / 08-22 周六 / 08-23 周日 / 08-24 周一 / 08-25 周二 / 10-01 周四(国庆)
FRI = date(2026, 8, 21)
MON = date(2026, 8, 24)
TUE = date(2026, 8, 25)
NATIONAL = date(2026, 10, 1)

_DAY = [["09:00", "10:15"], ["10:30", "11:30"], ["13:30", "15:00"]]


def _session() -> TradingSession:
    symbol_sessions = {
        "ag0": parse_sessions(_DAY, [["21:00", "02:30"]]),
        "rb0": parse_sessions(_DAY, [["21:00", "23:00"]]),
        "c0": parse_sessions(_DAY, []),
    }
    return TradingSession(
        symbol_sessions=symbol_sessions,
        holidays={NATIONAL},
        night_boundary=time(21, 0),
    )


def _dt(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm)


# ---------------------------------------------------------------------------
# 时段判定
# ---------------------------------------------------------------------------
def test_day_session_all_symbols_tradable():
    s = _session()
    ts = _dt(MON, 10, 0)
    assert s.is_tradable("ag0", ts)
    assert s.is_tradable("rb0", ts)
    assert s.is_tradable("c0", ts)


def test_night_session_ag_rb_tradable_c_not():
    s = _session()
    ts = _dt(MON, 21, 30)
    assert s.is_tradable("ag0", ts)
    assert s.is_tradable("rb0", ts)
    assert not s.is_tradable("c0", ts)


def test_night_crosses_midnight_ag_only():
    s = _session()
    ts = _dt(TUE, 1, 0)
    assert s.is_tradable("ag0", ts)   # ag 夜盘到 02:30，跨日
    assert not s.is_tradable("rb0", ts)  # rb 夜盘 23:00 已收
    assert not s.is_tradable("c0", ts)


def test_weekend_not_tradable():
    s = _session()
    ts = _dt(date(2026, 8, 22), 10, 0)  # 周六
    assert not s.is_trading_day(ts)
    assert not s.is_tradable("ag0", ts)


def test_holiday_not_tradable():
    s = _session()
    ts = _dt(NATIONAL, 10, 0)  # 国庆节（周四）
    assert s.is_trading_day(ts) is False
    assert not s.is_tradable("ag0", ts)


def test_out_of_session_not_tradable():
    s = _session()
    assert not s.is_tradable("c0", _dt(MON, 12, 0))   # 午休
    assert not s.is_tradable("rb0", _dt(MON, 16, 0))  # 日盘已收、夜盘未开


# ---------------------------------------------------------------------------
# 开盘延迟（Q6：跳过集合竞价，开盘后 N 分钟进入）
# ---------------------------------------------------------------------------
def test_open_delay_day_session():
    s = _session()
    assert not s.is_open_delay_passed("ag0", _dt(MON, 9, 2), delay_min=5)
    assert s.is_open_delay_passed("ag0", _dt(MON, 9, 6), delay_min=5)
    # 盘中节间复牌（10:30 / 13:30）不施加开盘延迟
    assert s.is_open_delay_passed("ag0", _dt(MON, 10, 32), delay_min=5)
    assert s.is_open_delay_passed("ag0", _dt(MON, 13, 32), delay_min=5)


def test_open_delay_night_session():
    s = _session()
    assert not s.is_open_delay_passed("ag0", _dt(MON, 21, 2), delay_min=5)
    assert s.is_open_delay_passed("ag0", _dt(MON, 21, 6), delay_min=5)


def test_open_delay_zero_disabled():
    s = _session()
    assert s.is_open_delay_passed("ag0", _dt(MON, 9, 1), delay_min=0)


# ---------------------------------------------------------------------------
# 夜盘跨日归属（§7.2 / §8.4）
# ---------------------------------------------------------------------------
def test_day_label_day_session_same_day():
    s = _session()
    assert s.day_label(_dt(FRI, 10, 0)) == FRI


def test_day_label_night_belongs_next_trading_day():
    s = _session()
    # 周五 21:00 夜盘 → 归属下一交易日（周一）
    assert s.day_label(_dt(FRI, 21, 30)) == MON
    # 周一 21:00 夜盘 → 归属周二
    assert s.day_label(_dt(MON, 21, 30)) == TUE


def test_day_label_weekend_skips_to_monday():
    s = _session()
    assert s.day_label(_dt(date(2026, 8, 22), 10, 0)) == MON  # 周六
    assert s.day_label(_dt(date(2026, 8, 23), 10, 0)) == MON  # 周日


def test_day_label_night_boundary_exact():
    s = _session()
    assert s.day_label(_dt(FRI, 21, 0)) == MON  # 21:00 整点即归属下一交易日


def test_next_trading_day_skips_holiday():
    s = _session()
    # 2026-09-30（周三）之后第一个交易日：10-01 国庆（唯一节假日）→ 落到 10-02（周五）
    nd = s.next_trading_day(date(2026, 9, 30))
    assert nd == date(2026, 10, 2)


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------
def test_from_config_parses_sessions_and_holidays():
    cfg = OmegaConf.create(
        {
            "symbols": {
                "ag0": {
                    "multiplier": 15,
                    "min_tick": 0.01,
                    "mode": "trade",
                    "sessions": {"day": _DAY, "night": [["21:00", "02:30"]]},
                },
                "c0": {
                    "multiplier": 10,
                    "min_tick": 1,
                    "mode": "accumulate",
                    "accumulate_days": 30,
                    "sessions": {"day": _DAY, "night": []},
                },
            },
            "holidays_2026": ["2026-10-01", "2026-10-02"],
        }
    )
    s = TradingSession.from_config(cfg)
    assert NATIONAL in s.holidays
    assert s.is_tradable("ag0", _dt(MON, 21, 30))
    assert not s.is_tradable("c0", _dt(MON, 21, 30))
    # ag 夜盘 21:00-02:30 含跨日 Session
    ag_sessions = s.sessions("ag0")
    assert any(ss.crosses_midnight for ss in ag_sessions)
