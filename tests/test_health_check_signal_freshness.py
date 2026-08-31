"""启动自检：信号缓存新鲜度 WARN（不阻断启动）—— **P3-C 交易日历 lag 口径**。

覆盖：

① 陈旧缓存（交易日 lag > 阈值）→ CheckItem WARN + 刷新命令提示；
② 新鲜缓存（lag=0，标准 T+1）→ CheckItem OK；
③ **周一用上周五信号必须放行**（P3-B 结构性误拦的回归护栏）；
④ **夜盘漏刷必须拦截**（P0-3 事故场景，同行信号在日盘放行作对照）；
⑤ 阈值取配置值（缺失回退 0）；
⑥ 缓存文件缺失 → FAIL（原语义不变）；
⑦ ``signal_freshness_days`` 与 ``SignalEngine`` **同口径**；
⑧ 生命周期参数展示阈值语义（0=正常值，与 P0-3 时代的病态 0 语义**相反**）。

口径变更（P3-C，2026-08-31）：自然日差 → **交易日历 lag**，与
``hexbroker.paper.signals._trading_lag`` 同口径。两代旧口径的前车之鉴：

====  ============================  ======  ======================================
代    度量                          阈值    结果
====  ============================  ======  ======================================
P0-3  ``np.busday_count`` 工作日差  0       盘中 lag 恒 ≥1 → 日盘永不主源开仓
P3-B  自然日差                      1       周一/假期后首日误拦 **21.36%**（已证伪）
P3-C  交易日历 lag（现行）          0       健康态误拦 **0.00%**，停更 1 日即拦
====  ============================  ======  ======================================

本文件的 ③ 即 P3-B 的直接回归护栏：同一组（信号=08-21 周五，asof=08-24 周一）在
旧口径下 fd=3>阈值1 → WARN（误拦），新口径下 lag=0<=阈值0 → OK（正确放行）。

注：不写真实 parquet（沙箱可能缺 pyarrow），改为注入 ``pandas.read_parquet``；
交易日历亦显式注入，使用例与文件系统解耦（CI 环境主湖可能缺失）。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.diagnostics.health_check import (
    check_data_sources,
    check_lifecycle,
    signal_freshness_days,
)
from hexbroker.paper.signals import _calendar_days, _to_date, _trading_lag

# ---------------------------------------------------------------------------
# 迷你交易日历（2026-02 含春节休市 2/14~2/23；2026-08/09 常规）
# ---------------------------------------------------------------------------
CAL: tuple[date, ...] = tuple(
    sorted(
        [date(2026, 2, d) for d in (9, 10, 11, 12, 13, 24, 25, 26, 27)]
        + [date(2026, 8, d) for d in (17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31)]
        + [date(2026, 9, d) for d in (1, 2, 3, 4)]
    )
)

# 日盘 10:30（当日未收盘 → R 回退到 08-21 上周五）／夜盘 21:30（当日已收盘 → R=当日）
ASOF_DAY = datetime(2026, 8, 24, 10, 30)  # 周一
ASOF_NIGHT = datetime(2026, 8, 24, 21, 30)  # 周一夜盘


@pytest.fixture(autouse=True)
def _calendar(inject_calendar):
    """注入迷你交易日历，使用例不依赖主湖（夹具见 ``tests/conftest.py``）。"""
    return inject_calendar(CAL)


def _cfg(cache_paths: list[str], threshold: int | None = 0) -> OmegaConf:
    body: dict = {"signal_caches": cache_paths, "quote_url": "https://hq.sinajs.cn/list="}
    if threshold is not None:
        body["freshness_threshold_trading_days"] = threshold
    return OmegaConf.create(body)


@pytest.fixture
def fake_cache(monkeypatch, tmp_path):
    """创建占位缓存文件并注入 read_parquet（返回指定最新 ts 的信号表）。"""

    def _make(name: str, latest_ts: str) -> str:
        path = tmp_path / name
        path.write_bytes(b"placeholder")
        df = pd.DataFrame(
            {
                "symbol": ["ag0", "rb0"],
                "ts": pd.to_datetime([latest_ts, latest_ts]),
                "p_up": [0.6, 0.55],
                "exp_ret": [0.5, 0.3],
                "is_effective": [True, True],
            }
        )
        monkeypatch.setattr(pd, "read_parquet", lambda *_a, **_k: df)
        return str(path)

    return _make


def _cache_items(items: list) -> list:
    return [it for it in items if it.name.startswith("信号缓存")]


# ---------------------------------------------------------------------------
# ① 陈旧缓存 → WARN
# ---------------------------------------------------------------------------
def test_stale_cache_reports_warn(fake_cache):
    """信号 08-19(周三)，asof 08-24 10:30（R=08-21）→ lag=2 > 阈值 0 → WARN。"""
    path = fake_cache("signals_stale.parquet", "2026-08-19 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_DAY))
    assert len(items) == 1
    assert items[0].status == "WARN"
    assert "信号陈旧 fd=2>阈值0" in items[0].detail  # 交易日 lag 口径
    assert "p22_tail_ext.py --skip-eval" in items[0].detail
    assert "2026-08-19 15:00" in items[0].detail


def test_very_stale_cache_reports_trading_day_gap(fake_cache):
    """06-29 缓存（不在迷你日历内 → lag 无法判定）与春节前后跨越分别验证。

    前者：信号日不在交易日日历 → ``_trading_lag`` 返回 None → 跳过新鲜度检查（不 WARN）。
    后者：信号 02-13（春节前最后交易日），asof 02-24 10:30（R=02-23? 不在日历 → R=02-13）
          → lag=0 → OK（假期后首日不误拦，正是 P3-B 的病灶之一）。
    """
    # 信号日非交易日 → 无法判定 → 不计陈旧（保守方向由 SignalEngine 侧的 None→拦截 兜底）
    path = fake_cache("signals_nonday.parquet", "2026-06-29 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_DAY))
    assert items[0].status == "OK"
    assert "新鲜度" not in items[0].detail

    # 春节后首日：节前信号必须放行
    path2 = fake_cache("signals_cny.parquet", "2026-02-13 15:00")
    items2 = _cache_items(
        check_data_sources(_cfg([path2]), offline=True, asof=datetime(2026, 2, 24, 10, 30))
    )
    assert items2[0].status == "OK"
    assert "fd=0<=阈值0" in items2[0].detail
    # 对照：自然日差口径下为 11 天（P3-B 会误拦）
    assert _calendar_days(_to_date("2026-02-13"), date(2026, 2, 24)) == 11


# ---------------------------------------------------------------------------
# ② 新鲜缓存 → OK
# ---------------------------------------------------------------------------
def test_fresh_cache_reports_ok(fake_cache):
    """信号 08-21(周五) = R 本身 → lag=0 <= 阈值 0 → OK。"""
    path = fake_cache("signals_fresh.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_DAY))
    assert items[0].status == "OK"
    assert "fd=0<=阈值0" in items[0].detail
    assert "2 行" in items[0].detail


# ---------------------------------------------------------------------------
# ③ 周一放行（P3-B 结构性误拦的回归护栏）
# ---------------------------------------------------------------------------
def test_monday_cache_not_blocked(fake_cache):
    """⛔ 回归护栏：周一用上周五信号**必须放行**。

    P3-B 自然日差口径下 fd=3>阈值1 → WARN（误拦），导致周一/假期后首日
    主源被静默禁用（全历史实测 448/2097 = 21.36%）。本口径 lag=0 → OK。
    """
    path = fake_cache("signals_monday.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_DAY))
    assert items[0].status == "OK", items[0].detail
    assert "fd=0<=阈值0" in items[0].detail
    # 反向对照：确认同一组输入在旧口径下确实会被误拦（证明本断言有鉴别力）
    assert _calendar_days(_to_date("2026-08-21"), ASOF_DAY.date()) == 3


# ---------------------------------------------------------------------------
# ④ 夜盘漏刷拦截（P0-3 事故场景）+ 日盘同信号放行（对照）
# ---------------------------------------------------------------------------
def test_night_session_stale_cache_blocked(fake_cache):
    """P0-3 事故场景：08-24 夜盘（当日日盘 15:00 已收盘 → R=08-24）缓存仍停在 08-21。

    lag = idx(08-24) - idx(08-21) = 1 → 必须 WARN。
    ⛔ 若 R 错取为「asof 之前的最后一个交易日」（=08-21）会算出 lag=0 而放行，
    等于漏掉整整一个已收盘交易日 —— 这正是 P0-3 事故的口径。
    """
    path = fake_cache("signals_night_stale.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_NIGHT))
    assert items[0].status == "WARN"
    assert "信号陈旧 fd=1>阈值0" in items[0].detail


def test_same_cache_passes_in_day_session(fake_cache):
    """④ 的对照：同一个 08-21 缓存在**日盘** 10:30 自检（R=08-21）→ lag=0 → 放行。

    两组用例合并即证明「放行/拦截由 asof 时刻决定，而非信号日」，
    是 R 定义（当日日盘是否已收盘）的判决性护栏。
    """
    path = fake_cache("signals_day_ok.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF_DAY))
    assert items[0].status == "OK"
    assert "fd=0<=阈值0" in items[0].detail


# ---------------------------------------------------------------------------
# ⑤ 阈值取配置（宽松阈值 → 陈旧仍 OK；缺失 → 回退 0）
# ---------------------------------------------------------------------------
def test_threshold_from_config_allows_stale_when_relaxed(fake_cache):
    """配置阈值 3 时信号 08-18（lag=3）→ 仍判 OK（阈值来源于配置，非硬编码）。"""
    path = fake_cache("signals_relaxed.parquet", "2026-08-18 15:00")
    items = _cache_items(check_data_sources(_cfg([path], threshold=3), offline=True, asof=ASOF_DAY))
    assert items[0].status == "OK"
    assert "fd=3<=阈值3" in items[0].detail


def test_threshold_defaults_to_zero_when_missing(fake_cache):
    """配置缺失 freshness_threshold_trading_days → 回退 0 → 落后 1 交易日即 WARN。"""
    path = fake_cache("signals_nothr.parquet", "2026-08-20 15:00")
    items = _cache_items(
        check_data_sources(_cfg([path], threshold=None), offline=True, asof=ASOF_DAY)
    )
    assert items[0].status == "WARN"
    assert "阈值0" in items[0].detail


# ---------------------------------------------------------------------------
# ⑥ 缺失文件 → FAIL（原语义不变）
# ---------------------------------------------------------------------------
def test_missing_cache_still_fails(tmp_path):
    missing = str(tmp_path / "not_exist.parquet")
    items = _cache_items(check_data_sources(_cfg([missing]), offline=True, asof=ASOF_DAY))
    assert items[0].status == "FAIL"
    assert "文件缺失" in items[0].detail


# ---------------------------------------------------------------------------
# ⑦ 与 SignalEngine 同口径（交易日历 lag）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sig_day,expected",
    [
        ("2026-08-21", 0),  # 上一交易日（= R）→ 标准 T+1
        ("2026-08-20", 1),  # 落后 1 交易日
        ("2026-08-19", 2),
        ("2026-08-17", 4),  # 跨整周（自然日差 7 — P3-B 旧口径值）
    ],
)
def test_freshness_days_matches_signal_engine_semantics(sig_day: str, expected: int):
    got = signal_freshness_days(pd.Timestamp(f"{sig_day} 15:00"), ASOF_DAY)
    assert got == expected
    # 与纯函数 _trading_lag 逐例对齐，杜绝两处实现漂移
    assert got == _trading_lag(_to_date(sig_day), ASOF_DAY, CAL)


def test_freshness_days_differs_from_calendar_days_for_weekend():
    """⛔ 防回潮：跨周末的 lag(0) 与自然日差(3) 必须不同。

    两者若相等即说明口径被改回自然日差（P3-B），周一误拦会复发。
    """
    sig = pd.Timestamp("2026-08-21 15:00")
    assert signal_freshness_days(sig, ASOF_DAY) == 0
    assert _calendar_days(_to_date("2026-08-21"), ASOF_DAY.date()) == 3


def test_freshness_days_none_when_no_timestamp():
    assert signal_freshness_days(None, ASOF_DAY) is None


# ---------------------------------------------------------------------------
# ⑧ 生命周期阈值语义（0 = 正常值，与 P0-3 时代的病态 0 语义相反）
# ---------------------------------------------------------------------------
def test_lifecycle_shows_zero_threshold_semantics():
    """阈值 0（生产值）→ 标准 T+1 正常值，且明确周一/假期后首日放行。"""
    cfg = OmegaConf.create(
        {
            "freshness_threshold_trading_days": 0,
            "symbols": {"ag0": {"mode": "trade"}},
            "holidays_2026": [],
        }
    )
    items = [it for it in check_lifecycle(cfg) if it.name == "信号新鲜度阈值"]
    assert len(items) == 1
    assert "lag<=0 交易日" in items[0].detail
    assert "标准 T+1" in items[0].detail
    assert "周一用周五信号" in items[0].detail
    # ⛔ 反向护栏：P0-3 时代的「0=盘中不可达」文案不得复现
    assert "不可达" not in items[0].detail


def test_lifecycle_shows_relaxed_threshold_semantics():
    """阈值 2 → 提示放宽至落后 2 个交易日仍放行。"""
    cfg = OmegaConf.create(
        {
            "freshness_threshold_trading_days": 2,
            "symbols": {"ag0": {"mode": "trade"}},
            "holidays_2026": [],
        }
    )
    items = [it for it in check_lifecycle(cfg) if it.name == "信号新鲜度阈值"]
    assert len(items) == 1
    assert "lag<=2 交易日" in items[0].detail
    assert "放宽至落后 2 个交易日" in items[0].detail
