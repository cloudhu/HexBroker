"""P3-C 信号缓存新鲜度防护单测（交易日历 lag 口径，阈值 0）。

口径（见 ``hexbroker.paper.signals._trading_lag``）::

    lag = idx(最新已收盘交易日 R) - idx(信号日)
    R = asof 当日（若 asof 时刻 >= 15:00，即日盘已收盘）否则 asof 之前的最后一个交易日

    lag <= 0 → 新鲜（标准 T+1 或更新）→ 放行
    lag >= 1 → 落后 N 个交易日（缓存停更）→ is_effective=False → 无持仓禁开（§8.2）

覆盖：
① **周一放行**（P3-B 回归核心）：信号=上周五 → lag=0 → 放行；
② 假期后首日放行：信号=春节前最后交易日 → lag=0 → 放行；
③ 常规隔夜放行；
④ **P0-3 真病灶仍拦截**：夜盘当日已收盘但缓存停在上一交易日 → lag=1 → 拦截；
⑤ 同一信号在**日盘**（当日未收盘）→ lag=0 → 放行（④/⑤ 对照，证明 ref 定义正确）；
⑥ 缓存停更 N 个交易日 → 拦截（参数化 1..5）；
⑦ 交易日历不可用 → **保守拦截**（10**9）+ 不抛异常；
⑧ 阈值默认 0 + ``configs/paper.yaml`` 双向对齐 + ⛔ **旧键不得存在**（防口径回潮）；
⑨ ``_trading_lag`` 纯函数口径（含负值 / 无法判定 / 非交易日信号日）；
⑩ **全历史健康态护栏**（真实湖日历）：所有相邻交易日对 lag 恒为 0（零误伤）；
⑪ 交易日历必须是**全品种并集**（非单品种，否则单品种数据缺口会放宽门禁）；
⑫ 级联语义不变（主源过期但兜底更新 → 用兜底）。

⚠️ 单测**显式注入日历**（``trading_calendar=CAL``），不读主湖 ——
保证用例与文件系统解耦、结果确定。仅 ⑩/⑪ 两条刻意用真实湖做端到端护栏。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.paper.signals import (
    DEFAULT_LAKE_DIR,
    SignalEngine,
    _calendar_days,
    _to_date,
    _trading_lag,
    load_trading_calendar,
)

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 注入用迷你交易日历（周末休市 + 春节休市），与 2026 真实日历结构一致：
#   2026-02: 09~13 交易日 | 16~23 春节休市 | 24~27 交易日
#   2026-08: 17~21 | 24~28 | 31
#   2026-09: 01~04
# ---------------------------------------------------------------------------
_FEB = [dt.date(2026, 2, d) for d in (9, 10, 11, 12, 13, 24, 25, 26, 27)]
_AUG = [dt.date(2026, 8, d) for d in (17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31)]
_SEP = [dt.date(2026, 9, d) for d in (1, 2, 3, 4)]
CAL: tuple[dt.date, ...] = tuple(sorted(_FEB + _AUG + _SEP))

# 日盘（当日未收盘）与夜盘（当日 15:00 已收盘）的 asof 时刻
DAY = "10:30"
NIGHT = "21:30"


def _frame(rows: list[tuple]) -> pd.DataFrame:
    """构造与 ``_load_cache`` 输出等价的规范化信号表。"""
    df = pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ts": pd.to_datetime([r[1] for r in rows]),
            "p_up": [r[2] for r in rows],
            "exp_ret": [r[3] for r in rows],
            "is_effective": [r[4] for r in rows],
        }
    )
    return df.sort_values(["symbol", "ts"]).reset_index(drop=True)


@pytest.fixture
def make_engine(monkeypatch, tmp_path):
    """构造 SignalEngine（内存缓存 + 注入日历，不碰文件系统）。"""

    def _make(sources: list[list[tuple]], **kwargs) -> SignalEngine:
        frames = [_frame(rows) for rows in sources]
        state = {"i": 0}

        def _fake_load(self, cache_path: Path) -> pd.DataFrame:  # noqa: ANN001
            df = frames[state["i"]]
            state["i"] += 1
            return df

        monkeypatch.setattr(SignalEngine, "_load_cache", _fake_load)
        paths = [tmp_path / f"src{i}.parquet" for i in range(len(sources))]
        kwargs.setdefault("trading_calendar", CAL)
        return SignalEngine(cache_paths=paths, **kwargs)

    return _make


def _sig(eng: SignalEngine, asof: str) -> object:
    return eng.latest_signal("ag0", asof)


# ---------------------------------------------------------------------------
# ① 周一放行 —— P3-B 回归核心（自然日差口径下 fd=3 被误拦）
# ---------------------------------------------------------------------------
def test_monday_using_friday_signal_is_fresh(make_engine):
    """08-28(周五) 信号在 08-31(周一) 日盘使用 → lag=0 → 放行。

    这是 P3-B（自然日差，阈值 1）误伤的场景：自然日差 = 3 > 1 → 拦截。
    本断言直接钉死「周一必须放行」，退回自然日口径会立即失败。
    """
    eng = make_engine([[("ag0", "2026-08-28 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-31 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True  # 标准 T+1：上周五即周一的前一交易日
    # 对照：同一对日期在自然日差口径下是 3（即 P3-B 误判的依据）
    assert _calendar_days(_to_date("2026-08-28"), _to_date("2026-08-31")) == 3


# ---------------------------------------------------------------------------
# ② 假期后首日放行（np.busday_count 口径同样误伤：法定节假日被算作工作日）
# ---------------------------------------------------------------------------
def test_holiday_return_signal_is_fresh(make_engine):
    """02-13(春节前最后交易日) 信号在 02-24(节后首日) 使用 → lag=0 → 放行。"""
    eng = make_engine([[("ag0", "2026-02-13 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-02-24 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True
    assert _calendar_days(_to_date("2026-02-13"), _to_date("2026-02-24")) == 11


# ---------------------------------------------------------------------------
# ③ 常规隔夜放行
# ---------------------------------------------------------------------------
def test_normal_overnight_signal_is_fresh(make_engine):
    """08-27(周四) 信号在 08-28(周五) 日盘使用 → lag=0 → 放行。"""
    eng = make_engine([[("ag0", "2026-08-27 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-28 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


# ---------------------------------------------------------------------------
# ④ P0-3 真病灶：夜盘当日已收盘但缓存停在上一交易日 → 必须仍拦得住
# ---------------------------------------------------------------------------
def test_p03_night_stale_is_blocked(make_engine):
    """08-24 夜盘（当日 15:00 已收盘、数据可得）缓存仍是 08-21 → lag=1 → 拦截。

    这是 P0-3 事故场景。若 ref 错误地取「asof 之前的最后一个交易日」（= 08-21），
    会算出 lag=0 而放行，等于漏掉一整个已收盘交易日的信息 —— 本断言防此退化为。
    """
    eng = make_engine([[("ag0", "2026-08-21 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-24 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == 1
    assert sig.is_effective is False  # P0-3 意图完整保留
    assert sig.source == "engine_a"


def test_same_signal_is_allowed_in_day_session(make_engine):
    """⑤ 同一 08-21 信号放在 08-24 **日盘**（当日未收盘）→ lag=0 → 放行。

    与 ④ 构成对照：差别仅在日盘/夜盘（当日数据是否已可得），
    证明 ref 的定义是「最新**已收盘**交易日」而非「asof 之前的交易日」。
    """
    eng = make_engine([[("ag0", "2026-08-21 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-24 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


def test_night_after_refresh_is_fresh(make_engine):
    """20:30 刷新后缓存含当日信号 → 夜盘 lag=0 → 放行（正常路径）。"""
    eng = make_engine([[("ag0", "2026-08-24 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-24 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


# ---------------------------------------------------------------------------
# ⑥ 缓存停更 N 个交易日 → 拦截（参数化）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_stale", [1, 2, 3, 5])
def test_cache_stopped_updating_is_blocked(make_engine, n_stale: int):
    """缓存停在 R 之前第 n 个交易日 → lag=n > 阈值 0 → 拦截。"""
    # 以 08-31 夜盘为 ref（R=08-31），往前数 n_stale 个交易日作为信号日
    ref_idx = CAL.index(dt.date(2026, 8, 31))
    sig_day = CAL[ref_idx - n_stale]
    eng = make_engine([[("ag0", f"{sig_day} 15:00", 0.80, 1.0, True)]])
    sig = _sig(eng, f"2026-08-31 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == n_stale
    assert sig.is_effective is False


# ---------------------------------------------------------------------------
# ⑦ 交易日历不可用 → 保守拦截 + 不抛异常
# ---------------------------------------------------------------------------
def test_unavailable_calendar_blocks_conservatively(make_engine):
    """日历为空 → lag 记 10**9（保守拦截），且**不得**抛异常或静默放行。"""
    eng = make_engine(
        [[("ag0", "2026-08-28 15:00", 0.80, 1.0, True)]], trading_calendar=[]
    )
    sig = _sig(eng, f"2026-08-31 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 10 ** 9
    assert sig.is_effective is False
    # ⛔ 空注入不得被真实日历偷偷覆盖（曾因 `if not self._calendar` 判断而失效）
    assert eng.trading_calendar == ()


# ---------------------------------------------------------------------------
# ⑧ 阈值与配置对齐
# ---------------------------------------------------------------------------
def test_freshness_threshold_default_is_zero(make_engine):
    """默认阈值 0 = 信号须覆盖最近一个已收盘交易日（标准 T+1）。"""
    eng = make_engine([[("ag0", "2026-08-28 09:00", 0.6, 0.2, True)]])
    assert eng.freshness_threshold == 0


def test_paper_yaml_threshold_is_zero():
    """configs/paper.yaml 与代码默认值一致，均为 0（交易日 lag 口径）。"""
    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    assert int(cfg.paper.freshness_threshold_trading_days) == 0


def test_paper_yaml_has_no_legacy_natural_day_key():
    """⛔ 防回潮：废弃键 ``freshness_threshold_days``（自然日差口径）不得再出现。

    该键在 P3-B 下为 1（误伤周一 21.36%）。若重新出现，说明有人按旧口径改配置。
    """
    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    assert "freshness_threshold_days" not in cfg.paper, (
        "检测到已废弃的自然日差口径键 freshness_threshold_days；"
        "现行口径为 freshness_threshold_trading_days（交易日 lag，阈值 0）"
    )


# ---------------------------------------------------------------------------
# ⑨ _trading_lag 纯函数口径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sig,ref,expected",
    [
        ("2026-08-28", "2026-08-31 10:30", 0),    # 周一用上周五信号（标准 T+1）
        ("2026-08-27", "2026-08-28 10:30", 0),    # 常规隔夜
        ("2026-02-13", "2026-02-24 10:30", 0),    # 长假后首日
        ("2026-08-21", "2026-08-24 21:30", 1),    # P0-3 病灶：夜盘漏刷
        ("2026-08-21", "2026-08-24 10:30", 0),    # 同信号在日盘 → 放行
        ("2026-08-24", "2026-08-24 21:30", 0),    # 夜盘当日已刷新
        ("2026-08-24", "2026-08-24 10:30", -1),   # 负 lag：信号比标准 T+1 更新
        ("2026-08-17", "2026-08-31 21:30", 10),   # 停更 10 个交易日
    ],
)
def test_trading_lag_semantics(sig: str, ref: str, expected: int):
    """交易日 lag 口径：与星期几/假期长度无关，只数交易日。"""
    ref_obj = dt.datetime.fromisoformat(ref)
    assert _trading_lag(dt.date.fromisoformat(sig), ref_obj, CAL) == expected


def test_trading_lag_returns_none_when_undeterminable():
    """无法判定的三种情形 → None（调用方转为保守拦截）。"""
    # 空日历
    assert _trading_lag(dt.date(2026, 8, 28), dt.datetime(2026, 8, 31, 10, 30), []) is None
    # 信号日不是交易日（周末）
    assert _trading_lag(dt.date(2026, 8, 29), dt.datetime(2026, 8, 31, 10, 30), CAL) is None
    # ref 早于日历起点
    assert _trading_lag(dt.date(2026, 8, 28), dt.datetime(2017, 1, 1, 10, 30), CAL) is None
    # 任一侧为 None
    assert _trading_lag(None, dt.datetime(2026, 8, 31, 10, 30), CAL) is None


def test_trading_lag_accepts_pure_date_as_pre_open():
    """纯 date（无时刻）视为盘前 → ref 取之前最后一个交易日。"""
    # 08-28 是交易日，但纯日期 → 未收盘 → ref=08-27；信号 08-28 → lag=-1
    assert _trading_lag(dt.date(2026, 8, 28), dt.date(2026, 8, 28), CAL) == -1
    # 信号 08-27 → lag=0（标准 T+1，启动自检语义）
    assert _trading_lag(dt.date(2026, 8, 27), dt.date(2026, 8, 28), CAL) == 0


# ---------------------------------------------------------------------------
# ⑩ 全历史健康态护栏（真实湖日历）—— 零误伤的决定性证据
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not DEFAULT_LAKE_DIR.exists(), reason="主湖缺失（CI 无数据），跳过端到端日历护栏"
)
def test_real_calendar_healthy_sample_has_zero_false_blocks():
    """健康态（信号恒 = 上一交易日）在全历史真实日历上必须 **lag 恒为 0**。

    这是「口径不误伤」的决定性护栏：若改为自然日差，448/2097 天会 >0（21.36%）。
    """
    cal = load_trading_calendar()
    assert len(cal) > 100, f"交易日历过短（{len(cal)} 天），主湖数据疑似缺失"
    bad = [
        (cal[i], cal[i - 1], _trading_lag(cal[i - 1], dt.datetime.combine(cal[i], dt.time(10, 30)), cal))
        for i in range(1, len(cal))
        if _trading_lag(cal[i - 1], dt.datetime.combine(cal[i], dt.time(10, 30)), cal) != 0
    ]
    assert not bad, f"健康态样本出现误伤（前 5 个）：{bad[:5]}"


@pytest.mark.skipif(
    not DEFAULT_LAKE_DIR.exists(), reason="主湖缺失（CI 无数据），跳过端到端日历护栏"
)
def test_real_calendar_still_blocks_p03_lesion():
    """真实日历下 P0-3 病灶（08-24 夜盘用 08-21 信号）必须仍被拦截。"""
    cal = load_trading_calendar()
    for d in (dt.date(2026, 8, 21), dt.date(2026, 8, 24)):
        assert d in cal, f"{d} 不在真实交易日历内，用例前提失效"
    lag = _trading_lag(dt.date(2026, 8, 21), dt.datetime(2026, 8, 24, 21, 30), cal)
    assert lag == 1  # 落后一个已收盘交易日 → 拦截


# ---------------------------------------------------------------------------
# ⑪ 交易日历必须是全品种并集（非单品种）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not DEFAULT_LAKE_DIR.exists(), reason="主湖缺失（CI 无数据），跳过日历并集护栏"
)
def test_trading_calendar_is_union_not_per_symbol():
    """交易日历须覆盖**所有**品种日期的并集。

    反例（若退回单品种日历）：sc0 比并集少 54 天、ni0 少 1 天 ——
    单品种的数据缺口会让「上一交易日」被前移，把缓存停更误判为新鲜（门禁变松）。
    """
    cal = set(load_trading_calendar())
    sym_dirs = [d for d in DEFAULT_LAKE_DIR.iterdir() if d.is_dir()]
    assert sym_dirs, "主湖无品种目录"
    per_symbol_max = 0
    for d in sym_dirs:
        days: set[dt.date] = set()
        for f in (d / "1d").glob("*.parquet"):
            days |= set(pd.to_datetime(pd.read_parquet(f, columns=["datetime"])["datetime"]).dt.date)
        per_symbol_max = max(per_symbol_max, len(days))
        assert days <= cal, f"{d.name} 存在不在并集内的日期（并集定义被破坏）"
    assert len(cal) >= per_symbol_max


# ---------------------------------------------------------------------------
# ⑫ 级联语义不变 + 别名清理
# ---------------------------------------------------------------------------
def test_cascade_still_prefers_fresher_backup(make_engine):
    """主源落后但兜底更新 → 用兜底（跨口径不变的行为）。"""
    eng = make_engine(
        [
            [("ag0", "2026-08-21 15:00", 0.80, 1.0, True)],   # 主源：08-24 夜盘下 lag=1 → 过期
            [("ag0", "2026-08-24 15:00", 0.62, 0.5, True)],   # 兜底：当日 → lag=0
        ]
    )
    sig = _sig(eng, f"2026-08-24 {NIGHT}")
    assert sig is not None
    assert sig.source == "engine_a_fb1"
    assert sig.freshness_days == 0
    assert sig.is_effective is True
    assert sig.p_up == pytest.approx(0.62)


def test_legacy_business_days_alias_is_removed():
    """⛔ ``_business_days`` 别名必须已移除（名不副实，历史两次误导口径）。"""
    import hexbroker.paper.signals as signals

    assert not hasattr(signals, "_business_days")


def test_cache_is_effective_false_is_respected(make_engine):
    """缓存自身 is_effective=False 时，即使 lag=0 也不放行（原语义不变）。"""
    eng = make_engine([[("ag0", "2026-08-28 15:00", 0.80, 1.0, False)]])
    sig = _sig(eng, f"2026-08-31 {DAY}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is False
