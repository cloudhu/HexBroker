"""P0-3 信号缓存新鲜度防护单测（阈值=1，自然日差口径）。

覆盖：
① threshold=1 且 fd=3（周五信号周一用，P0-3 事故场景）→ is_effective=False（禁开）；
② threshold=1 且 fd=0（同一交易日）→ is_effective=True；
③ 跨周末主源过期 + 兜底源无更新 → 返回主源且 is_effective=False；
④ 主源过期 + 兜底源当日新鲜 → 回退兜底源（级联语义不变，effective=True）；
⑤ ``freshness_threshold`` property 返回构造值 / 默认值（默认 1）；
⑥ configs/paper.yaml 与 SignalEngine 默认值双向对齐（回归护栏：阈值必须为 1）；
⑦ **口径护栏**：自然日差 vs 工作日差（周五→周一 =3 而非 1）—— P3-B 修复的核心。

口径变更背景（P3-B，2026-08-31）：
    信号为**回溯性**（对已有 bar 打分，不为未来外推），盘中最新信号日恒为上一交易日，
    ``fd=0`` 结构性不可达 → 旧阈值 0 等价于「日盘永不主源开仓」，属 P0-3 过度矫正。
    度量由 ``np.busday_count``（工作日差）改为**自然日差**后，阈值取 1：
    正常隔夜=1 放行，跨周末=3 / 跨假期≥2 拦截，P0-3 真实意图完整保留。
    详见 ``artifacts/_p3b_freshness_rootcause_20260831.md``。

注：本测试不写真实 parquet（沙箱可能缺 pyarrow），改为注入 ``_load_cache``
返回内存 DataFrame，与生产缓存列/类型一致（symbol/ts/p_up/exp_ret/is_effective）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.paper.signals import SignalEngine, _calendar_days, _to_date

ROOT = Path(__file__).resolve().parents[1]


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
    """构造 SignalEngine（内存缓存注入，避免 parquet/pyarrow 依赖）。"""

    def _make(sources: list[list[tuple]], **kwargs) -> SignalEngine:
        frames = [_frame(rows) for rows in sources]
        state = {"i": 0}

        def _fake_load(self, cache_path: Path) -> pd.DataFrame:  # noqa: ANN001
            df = frames[state["i"]]
            state["i"] += 1
            return df

        monkeypatch.setattr(SignalEngine, "_load_cache", _fake_load)
        paths = [tmp_path / f"src{i}.parquet" for i in range(len(sources))]
        return SignalEngine(cache_paths=paths, **kwargs)

    return _make


# ---------------------------------------------------------------------------
# ①【P0-3 事故回归护栏】threshold=1 + 周五信号周一用 → 仍必须过期
#    守护：2026-08-24 夜盘用 08-21(周五) 信号开仓事故不得因口径变更而复现。
#    旧口径工作日差 fd=1（阈值 0 拦截）；新口径自然日差 fd=3（阈值 1 仍拦截）。
# ---------------------------------------------------------------------------
def test_cross_weekend_signal_stale_under_threshold_one(make_engine):
    """08-21(周五) 信号在 08-24(周一) 使用 → fd=3 > 1 → is_effective=False。"""
    eng = make_engine([[("ag0", "2026-08-21 15:00", 0.80, 1.0, True)]], freshness_threshold_days=1)
    sig = eng.latest_signal("ag0", asof="2026-08-24 21:30")
    assert sig is not None
    assert sig.freshness_days == 3  # 自然日差：周五→周一 = 3（工作日差口径下为 1）
    assert sig.is_effective is False  # 跨周末过期 → 无持仓禁开（§8.2 / P0-3 事故场景）
    assert sig.source == "engine_a"


def test_normal_overnight_signal_is_fresh_under_threshold_one(make_engine):
    """08-24(周一) 信号在 08-25(周二) 使用 → fd=1 <= 1 → 放行（P3-B 修复目标）。"""
    eng = make_engine([[("ag0", "2026-08-24 15:00", 0.80, 1.0, True)]], freshness_threshold_days=1)
    sig = eng.latest_signal("ag0", asof="2026-08-25 09:30")
    assert sig is not None
    assert sig.freshness_days == 1
    assert sig.is_effective is True  # 正常隔夜：信息仅衰减 1 自然日 → 允许主源驱动开仓


# ---------------------------------------------------------------------------
# ② fd=0（同一交易日）→ 新鲜（与阈值无关，两种口径下同日均为 0）
# ---------------------------------------------------------------------------
def test_same_day_signal_is_fresh(make_engine):
    """同一交易日信号 fd=0 → is_effective 保持缓存原值 True。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.80, 1.0, True)]], freshness_threshold_days=1)
    sig = eng.latest_signal("ag0", asof="2026-08-24 10:00")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


