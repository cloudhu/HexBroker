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
⑫ 级联语义不变（主源过期但兜底更新 → 用兜底）；
⑬ **夜盘门禁**（P3-C 补丁）：停更恰好 1 交易日必须拦截（旧版漏放的判别性场景）、
   当日已刷新必须放行（行为反转修复）、未来信号日不得污染日历、日盘语义不受增补影响。

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
    augment_calendar,
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


# ---------------------------------------------------------------------------
# ⑬ 夜盘门禁（P3-C 补丁，2026-08-31；QA 独立复核揪出的 P0 缺陷）
#
# 缺陷：初版 `bisect_right(cal, ref_day) - 1` 在 ref_day **不在日历**时静默
# 回退到上一交易日。主湖日线次日才补数 ⇒ 夜盘时刻主湖恒无当日 ⇒ 缓存停更
# 整整一个交易日却算出 lag=0 → **放行**（门禁失明），正是 P0-3 事故形态；
# 而 20:30 老实刷出当日信号的反被拦（行为反转）。
#
# ⛔ 判别性场景只有「停更**恰好 1 个交易日**」：停更 ≥2 时旧版也拦得住。
# ---------------------------------------------------------------------------
# 模拟「主湖滞后」的日历：不含当日 08-31（夜盘补数执行前的真实状态）
CAL_NO_TODAY: tuple[dt.date, ...] = tuple(d for d in CAL if d <= dt.date(2026, 8, 28))
D0831 = dt.date(2026, 8, 31)


