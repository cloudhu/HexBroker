"""P0-3 信号缓存新鲜度防护单测（阈值=0，隔夜过期）。

覆盖：
① threshold=0 且 fd=1（周五信号周一用）→ is_effective=False（禁开，技术兜底接手）；
② threshold=0 且 fd=0（同一交易日）→ is_effective=True；
③ 跨交易日主源过期 + 兜底源无更新 → 返回主源且 is_effective=False；
④ 主源过期 + 兜底源当日新鲜 → 回退兜底源（级联语义不变，effective=True）；
⑤ ``freshness_threshold`` property 返回构造值 / 默认值（默认 0）；
⑥ configs/paper.yaml 与 SignalEngine 默认值双向对齐（回归护栏：阈值必须为 0）。

注：本测试不写真实 parquet（沙箱可能缺 pyarrow），改为注入 ``_load_cache``
返回内存 DataFrame，与生产缓存列/类型一致（symbol/ts/p_up/exp_ret/is_effective）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.paper.signals import SignalEngine, _business_days, _to_date

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
# ① threshold=0 + fd=1（周五信号周一用）→ 过期
# ---------------------------------------------------------------------------
def test_threshold_zero_overnight_signal_is_stale(make_engine):
    """08-21(周五) 信号在 08-24(周一) 使用 → fd=1 > 0 → is_effective=False。"""
    eng = make_engine([[("ag0", "2026-08-21 15:00", 0.80, 1.0, True)]], freshness_threshold_days=0)
    sig = eng.latest_signal("ag0", asof="2026-08-24 21:30")
    assert sig is not None
    assert sig.freshness_days == 1
    assert sig.is_effective is False  # 隔夜过期 → 无持仓禁开（§8.2）
    assert sig.source == "engine_a"


# ---------------------------------------------------------------------------
# ② threshold=0 + fd=0（同一交易日）→ 新鲜
# ---------------------------------------------------------------------------
def test_threshold_zero_same_day_signal_is_fresh(make_engine):
    """同一交易日信号 fd=0 → is_effective 保持缓存原值 True。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.80, 1.0, True)]], freshness_threshold_days=0)
    sig = eng.latest_signal("ag0", asof="2026-08-24 10:00")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is True


def test_threshold_zero_respects_cache_is_effective_false(make_engine):
    """缓存本身 is_effective=False 时，即使 fd=0 也不放行（与原语义一致）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.80, 1.0, False)]], freshness_threshold_days=0)
    sig = eng.latest_signal("ag0", asof="2026-08-24 10:00")
    assert sig is not None
    assert sig.freshness_days == 0
    assert sig.is_effective is False


# ---------------------------------------------------------------------------
# ③ 跨交易日主源过期 + 兜底无更新 → 主源 + is_effective=False
# ---------------------------------------------------------------------------
def test_cross_day_primary_stale_backup_not_newer(make_engine):
    """主源 08-21 / 兜底 06-29，asof 08-25 → 返回主源，effective=False。"""
    eng = make_engine(
        [
            [("ag0", "2026-08-21 15:00", 0.80, 1.0, True)],
            [("ag0", "2026-06-29 15:00", 0.55, 0.3, True)],
        ],
        freshness_threshold_days=0,
    )
    sig = eng.latest_signal("ag0", asof="2026-08-25 09:30")
    assert sig is not None
    assert sig.source == "engine_a"
    assert sig.freshness_days == 2  # 08-21 → 08-25：周五、周一 2 个工作日
    assert sig.is_effective is False


# ---------------------------------------------------------------------------
# ④ 主源过期 + 兜底当日新鲜 → 级联回退（语义不变）
# ---------------------------------------------------------------------------
def test_cascade_still_prefers_fresher_backup_under_zero_threshold(make_engine):
    """阈值 0 下级联仍生效：主源隔夜过期而兜底为当日信号 → 用兜底。"""
    eng = make_engine(
        [
            [("ag0", "2026-08-21 15:00", 0.80, 1.0, True)],
            [("ag0", "2026-08-24 09:00", 0.62, 0.5, True)],
        ],
        freshness_threshold_days=0,
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
def test_freshness_threshold_property_default_is_zero(make_engine):
    """未显式传参 → 默认阈值 0（隔夜过期）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.6, 0.2, True)]])
    assert eng.freshness_threshold == 0


def test_freshness_threshold_property_returns_configured_value(make_engine):
    """显式传入阈值 → property 返回该值（int 化）。"""
    eng = make_engine([[("ag0", "2026-08-24 09:00", 0.6, 0.2, True)]], freshness_threshold_days=3)
    assert eng.freshness_threshold == 3
    sig = eng.latest_signal("ag0", asof="2026-08-26 10:00")
    assert sig is not None and sig.is_effective is True  # fd=2 <= 3


# ---------------------------------------------------------------------------
# ⑥ 配置回归护栏：paper.yaml 阈值必须为 0
# ---------------------------------------------------------------------------
def test_paper_yaml_threshold_is_zero():
    """configs/paper.yaml 与代码默认值一致，均为 0（P0-3 隔夜过期）。"""
    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    assert int(cfg.paper.freshness_threshold_days) == 0


def test_business_days_helper_semantics():
    """_business_days 工作日差口径：周五→周一 =1，同日 =0，跨周 =5。"""
    assert _business_days(_to_date("2026-08-21"), _to_date("2026-08-24")) == 1
    assert _business_days(_to_date("2026-08-24"), _to_date("2026-08-24")) == 0
    assert _business_days(_to_date("2026-08-17"), _to_date("2026-08-24")) == 5
