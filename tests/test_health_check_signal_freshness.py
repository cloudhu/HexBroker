"""P0-3 启动自检：信号缓存新鲜度 WARN（不阻断启动）。

覆盖：
① 陈旧缓存（latest 早于基准日若干工作日）→ CheckItem WARN + 刷新命令提示；
② 新鲜缓存（同一交易日）→ CheckItem OK；
③ 阈值取配置值（缺失回退 0）；
④ 缓存文件缺失 → FAIL（原语义不变）；
⑤ ``signal_freshness_days`` 与 ``SignalEngine`` 工作日口径一致；
⑥ 生命周期参数展示阈值 0 的语义说明。

注：不写真实 parquet（沙箱可能缺 pyarrow），改为注入 ``pandas.read_parquet``。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from omegaconf import OmegaConf

from hexbroker.diagnostics.health_check import (
    check_data_sources,
    check_lifecycle,
    signal_freshness_days,
)
from hexbroker.paper.signals import _business_days, _to_date

ASOF = date(2026, 8, 24)  # 周一


def _cfg(cache_paths: list[str], threshold: int | None = 0) -> OmegaConf:
    body: dict = {"signal_caches": cache_paths, "quote_url": "https://hq.sinajs.cn/list="}
    if threshold is not None:
        body["freshness_threshold_days"] = threshold
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
    """08-21 缓存在 08-24 自检 → fd=1 > 阈值 0 → WARN + 刷新命令。"""
    path = fake_cache("signals_stale.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF))
    assert len(items) == 1
    assert items[0].status == "WARN"
    assert "信号陈旧 fd=1>阈值0" in items[0].detail
    assert "p22_tail_ext.py --skip-eval" in items[0].detail
    assert "2026-08-21 15:00" in items[0].detail


def test_very_stale_cache_reports_business_day_gap(fake_cache):
    """06-29 缓存在 08-24 自检 → fd 为工作日差（>>0）→ WARN。"""
    path = fake_cache("signals_v8.parquet", "2026-06-29 15:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF))
    expected = _business_days(_to_date("2026-06-29"), ASOF)
    assert items[0].status == "WARN"
    assert f"fd={expected}>阈值0" in items[0].detail


# ---------------------------------------------------------------------------
# ② 新鲜缓存 → OK
# ---------------------------------------------------------------------------
def test_fresh_cache_reports_ok(fake_cache):
    """同一交易日缓存 → fd=0 <= 阈值 0 → OK。"""
    path = fake_cache("signals_fresh.parquet", "2026-08-24 09:00")
    items = _cache_items(check_data_sources(_cfg([path]), offline=True, asof=ASOF))
    assert items[0].status == "OK"
    assert "fd=0<=阈值0" in items[0].detail
    assert "2 行" in items[0].detail


# ---------------------------------------------------------------------------
# ③ 阈值取配置（宽松阈值 → 隔夜仍 OK；缺失 → 回退 0）
# ---------------------------------------------------------------------------
def test_threshold_from_config_allows_stale_when_relaxed(fake_cache):
    """配置阈值 5 时 fd=1 → 仍判 OK（阈值来源于配置，非硬编码）。"""
    path = fake_cache("signals_relaxed.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path], threshold=5), offline=True, asof=ASOF))
    assert items[0].status == "OK"
    assert "fd=1<=阈值5" in items[0].detail


def test_threshold_defaults_to_zero_when_missing(fake_cache):
    """配置缺失 freshness_threshold_days → 回退 0 → 隔夜信号 WARN。"""
    path = fake_cache("signals_nothr.parquet", "2026-08-21 15:00")
    items = _cache_items(check_data_sources(_cfg([path], threshold=None), offline=True, asof=ASOF))
    assert items[0].status == "WARN"
    assert "阈值0" in items[0].detail


# ---------------------------------------------------------------------------
# ④ 缺失文件 → FAIL（原语义不变）
# ---------------------------------------------------------------------------
def test_missing_cache_still_fails(tmp_path):
    missing = str(tmp_path / "not_exist.parquet")
    items = _cache_items(check_data_sources(_cfg([missing]), offline=True, asof=ASOF))
    assert items[0].status == "FAIL"
    assert "文件缺失" in items[0].detail


# ---------------------------------------------------------------------------
# ⑤ 口径一致性 + ⑥ 生命周期展示
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sig_day,expected",
    [("2026-08-24", 0), ("2026-08-21", 1), ("2026-08-20", 2), ("2026-08-17", 5)],
)
def test_freshness_days_matches_signal_engine_semantics(sig_day: str, expected: int):
    assert signal_freshness_days(pd.Timestamp(f"{sig_day} 15:00"), ASOF) == expected
    assert signal_freshness_days(pd.Timestamp(f"{sig_day} 15:00"), ASOF) == _business_days(
        _to_date(sig_day), ASOF
    )


def test_freshness_days_none_when_no_timestamp():
    assert signal_freshness_days(None, ASOF) is None


def test_lifecycle_shows_zero_threshold_semantics():
    cfg = OmegaConf.create(
        {
            "freshness_threshold_days": 0,
            "symbols": {"ag0": {"mode": "trade"}},
            "holidays_2026": [],
        }
    )
    items = [it for it in check_lifecycle(cfg) if it.name == "信号新鲜度阈值"]
    assert len(items) == 1
    assert "0 交易日" in items[0].detail
    assert "隔夜过期" in items[0].detail