@pytest.mark.parametrize(
    "cal,label",
    [(CAL, "日历已含当日"), (CAL_NO_TODAY, "主湖滞后（靠独立交易日判定增补）")],
)
def test_night_stale_exactly_one_trading_day_is_blocked(make_engine, cal, label):
    """🔴 判别性场景：夜盘缓存停更**恰好 1 个交易日** → 必须拦截（两种日历状态都要拦）。

    旧版在此算 lag=0 放行（门禁失明 = P0-3 事故原样复现）—— 这是旧版唯一漏放、
    也是唯一的判别性场景（停更 ≥2 时旧版本来就拦得住）。

    主湖滞后时当日不在日历里，靠 :func:`augment_calendar` 的**独立交易日判定**
    （周一至周五 + 节假日表）把当日补回日历，从而给出**可执行的确定值** lag=1，
    而不是「无法判定」。
    """
    eng = make_engine(
        [[("ag0", "2026-08-28 15:00", 0.80, 1.0, True)]],  # 缓存停在上一交易日
        trading_calendar=cal,
    )
    sig = _sig(eng, f"2026-08-31 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == 1, f"{label}：应给出确定值 1（旧版漏放为 0）"
    assert sig.is_effective is False


def test_augment_calendar_skips_non_trading_ref_day(make_engine):
    """⛔ ref_day **不是**交易日（周六）→ 不得增补 → 夜盘无法判定 → 保守拦截。

    独立交易日判定只把真正的交易日补进日历；休市日不得被当成 R。
    """
    eng = make_engine(
        [[("ag0", "2026-08-28 15:00", 0.80, 1.0, True)]],
        trading_calendar=CAL_NO_TODAY,
    )
    sig = _sig(eng, "2026-08-29 21:30")   # 周六
    assert sig is not None
    assert sig.freshness_days == 10 ** 9  # None → 保守值
    assert sig.is_effective is False


@pytest.mark.parametrize("n_lag", [2, 3, 5])
def test_night_stale_more_than_one_day_also_blocked(make_engine, n_lag):
    """停更 ≥2 个交易日同样拦截（旧版本就拦得住，此条防补丁引入回归）。"""
    cal = list(CAL)
    idx = cal.index(D0831)
    sig_day = cal[idx - n_lag]
    eng = make_engine(
        [[("ag0", f"{sig_day.isoformat()} 15:00", 0.80, 1.0, True)]],
        trading_calendar=CAL,                 # 当日已在日历 → 可算出具体 lag
    )
    sig = _sig(eng, f"2026-08-31 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == n_lag
    assert sig.is_effective is False


def test_night_fresh_same_day_signal_passes_via_calendar_augmentation(make_engine):
    """🔴 行为反转修复：夜盘当日已刷出信号，即使主湖仍无当日 → 放行。

    旧版在此返回 None 拦截 —— **越新的信号越被拦**。修复靠 `_calendar_for`
    把「缓存中 ≤ ref_day 的信号日」并入日历（信号是对已有 bar 打分的，
    缓存里存在某日信号 ⇒ 该日是交易日，不依赖主湖是否已补数）。
    """
    eng = make_engine(
        [[("ag0", "2026-08-31 15:00", 0.80, 1.0, True)]],  # 20:30 已刷新出当日信号
        trading_calendar=CAL_NO_TODAY,                      # 主湖滞后，无当日
    )
    sig = _sig(eng, f"2026-08-31 {NIGHT}")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


def test_calendar_augmentation_excludes_future_signal_days(make_engine):
    """⛔ 缓存里混入的**未来日期**信号日，不得进入生效日历（防污染 R）。

    `_calendar_for` 只纳入 ``<= ref_day`` 的信号日，否则未来日期会把 R 推到
    未来，门禁失真。
    """
    eng = make_engine(
        [[
            ("ag0", "2026-08-31 15:00", 0.80, 1.0, True),   # 合法：当日
            ("ag0", "2026-09-15 15:00", 0.90, 2.0, True),   # 未来日期（数据污染）
        ]],
        trading_calendar=CAL_NO_TODAY,
    )
    cal = eng._calendar_for(D0831)
    assert D0831 in cal, "当日信号日必须被并入（否则行为反转）"
    assert dt.date(2026, 9, 15) not in cal, "未来信号日不得污染生效日历"


def test_day_session_semantics_unchanged_by_augmentation(make_engine):
    """日盘语义不因日历增补而改变：R 恒为**上一交易日**。

    增补只把「当日」加进日历；日盘 R 取 `bisect_left(cal, T) - 1`，
    仍落在上一交易日 → lag 不变。
    """
    rows = [("ag0", "2026-08-28 15:00", 0.80, 1.0, True)]
    eng_with = make_engine([rows], trading_calendar=CAL)                  # 含当日
    eng_without = make_engine([rows], trading_calendar=CAL_NO_TODAY)      # 不含当日
    s_with = _sig(eng_with, f"2026-08-31 {DAY}")
    s_without = _sig(eng_without, f"2026-08-31 {DAY}")
    assert s_with.freshness_days == 0
    assert s_without.freshness_days == 0, "日盘 R 取上一交易日，日历是否含当日不影响结果"


def test_trading_lag_night_requires_exact_match():
    """`_trading_lag` 纯函数：夜盘当日**必须严格命中**，绝不回退到上一交易日。"""
    cal_with = CAL
    cal_without = CAL_NO_TODAY
    asof_night = dt.datetime(2026, 8, 31, 21, 30)
    # 当日不在日历 → 无法判定（None），不是 0
    assert _trading_lag(D0831, asof_night, cal_without, dt.time(15, 0)) is None
    # 当日在日历 → 严格命中 → 0
    assert _trading_lag(D0831, asof_night, cal_with, dt.time(15, 0)) == 0
    # 停更 1 个交易日（当日在日历）→ 1
    assert _trading_lag(dt.date(2026, 8, 28), asof_night, cal_with, dt.time(15, 0)) == 1


# ---------------------------------------------------------------------------
# ⑭ 日历增补的覆盖边界 —— 只补主湖之外，绝不改写主湖已知历史
#    （2026-08-31 QA 独立复核揪出的「脏信号日污染」，观察项 1）
# ---------------------------------------------------------------------------
# 迷你日历里 2026-02-16~23 是春节休市（不在 CAL 中），且远早于 cal_max(09-04)，
# 是「主湖覆盖区间内的非交易日」的等价替身 —— 与生产缓存里的 4 个脏信号日
# （2020-10-02 / 2021-10-01 / 2022-04-04 / 2024-06-10）同构。
DIRTY_SIG_DAY = dt.date(2026, 2, 17)   # 春节休市期间的脏信号日
PRE_HOLIDAY = dt.date(2026, 2, 13)     # 休市前最后一个交易日
FIRST_AFTER_HOLIDAY = dt.date(2026, 2, 24)  # 休市后首个交易日


def test_augment_calendar_never_rewrites_lake_covered_range():
    """⛔⛔ 主湖覆盖区间内的非交易日信号日，绝不得进入生效日历。

    铁律：``augment_calendar`` 只补 ``> cal_max`` 的日期。主湖并集对它所覆盖的
    区间是**权威记录**（天然含法定休市），该区间内「不在主湖里」=「不是交易日」，
    不允许用「缓存里有信号」去反推。
    """
    cal_before = CAL
    assert DIRTY_SIG_DAY not in cal_before, "用例前提：脏日期本就不在主湖日历中"
    assert DIRTY_SIG_DAY < cal_before[-1], "用例前提：脏日期位于主湖覆盖区间之内"

    cal_after = augment_calendar(
        CAL, ref_day=FIRST_AFTER_HOLIDAY, signal_days=[DIRTY_SIG_DAY]
    )
    assert DIRTY_SIG_DAY not in cal_after, "主湖覆盖区间内的脏信号日污染了生效日历"
    assert cal_after == cal_before, "生效日历被改写（主湖已知历史被污染）"


def test_dirty_signal_day_must_not_shift_reference_point():
    """脏日期不得把日盘参考点 R 从「休市前最后交易日」推到脏日期上。

    ⚠️ 场景代表性说明（2026-08-31 QA 复核指正）：本用例**手工指定被判对象**
    （``_trading_lag(PRE_HOLIDAY, ...)``），对应「脏日期之后、T 之前还有别的
    健康信号日」的隐患形态。引擎真实路径（``latest_signal`` 取最新行 = 脏行本身）
    下的失效后果是**放行脏行**而非误拦 —— 见下一个用例。两者都是真机制，
    但别把本用例当作「引擎会误拦」的证据。

    ⚠️ ``holidays=frozenset()`` 是刻意的：生产现场的 4 个脏日期
    （2020/2021/2022/2024）都不在 ``holidays_2026`` 覆盖范围内，
    ``is_market_trading_day`` 对它们恒返回 True —— 空节假日表正是该情形的等价替身。
    """
    cal = augment_calendar(
        CAL,
        ref_day=FIRST_AFTER_HOLIDAY,
        signal_days=[DIRTY_SIG_DAY],
        holidays=frozenset(),
    )
    assert DIRTY_SIG_DAY not in cal, "脏信号日污染了生效日历"
    lag = _trading_lag(
        PRE_HOLIDAY, dt.datetime.combine(FIRST_AFTER_HOLIDAY, dt.time(10, 30)), cal
    )
    assert lag == 0, f"健康信号 {PRE_HOLIDAY} 被误拦（lag={lag}），R 已被脏日期推前"


def test_lake_covered_dirty_signal_row_is_blocked_not_promoted(make_engine):
    """⛔ 主湖覆盖区间内的非交易日信号**行**，必须被拦截，不得「自证为新鲜」。

    失效链（旧行为，无 ``cal_max`` 守卫）—— 比「误拦健康信号」更危险：

    1. 脏日期通过 ``is_market_trading_day``（节假日表覆盖不到该年份时恒 True）→ 进日历
    2. 日盘 ``R = bisect_left(cal, T) - 1`` 被推到脏日期上
    3. 而 ``latest_signal`` 取的正是**最新那行** —— 也就是脏行本身
    4. ``lag = idx(脏日期) - idx(脏日期) = 0`` → **放行一个非交易日的信号** ⛔

    守卫后：脏日期不进日历 → 脏行在日历中没有索引 → ``None`` → ``10**9`` 保守拦截 ✅

    ⚠️ 据此，QA 原文描述的「健康信号被误拦」只在**手工指定被判对象**时出现
    （见上一条用例）；引擎真实路径下取最新行，旧行为的后果是**放行脏行**而非误拦。
    两种形态都已钉死。
    """
    eng = make_engine(
        [[
            ("ag0", "2026-02-13 15:00", 0.80, 1.0, True),  # 休市前最后一个交易日（健康）
            ("ag0", "2026-02-17 15:00", 0.70, 0.5, True),  # 休市期间的脏行（更新 → 被取到）
        ]],
        trading_calendar=CAL,
        holidays=frozenset(),  # 同构生产现场：节假日表覆盖不到该日期
    )
    sig = _sig(eng, f"2026-02-24 {DAY}")  # 休市后首个交易日，日盘
    assert sig is not None
    assert _to_date(sig.ts) == DIRTY_SIG_DAY, f"引擎应取最新行（脏行），实际 {sig.ts}"
    assert sig.freshness_days == 10**9, "脏行竟被判为新鲜（日历被污染，自证循环）"
    assert sig.is_effective is False, "非交易日的信号行被放行"


def test_augment_calendar_still_adds_beyond_lake_coverage():
    """守卫不得矫枉过正：主湖覆盖**之外**的交易日照补不误（否则夜盘能力受损）。"""
    cal_no_today = CAL_NO_TODAY  # cal_max = 2026-08-28
    assert cal_no_today[-1] < D0831, "用例前提：当日超出主湖覆盖"
    cal_after = augment_calendar(cal_no_today, ref_day=D0831)
    assert D0831 in cal_after, "主湖覆盖之外的交易日必须补入（否则夜盘恒 None）"
    # 且不影响主湖已知历史：脏日期仍不得进
    assert DIRTY_SIG_DAY not in cal_after


# ---------------------------------------------------------------------------
# ⑮ 真实生产脏信号日（QA 观察项 1 的原始现场）—— 需要真实主湖
# ---------------------------------------------------------------------------
# 真实生产缓存中实测存在的 4 个非交易日信号日（QA 独立复核揪出）：
#   2020-10-02 / 2021-10-01（国庆） · 2022-04-04（清明） · 2024-06-10（端午）
# 以及它们各自「真实上一交易日」（健康信号应取之处）。
_REAL_DIRTY_CASES = [
    (dt.date(2020, 10, 2), dt.date(2020, 9, 30), dt.date(2020, 10, 9)),
    (dt.date(2021, 10, 1), dt.date(2021, 9, 30), dt.date(2021, 10, 8)),
    (dt.date(2022, 4, 4), dt.date(2022, 4, 1), dt.date(2022, 4, 6)),
    (dt.date(2024, 6, 10), dt.date(2024, 6, 7), dt.date(2024, 6, 11)),
]


@pytest.mark.skipif(
    not DEFAULT_LAKE_DIR.exists(), reason="主湖缺失（CI 无数据），跳过生产脏信号日护栏"
)
@pytest.mark.parametrize(
    ("dirty", "prev_trading_day", "next_trading_day"), _REAL_DIRTY_CASES
)
def test_real_production_dirty_signal_days_do_not_pollute_calendar(
    dirty: dt.date, prev_trading_day: dt.date, next_trading_day: dt.date
):
    """钉死 QA 报的生产现场：真实脏信号日不得污染日历，参考点 R 不得被推前。

    ⚠️ 叙事更正（2026-08-31 QA 复核撤回原表述）：QA 原报告称此现场造成
    「健康信号误拦」，后经双方确认，引擎真实路径下（``latest_signal`` 取最新行）
    旧行为的实际后果是**放行脏行**（漏放，更危险）；本用例手工指定健康信号
    作被判对象，验证的是「R 被推移」这一机制 —— 两种形态分别在
    ``test_lake_covered_dirty_signal_row_is_blocked_not_promoted``（引擎路径）
    与本用例（手工指定）各自钉死。

    ⛔ 注意：QA 建议的 ``is_market_trading_day`` 过滤**修不掉这个 bug** ——
    ``holidays_2026`` 只覆盖 2026 年，``is_market_trading_day(2020-10-02)``
    （周五）返回 True。真正的分界是 ``cal_max``。
    """
    cal = load_trading_calendar()
    assert dirty not in cal, f"用例前提失效：{dirty} 已在主湖日历中"
    assert prev_trading_day in cal, f"用例前提失效：{prev_trading_day} 不在主湖日历中"
    assert next_trading_day in cal, f"用例前提失效：{next_trading_day} 不在主湖日历中"

    cal_aug = augment_calendar(
        cal, ref_day=next_trading_day, signal_days=[dirty]
    )
    assert dirty not in cal_aug, f"脏信号日 {dirty} 污染了生效日历"

    # 健康信号（真实上一交易日）在休市后首个交易日的日盘使用 → 必须放行
    lag = _trading_lag(
        prev_trading_day,
        dt.datetime.combine(next_trading_day, dt.time(10, 30)),
        cal_aug,
    )
    assert lag == 0, (
        f"健康信号 {prev_trading_day} 在 {next_trading_day} 日盘被误拦（lag={lag}）"
    )