def test_same_day_respects_cache_is_effective_false(make_engine):
    """缓存本身 is_effective=False 时，即使 fd=0 也不放行（与原语义一致）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.80, 1.0, False)]], freshness_threshold_days=1)
    sig = eng.latest_signal("ag0", asof="2026-08-24 10:00")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is False


# ---------------------------------------------------------------------------
# ③ 跨交易日主源过期 + 兜底无更新 → 主源 + is_effective=False
# ---------------------------------------------------------------------------
def test_cross_day_primary_stale_backup_not_newer(make_engine):
    """主源 08-21(周五) / 兜底 06-29，asof 08-25(周二) → 返回主源，effective=False。"""
    eng = make_engine(
        [
            [("ag0", "2026-08-21 15:00", 0.80, 1.0, True)],
            [("ag0", "2026-06-29 15:00", 0.55, 0.3, True)],
        ],
        freshness_threshold_days=1,
    )
    sig = eng.latest_signal("ag0", asof="2026-08-25 09:30")
    assert sig is not None
    assert sig.source == "engine_a"
    assert sig.freshness_days == 4  # 08-21 → 08-25：自然日差 4（工作日差口径下为 2）
    assert sig.is_effective is False


# ---------------------------------------------------------------------------
# ④ 主源过期 + 兜底当日新鲜 → 级联回退（语义不变）
# ---------------------------------------------------------------------------
def test_cascade_still_prefers_fresher_backup(make_engine):
    """级联仍生效：主源跨周末过期（fd=3）而兜底为当日信号（fd=0）→ 用兜底。"""
    eng = make_engine(
        [
            [("ag0", "2026-08-21 15:00", 0.80, 1.0, True)],
            [("ag0", "2026-08-24 09:00", 0.62, 0.5, True)],
        ],
        freshness_threshold_days=1,
    )
    sig = eng.latest_signal("ag0", asof="2026-08-24 10:00")
    assert sig is not None
    assert sig.source == "engine_a_fb1"
    assert sig.freshness_days == 0
    assert sig.is_effective is True
    assert sig.p_up == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# ⑤ freshness_threshold property
# ---------------------------------------------------------------------------
def test_freshness_threshold_property_default_is_one(make_engine):
    """未显式传参 → 默认阈值 1（自然日差口径：允许相邻交易日）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.6, 0.2, True)]])
    assert eng.freshness_threshold == 1


def test_freshness_threshold_property_returns_configured_value(make_engine):
    """显式传入阈值 → property 返回该值（int 化）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.6, 0.2, True)]], freshness_threshold_days=3)
    assert eng.freshness_threshold == 3
    sig = eng.latest_signal("ag0", asof="2026-08-26 10:00")
    assert sig is not None and sig.is_effective is True  # fd=2 <= 3


# ---------------------------------------------------------------------------
# ⑥ 配置回归护栏：paper.yaml 阈值必须为 1（与 SignalEngine 默认值双向对齐）
# ---------------------------------------------------------------------------
def test_paper_yaml_threshold_is_one():
    """configs/paper.yaml 与代码默认值一致，均为 1（自然日差口径，P0-3 + P3-B）。"""
    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    assert int(cfg.paper.freshness_threshold_days) == 1


# ---------------------------------------------------------------------------
# ⑦ 口径护栏：自然日差 vs 工作日差（P3-B 核心，防止退回 np.busday_count）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("2026-08-24", "2026-08-24", 0),   # 同日
        ("2026-08-24", "2026-08-25", 1),   # 周一→周二：正常隔夜
        ("2026-08-25", "2026-08-26", 1),   # 周二→周三
        ("2026-08-21", "2026-08-24", 3),   # 周五→周一：跨周末（工作口径误判为 1）
        ("2026-08-28", "2026-08-31", 3),   # 周五→周一：2026-08-31 当日实测场景
        ("2026-08-17", "2026-08-24", 7),   # 跨整周（工作口径为 5）
        ("2026-02-13", "2026-02-23", 10),  # 春节跨越（工作口径为 5，严重低估衰减）
    ],
)
def test_calendar_days_helper_semantics(a: str, b: str, expected: int):
    """_calendar_days 自然日差口径：跨周末/假期按真实日历跨度计，不被交易日数掩盖。"""
    assert _calendar_days(_to_date(a), _to_date(b)) == expected


def test_calendar_days_is_not_business_days():
    """反向护栏：跨周末必须 ≠1（若退回 np.busday_count 工作日差，此断言立即失败）。"""
    fri, mon = _to_date("2026-08-21"), _to_date("2026-08-24")
    tue, wed = _to_date("2026-08-25"), _to_date("2026-08-26")
    # 旧口径下两者都是 1，无法区分；新口径必须区分开
    assert _calendar_days(fri, mon) != _calendar_days(tue, wed)
    assert _calendar_days(fri, mon) == 3
    assert _calendar_days(tue, wed) == 1


def test_calendar_days_handles_none_and_reversed():
    """None → 超大值（无信号）；日期颠倒 → 取绝对值。"""
    assert _calendar_days(None, _to_date("2026-08-24")) == 10 ** 9
    assert _calendar_days(_to_date("2026-08-24"), None) == 10 ** 9
    assert _calendar_days(_to_date("2026-08-24"), _to_date("2026-08-21")) == 3


def test_business_days_alias_points_to_calendar_days():
    """向后兼容别名 ``_business_days`` 必须指向新口径，避免两处语义漂移。"""
    from hexbroker.paper.signals import _business_days

    assert _business_days is _calendar_days
