"""P2-4 口径契约回归测试：取证统一取数入口必须与生产同源。

背景（2026-09-01 血泪，见 deliverables/复盘_2026-09-01_上午.md）：
取证脚本直读主湖 parquet（复权价）而生产走 sina（名义价、不含当日），
导致 P0-2「S1 单位错配」与 P0-3「ATR 口径混用」两条 P0 **被错误定性**。

本测试把「同源」这件事固化为契约：
- ``load_bars`` 必须走 ``RealTimeQuoteClient``（不是 parquet）；
- 派生量必须与**生产实现**行为一致（禁止本地副本）；
- ``bars_health`` 必须能识别复权价（k≠1），即主湖口径一旦混入即报警。

全部用例离线可跑（不依赖网络）。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "forensics_prod_data", SCRIPTS / "forensics_prod_data.py"
)
assert _spec is not None and _spec.loader is not None
fpd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpd)

from hexbroker.paper.scheduler import TradingScheduler  # noqa: E402


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
def _make_bars(n: int = 40, k: float = 1.0, seed: int = 7) -> pd.DataFrame:
    """构造日线：k=1.0 → 名义价（sina 口径）；k≠1 → 复权价（主湖口径）。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(datetime.now().date() - timedelta(days=n), periods=n, freq="D")
    close = 3000.0 + np.cumsum(rng.normal(0, 10, n))
    high = close + np.abs(rng.normal(5, 2, n))
    low = close - np.abs(rng.normal(5, 2, n))
    df = pd.DataFrame(
        {
            "open": close - 1.0,
            "high": high,
            "low": low,
            "close": close * k,
            "volume": rng.integers(1e5, 5e5, n).astype(float),
            "raw_close": close,
        },
        index=idx,
    )
    df.index.name = "datetime"
    return df


class _FakeClient:
    """替换 RealTimeQuoteClient，记录调用参数并返回哨兵 DataFrame。"""

    calls: list[tuple] = []

    def __init__(self, *a, **kw):
        pass

    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120):
        _FakeClient.calls.append((symbol, freq, days))
        return pd.DataFrame({"close": [1.0, 2.0, 3.0]})


# ----------------------------------------------------------------------
# ① 取数路径契约
# ----------------------------------------------------------------------
def test_load_bars_goes_through_production_client(monkeypatch):
    """load_bars 必须走 RealTimeQuoteClient（生产同源），不得直读 parquet。"""
    _FakeClient.calls = []
    monkeypatch.setattr(fpd, "RealTimeQuoteClient", _FakeClient)
    out = fpd.load_bars("rb0", "1d", 120)
    assert _FakeClient.calls == [("rb0", "1d", 120)]
    assert list(out["close"]) == [1.0, 2.0, 3.0]


def test_load_bars_never_reads_main_lake(monkeypatch):
    """反向契约：把 pd.read_parquet 打断，取证取数仍应成功（证明不读主湖）。"""

    def _boom(*a, **kw):  # pragma: no cover - 触发即失败
        raise AssertionError("取证入口禁止读取主湖 parquet（P2-4）")

    monkeypatch.setattr(pd, "read_parquet", _boom)
    monkeypatch.setattr(fpd, "RealTimeQuoteClient", _FakeClient)
    out = fpd.load_bars("ag0")
    assert not out.empty


# ----------------------------------------------------------------------
# ② 派生量必须与生产实现一致（禁止本地副本）
# ----------------------------------------------------------------------
def test_aux_from_bars_matches_production_impl():
    bars = _make_bars()
    got = fpd.aux_from_bars(bars)
    want = TradingScheduler._aux_from_bars(None, bars)
    assert len(got) == len(want) == 3
    np.testing.assert_allclose(got[0], want[0])
    np.testing.assert_allclose(got[1], want[1])
    if got[2] is None or want[2] is None:
        assert got[2] == want[2]
    else:
        assert got[2] == pytest.approx(want[2])


def test_atr_from_bars_matches_production_impl():
    bars = _make_bars()
    want = TradingScheduler._atr_from_bars(
        type("_S", (), {"_default_atr_pct": 0.02})(), "rb0", bars,
        fpd.Quote(symbol="rb0", ts=datetime.now(), price=3000.0),
    )
    got = fpd.atr_from_bars("rb0", bars, price=3000.0)
    assert got == pytest.approx(float(want))


def test_atr_from_bars_fallback_without_crash():
    """空 bars 走生产兜底分支（self._default_atr_pct 打桩），不得因 self=None 崩。"""
    got = fpd.atr_from_bars("rb0", pd.DataFrame(), price=3000.0, default_atr_pct=0.02)
    assert got == pytest.approx(60.0)


# ----------------------------------------------------------------------
# ③ 口径自检：能识别「复权价混入」
# ----------------------------------------------------------------------
def test_bars_health_flags_nominal_bars_as_k_equals_one():
    h = fpd.bars_health(_make_bars(k=1.0))
    assert h["k_constant_one"] is True
    assert h["rows"] == 40


def test_bars_health_detects_adjusted_main_lake_bars():
    """主湖口径（k≈1.18）必须被判为**非**名义价 —— 这是 P0-2 误判的探测器。"""
    h = fpd.bars_health(_make_bars(k=1.18))
    assert h["k_constant_one"] is False
    assert h["k_min"] == pytest.approx(1.18, abs=1e-6)


def test_bars_health_empty_bars_is_safe():
    h = fpd.bars_health(pd.DataFrame())
    assert h["rows"] == 0
    assert h["last_date"] is None
    assert h["includes_today"] is False
