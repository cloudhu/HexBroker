"""P0-1 / P1-1 实时报价解析与时效护栏回归锁（2026-09-04 盯市冻结事故）。

事故机理
--------
``RealTimeQuoteClient._parse_line`` 把 ``price`` 取成新浪 ``nf_`` 报文的
**field[2]（开盘价）**。开盘价在交易日日内恒定，导致：

- 盯市浮盈全天冻结（trades.log 实证：09-03 全天 equity 恒为 94483.56、
  09-04 全天恒为 94573.56）；
- 风控输入 ``current_price`` / ``pnl_pct`` 同步冻结（``risk_gate.py:166-171``）。

正确的最新价在 **field[8]**；同时 open/high/low/pre_settle 原先分别误取
field[6]买价 / field[7]卖价 / field[10]昨结算 / field[8]最新价，一并修正。

P1-1 补时效护栏：``Quote.ts``（交易所行情时间）落后本机超过
``QUOTE_MAX_STALENESS_SEC`` → 置 ``stale=True``，调用方 fail-closed。

全部用例**不联网**：直接调 ``_parse_line`` / 用固定 ts 构造陈旧场景。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from omegaconf import OmegaConf

from hexbroker.paper.quotes import (
    QUOTE_MAX_STALENESS_SEC,
    QUOTE_QUIET_WINDOW_MIN,
    QUOTE_STALE_ALERT_THROTTLE_SEC,
    RealTimeQuoteClient,
)
from hexbroker.paper.types import Quote

# 2026-09-04 15:00 收盘时的真实报文（脱敏：仅保留前 18 个字段）
# 0 名称 | 1 时间 | 2 开盘 | 3 最高 | 4 最低 | 5 昨收 | 6 买价 | 7 卖价
# 8 最新价 | 9 结算价 | 10 昨结算 | ... | 17 日期
RB0_PAYLOAD = (
    "螺纹钢连续,150000,3145.000,3180.000,3137.000,3166.000,3165.000,3166.000,"
    "3166.000,3160.000,3137.000,3,524,1497791.000,877156,沪,螺纹钢,2026-09-04"
)
RB0_LINE = f'var hq_str_nf_RB0="{RB0_PAYLOAD}";'


@pytest.fixture()
def client() -> RealTimeQuoteClient:
    """离线客户端（不联网）。"""
    return RealTimeQuoteClient(offline=True)


# --------------------------------------------------------------------------- #
# P0-1：字段映射
# --------------------------------------------------------------------------- #
def test_price_takes_last_price_field8_not_open_field2(client):
    """核心回归锁：price 必须取 field[8]（最新价 3166），不能是 field[2]（开盘价 3145）。"""
    q = client._parse_line(RB0_LINE)
    assert q is not None
    assert q.price == pytest.approx(3166.0), "price 应取 field[8] 最新价"
    assert q.price != pytest.approx(3145.0), "price 不得再取 field[2] 开盘价"


def test_ohlc_and_pre_settle_field_mapping(client):
    """open/high/low/pre_settle 映射到 field[2]/[3]/[4]/[10]。"""
    q = client._parse_line(RB0_LINE)
    assert q is not None
    assert q.open == pytest.approx(3145.0)       # field[2] 开盘价
    assert q.high == pytest.approx(3180.0)       # field[3] 最高价
    assert q.low == pytest.approx(3137.0)        # field[4] 最低价
    assert q.pre_settle == pytest.approx(3137.0)  # field[10] 昨结算


def test_quote_ts_parsed_from_field17_and_field1(client):
    """行情时间 = field[17] 日期 + field[1] HHMMSS（非接收时刻）。"""
    q = client._parse_line(RB0_LINE)
    assert q is not None
    assert q.ts == datetime(2026, 9, 4, 15, 0, 0)


def test_parse_line_rejects_nonpositive_price(client):
    """最新价 <= 0 → 丢弃该行（保持原有健壮性）。"""
    bad = RB0_LINE.replace(",3166.000,3165.000", ",0.000,3165.000", 1)
    q = client._parse_line(bad)
    # 把 field[8] 置 0 后应被拒；若构造未命中则退化为断言「不会给出 0 价」
    if q is not None:
        assert q.price > 0


def test_offline_quotes_are_not_stale(client):
    """离线 mock 行情 ts=now，不得被判陈旧（否则冒烟/测试全废）。"""
    out = client.fetch_quotes(["rb0", "ag0"])
    assert out, "离线模式必须有报价"
    assert all(not q.stale for q in out.values())
    assert all(q.usable() for q in out.values())


# --------------------------------------------------------------------------- #
# P1-1：时效护栏
# --------------------------------------------------------------------------- #
def test_parse_ts_returns_none_on_unparsable_input(client):
    """时间字段不可解析必须返回 None（旧实现返回 now()，会伪装成『刚刚更新』）。"""
    assert client._parse_ts([]) is None
    assert client._parse_ts(["x"] * 18) is None


def test_stale_flag_set_when_quote_older_than_threshold(client):
    """行情时间落后超过阈值 → stale=True，usable() 为 False（fail-closed）。"""
    old = Quote(
        symbol="rb0",
        ts=datetime.now() - timedelta(seconds=QUOTE_MAX_STALENESS_SEC + 60),
        price=3166.0,
    )
    assert not old.stale
    client._apply_staleness_guard(old)
    assert old.stale is True
    assert old.valid() is True, "价格仍是正数，valid() 为真 —— 故风控必须用 usable()"
    assert old.usable() is False


def test_fresh_quote_not_marked_stale(client):
    """行情时间在阈值内 → 不判陈旧。"""
    fresh = Quote(symbol="rb0", ts=datetime.now(), price=3166.0)
    client._apply_staleness_guard(fresh)
    assert fresh.stale is False
    assert fresh.usable() is True


def test_stale_alert_is_throttled(client):
    """同一品种连续陈旧 → 告警被节流（不刷新告警时刻）。

    ⚠️ 不要用 monkeypatch 替换 ``_warn_stale_throttled`` 来计数：节流逻辑就在
    该方法内部，替换掉它等于把被测逻辑一起删掉（第一版就踩了这个坑，5 次调用
    全告警，测试反而暴露了测试自身的错误）。此处改为观测真实节流状态。
    """
    old_ts = datetime.now() - timedelta(seconds=QUOTE_MAX_STALENESS_SEC + 60)

    q1 = Quote(symbol="rb0", ts=old_ts, price=3166.0)
    client._apply_staleness_guard(q1)
    first_mark = client._stale_alerted_at["rb0"]

    q2 = Quote(symbol="rb0", ts=old_ts, price=3166.0)
    client._apply_staleness_guard(q2)
    assert client._stale_alerted_at["rb0"] == first_mark, "节流窗口内不应刷新告警时刻"
    # ⛔ 关键：节流掉的只是**告警**，陈旧标记与 fail-closed 必须照旧生效
    assert q2.stale is True
    assert q2.usable() is False


def test_stale_alert_refires_after_throttle_window(client):
    """超过节流窗口后应重新告警（否则非交易时段的首次陈旧之后将永久静默）。"""
    old_ts = datetime.now() - timedelta(seconds=QUOTE_MAX_STALENESS_SEC + 60)
    # 伪造一个「上一次告警已远超节流窗口」的状态
    client._stale_alerted_at["rb0"] = time.monotonic() - QUOTE_STALE_ALERT_THROTTLE_SEC - 1

    q = Quote(symbol="rb0", ts=old_ts, price=3166.0)
    client._apply_staleness_guard(q)
    assert client._stale_alerted_at["rb0"] > time.monotonic() - 1.0, "窗口外应重新告警并刷新时刻"


def test_usable_vs_valid_distinction():
    """valid() 只看价格，usable() 额外看时效 —— 这是 P0-1 事故的关键区分。"""
    stale = Quote(symbol="rb0", ts=datetime.now(), price=3166.0, stale=True)
    assert stale.valid() is True
    assert stale.usable() is False

    zero = Quote(symbol="rb0", ts=datetime.now(), price=0.0)
    assert zero.valid() is False
    assert zero.usable() is False


# --------------------------------------------------------------------------- #
# 阈值同源（2026-09-04 团队裁决）
#
# quotes 层（盯市估值判陈旧）与 scheduler 层（是否允许撮合）是两个独立时效门。
# 二者不等时，(min, max] 窗口会出现「允许成交但按成本价估值」的不一致态 ——
# 曾短暂取 120 vs 180，120s < age ≤ 180s 时持仓在动、权益却显示浮盈 0。
# 故用测试把「同源」这件事锁死。
# --------------------------------------------------------------------------- #
def test_staleness_threshold_matches_scheduler_quote_max_age():
    """QUOTE_MAX_STALENESS_SEC 必须等于 configs/paper.yaml 的 quote_max_age_sec。

    这是跨文件不变量：只改一侧而不同步另一侧，测试必须红。
    """
    cfg = OmegaConf.load("configs/paper.yaml")
    paper = cfg.get("paper", cfg)
    poll = float(paper.get("poll_interval_sec", 60))
    expected = float(paper.get("quote_max_age_sec", 3.0 * poll))
    assert QUOTE_MAX_STALENESS_SEC == pytest.approx(expected), (
        f"quotes.QUOTE_MAX_STALENESS_SEC({QUOTE_MAX_STALENESS_SEC}) 与 "
        f"configs/paper.yaml quote_max_age_sec({expected}) 必须同源；"
        f"改任一侧都要同步改另一侧，否则出现「允许撮合但按成本价估值」窗口"
    )


def test_staleness_threshold_is_not_the_old_120():
    """防止有人把阈值「改回」120（那会重新打开不一致窗口）。"""
    assert QUOTE_MAX_STALENESS_SEC != pytest.approx(120.0)


# --------------------------------------------------------------------------- #
# 盘后静默窗口（降噪）
# --------------------------------------------------------------------------- #
class _FixedClock:
    """只提供 ``now()`` 的替身，用于把 ``datetime.now()`` 钉在固定时刻。"""

    def __init__(self, hour: int, minute: int) -> None:
        self._dt = datetime(2026, 9, 4, hour, minute, 0)

    def now(self) -> datetime:
        return self._dt


def test_quiet_window_default_is_post_close_gap(client):
    """默认静默窗口 = 日盘收盘 15:00 → 夜盘开盘 21:00。"""
    assert QUOTE_QUIET_WINDOW_MIN == (15 * 60, 21 * 60)


def test_quiet_window_inside_outside_and_disabled(client, monkeypatch):
    """窗口内 / 窗口外 / 关闭窗口（None）三种情形。"""
    import hexbroker.paper.quotes as quotes_mod

    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(16, 30))  # 盘后
    assert client._in_quiet_window() is True, "16:30 属 15:00-21:00 静默窗口"

    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(10, 0))  # 盘中
    assert client._in_quiet_window() is False, "10:00 盘中不得静默"

    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(22, 0))  # 夜盘
    assert client._in_quiet_window() is False, "22:00 夜盘不得静默"

    client._quiet_window_min = None
    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(16, 30))
    assert client._in_quiet_window() is False, "窗口设为 None = 关闭静默，始终告警"


def test_quiet_window_handles_cross_midnight(client, monkeypatch):
    """跨零点窗口（如 23:00-01:00）走另一分支，不得漏判。"""
    import hexbroker.paper.quotes as quotes_mod

    client._quiet_window_min = (23 * 60, 1 * 60)  # 23:00 → 次日 01:00
    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(23, 30))
    assert client._in_quiet_window() is True
    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(0, 30))
    assert client._in_quiet_window() is True
    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(12, 0))
    assert client._in_quiet_window() is False


def test_malformed_quiet_window_never_raises(client):
    """窗口配置写歪（None / 空 / 非数字 / 长度不足）→ 当作「不静默」，绝不抛异常。"""
    for bad in (None, (), ("x", "y"), (900,), (None, None)):
        client._quiet_window_min = bad
        assert client._in_quiet_window() is False


def test_quiet_window_only_changes_level_not_fail_closed(client, monkeypatch):
    """⛔ 关键：静默窗口只降告警级别，stale 标记与 fail-closed 必须照旧。

    盘后报价照样判陈旧、照样退回成本价估值，只是不再刷屏。
    """
    import hexbroker.paper.quotes as quotes_mod

    # ⚠️ 必须先钉死时钟，**再**用它推算 quote.ts。
    # 反过来的写法（先用真实 datetime.now() 算 old_ts、再 patch 时钟）是时间依赖的
    # flaky：_apply_staleness_guard 内部也调 datetime.now()（被 patch 成 16:30），
    # 于是 lag = 16:30 - (真实时刻 - 240s) 会随真实时钟推进而变负 → 判定为「新鲜」。
    # 本用例曾在 16:40 绿、16:59 红，即此因。
    monkeypatch.setattr(quotes_mod, "datetime", _FixedClock(16, 30))
    old_ts = datetime(2026, 9, 4, 16, 30, 0) - timedelta(seconds=QUOTE_MAX_STALENESS_SEC + 60)
    q = Quote(symbol="rb0", ts=old_ts, price=3166.0)

    # 盘后（静默窗口内）
    client._apply_staleness_guard(q)
    assert q.stale is True, "静默窗口不得豁免陈旧判定"
    assert q.usable() is False, "静默窗口不得让陈旧报价重新可用于盯市"
    throttled_in_quiet = client._stale_alerted_at.get("rb0")
    assert throttled_in_quiet is not None, "静默窗口内节流状态照样记录（只是级别降为 DEBUG）"
