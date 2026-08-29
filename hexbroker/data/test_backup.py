"""备源 raw 拉取器单测（P0-4）。

验证策略：用可注入的假源替代真实联网源，覆盖降级路由、陈旧判定、
交叉校验与边界。所有场景都断言**具体行为**，而非"没抛异常"。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import hexbroker.data.backup as backup_mod
from hexbroker import HexDataError, HexEmptyDataError, HexNetworkError, HexStaleDataError
from hexbroker.data.backup import (
    BackupExhaustedError,
    BackupRawFetcher,
    RawPull,
)
from hexbroker.data.schema import BarFrame

COLS = ["open", "high", "low", "close", "volume", "amount", "open_interest"]


def _frame(rows: dict[str, dict[str, float]]) -> pd.DataFrame:
    """rows: {symbol: {date_str: close}} → 合法 BarFrame 的 df。"""
    recs = []
    for sym, series in rows.items():
        for ds, close in series.items():
            recs.append({
                "symbol": sym, "datetime": pd.Timestamp(ds),
                "open": close, "high": close * 1.01, "low": close * 0.99,
                "close": close, "volume": 1000.0, "amount": close * 1000,
                "open_interest": 5000.0,
            })
    df = pd.DataFrame(recs).set_index(["symbol", "datetime"]).sort_index()
    df["adj_close"] = df["close"]
    df["raw_close"] = df["close"]
    return df


class FakeSource:
    """可注入假源。``behavior`` 决定 fetch_bars 的行为。"""

    def __init__(self, name="fake", rows=None, exc=None, stale_from=None):
        self.name = name
        self.rows = rows or {}
        self.exc = exc
        self.stale_from = stale_from
        self.calls: list[tuple] = []

    def fetch_bars(self, symbols, start, end, freq="1d"):
        self.calls.append((list(symbols), start, end, freq))
        if self.exc is not None:
            raise self.exc
        rows = self.rows
        if self.stale_from is not None:
            rows = {s: {d: c for d, c in ser.items() if d <= self.stale_from}
                    for s, ser in self.rows.items()}
        df = _frame(rows)
        if df.empty:
            raise HexEmptyDataError(f"{self.name} 无数据", source=self.name)
        return BarFrame(df=df, freq=freq, source=self.name)


@pytest.fixture
def patch_build(monkeypatch):
    """把 _build_source 换成从 registry 取假源，并记录实例化次数。"""
    registry: dict[str, FakeSource] = {}
    created: list[str] = []

    def _build(name, root=None, save=False):
        created.append(name)
        if name not in registry:
            raise HexDataError(f"未知备源名：{name}")
        return registry[name]

    monkeypatch.setattr(backup_mod, "_build_source", _build)
    return registry, created


# ---------------------------------------------------------------------------
# 构造与参数校验
# ---------------------------------------------------------------------------
class TestInit:
    def test_empty_sources_raises(self):
        with pytest.raises(HexDataError, match="备源列表为空"):
            BackupRawFetcher(sources=())

    def test_unknown_source_name_raises_at_construction(self):
        """配置错误应在构造时炸，而不是伪装成"全部备源失败"。"""
        with pytest.raises(HexDataError, match="未知备源名"):
            BackupRawFetcher(sources=("nope",))

    def test_empty_symbols_raises(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0}})
        with pytest.raises(HexDataError, match="symbols 为空"):
            BackupRawFetcher(sources=("sina",)).fetch_raw([], "2026-08-01", "2026-08-28")


# ---------------------------------------------------------------------------
# 降级路由
# ---------------------------------------------------------------------------
class TestFallbackRouting:
    def test_first_source_wins(self, patch_build):
        registry, created = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0}})
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3001.0}})
        f = BackupRawFetcher(sources=("sina", "akshare"))
        got = f.fetch_raw(["rb0"], "2026-08-01", "2026-08-28")
        assert got["rb0"].source == "sina"
        assert float(got["rb0"].close.iloc[-1]) == pytest.approx(3000.0)

    def test_falls_back_on_empty(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", exc=HexEmptyDataError("sina 空", source="sina"))
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3111.0}})
        got = BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert got["rb0"].source == "akshare"

    def test_falls_back_on_network(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", exc=HexNetworkError("超时", source="sina"))
        registry["akshare"] = FakeSource("akshare", {"cu0": {"2026-08-27": 108300.0}})
        got = BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
            ["cu0"], "2026-08-01", "2026-08-28")
        assert got["cu0"].source == "akshare"

    def test_all_fail_raises_with_attempts(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", exc=HexEmptyDataError("空", source="sina"))
        registry["akshare"] = FakeSource("akshare", exc=HexNetworkError("超时", source="akshare"))
        with pytest.raises(BackupExhaustedError) as ei:
            BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
                ["rb0"], "2026-08-01", "2026-08-28")
        # 逐源失败原因必须留痕 —— 否则就是"静默降级"
        assert len(ei.value.attempts) == 2
        assert "sina" in ei.value.summary()
        assert "HexNetworkError" in ei.value.summary()

    def test_unexpected_exception_also_falls_back(self, patch_build):
        """第三方库异常类型不可控，必须也能降级而不是炸穿。"""
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", exc=RuntimeError("第三方炸了"))
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3000.0}})
        got = BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert got["rb0"].source == "akshare"


# ---------------------------------------------------------------------------
# 新鲜度门禁
# ---------------------------------------------------------------------------
class TestFreshness:
    def test_stale_source_is_treated_as_failure(self, patch_build):
        """陈旧 = 失败，不得静默当作刷新成功（2026-08-28 事故的直接教训）。"""
        registry, _ = patch_build
        registry["sina"] = FakeSource(
            "sina", {"rb0": {"2026-06-29": 3000.0, "2026-08-27": 3100.0}},
            stale_from="2026-06-29")
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3100.0}})
        got = BackupRawFetcher(sources=("sina", "akshare"), max_stale_days=5).fetch_raw(
            ["rb0"], "2026-08-20", "2026-08-28")
        assert got["rb0"].source == "akshare"

    def test_all_stale_raises(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource(
            "sina", {"rb0": {"2026-06-29": 3000.0, "2026-08-27": 3100.0}},
            stale_from="2026-06-29")
        with pytest.raises(BackupExhaustedError) as ei:
            BackupRawFetcher(sources=("sina",), max_stale_days=5).fetch_raw(
                ["rb0"], "2026-08-20", "2026-08-28")
        assert any("陈旧" in err for _src, err in ei.value.attempts)

    def test_historical_backfill_exempt(self, patch_build):
        """历史回填（end 远早于今天）不应触发陈旧判定。"""
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2024-03-01": 3000.0}})
        got = BackupRawFetcher(sources=("sina",), max_stale_days=5).fetch_raw(
            ["rb0"], "2024-01-01", "2024-03-05")
        assert got["rb0"].source == "sina"
        assert float(got["rb0"].close.iloc[-1]) == pytest.approx(3000.0)


# ---------------------------------------------------------------------------
# 交叉校验
# ---------------------------------------------------------------------------
class TestCrossCheck:
    def test_divergence_warns(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-26": 3000.0,
                                                       "2026-08-27": 3100.0}})
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-26": 3000.0,
                                                             "2026-08-27": 3300.0}})
        got = BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert any("不一致" in w for w in got["rb0"].warnings)

    def test_identical_no_warning(self, patch_build):
        registry, _ = patch_build
        rows = {"rb0": {"2026-08-26": 3000.0, "2026-08-27": 3100.0}}
        registry["sina"] = FakeSource("sina", rows)
        registry["akshare"] = FakeSource("akshare", rows)
        got = BackupRawFetcher(sources=("sina", "akshare")).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert got["rb0"].warnings == []

    def test_disabled_skips_second_source(self, patch_build):
        """cross_check=False 时不应再拉第二个源（避免无谓耗时）。"""
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0}})
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3000.0}})
        BackupRawFetcher(sources=("sina", "akshare"), cross_check=False).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert len(registry["akshare"].calls) == 0

    def test_enabled_pulls_second_source(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0}})
        registry["akshare"] = FakeSource("akshare", {"rb0": {"2026-08-27": 3000.0}})
        BackupRawFetcher(sources=("sina", "akshare"), cross_check=True).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert len(registry["akshare"].calls) == 1


# ---------------------------------------------------------------------------
# 数据结构与便捷方法
# ---------------------------------------------------------------------------
class TestRawPullShape:
    def test_fields_populated(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"cu0": {"2026-08-27": 108300.0}})
        got = BackupRawFetcher(sources=("sina",)).fetch_raw(
            ["cu0"], "2026-08-01", "2026-08-28")
        p = got["cu0"]
        assert isinstance(p, RawPull)
        assert p.symbol == "cu0"
        assert p.source == "sina"
        assert p.open_interest is not None
        assert float(p.open_interest.iloc[-1]) == pytest.approx(5000.0)
        assert p.latest == date(2026, 8, 27)

    def test_fetch_close_series(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0},
                                               "cu0": {"2026-08-27": 108300.0}})
        ser = BackupRawFetcher(sources=("sina",)).fetch_close_series(
            ["rb0", "cu0"], "2026-08-01", "2026-08-28")
        assert set(ser) == {"rb0", "cu0"}
        assert all(isinstance(v, pd.Series) for v in ser.values())

    def test_close_is_nominal_not_adjusted(self, patch_build):
        """红线：备源只供名义价，绝不自造后复权。"""
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {"rb0": {"2026-08-27": 3000.0}})
        got = BackupRawFetcher(sources=("sina",)).fetch_raw(
            ["rb0"], "2026-08-01", "2026-08-28")
        assert float(got["rb0"].close.iloc[-1]) == pytest.approx(3000.0)

    def test_multi_symbol(self, patch_build):
        registry, _ = patch_build
        registry["sina"] = FakeSource("sina", {
            "rb0": {"2026-08-27": 3000.0},
            "cu0": {"2026-08-27": 108300.0},
        })
        got = BackupRawFetcher(sources=("sina",)).fetch_raw(
            ["rb0", "cu0"], "2026-08-01", "2026-08-28")
        assert set(got) == {"rb0", "cu0"}
