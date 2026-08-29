"""故障切换编排器测试（FakePrimary 注入 + patch 备源构造）。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from hexbroker import (
    HexDataError,
    HexEmptyDataError,
    HexNetworkError,
    HexQuotaError,
    HexStaleDataError,
)
from hexbroker.data import failover as fo
from hexbroker.data.failover import FailoverOrchestrator
from hexbroker.data.store import DataLake

SYM = "cu0"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _idx(*dates: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([pd.Timestamp(d) for d in dates])


def _mk_lake_with_history(tmp_path, sym=SYM, closes=(100.0, 101.0, 102.0)):
    """湖内 3 天历史（后复权锚点），默认 close=100/101/102。"""
    lake = DataLake(tmp_path)
    idx = _idx("2026-08-20", "2026-08-21", "2026-08-24")
    df = pd.DataFrame(
        {"close": list(closes), "volume": [10.0, 11.0, 12.0]},
        index=pd.MultiIndex.from_product([[sym], idx],
                                         names=["symbol", "datetime"]),
    )
    lake.save_processed(_SimpleFrame(df, "1d"), symbol=sym)
    return lake


class _SimpleFrame:
    def __init__(self, df, freq):
        self.df = df
        self.freq = freq
        self.symbols = list(df.index.get_level_values("symbol").unique())

    def by_symbol(self, sym):
        return self.df.loc[[sym]]


def _raw_series(values, dates):
    s = pd.Series(values, index=_idx(*dates), dtype=float)
    s.name = "close"
    return s


def _patch_backup(monkeypatch, raw_map):
    """把 BackupRawFetcher 的源构造替换为 FakeSource。

    raw_map: {source_name: {symbol: Series}}；源内抛错用 HexXxxError 实例。
    """
    class FakeSource:
        def __init__(self, name):
            self.name = name

        def fetch_bars(self, symbols, start, end, freq):
            item = raw_map[self.name]
            if isinstance(item, Exception):
                raise item
            frames = []
            for sym in symbols:
                s = item[sym]
                df = s.to_frame("close")
                df["volume"] = 100.0
                frames.append(df.assign(symbol=sym).reset_index()
                              .rename(columns={"index": "datetime"}))
            out = pd.concat(frames)
            out["datetime"] = pd.to_datetime(out["datetime"])
            return _FakeBars(out.set_index(["symbol", "datetime"]))

    class _FakeBars:
        def __init__(self, df):
            self.df = df

    from hexbroker.data import backup as backup_mod

    def _build(name, root, save):
        if name not in raw_map:
            raise HexDataError(f"未知备源名：{name}")
        return FakeSource(name)

    monkeypatch.setattr(backup_mod, "_build_source", _build)
    return backup_mod.BackupRawFetcher(save=False)


START, END = "2026-08-20", "2026-08-25"


# --------------------------------------------------------------------------
# Tier 1 主源
# --------------------------------------------------------------------------
class TestPrimary:
    def test_primary_success_no_provisional(self):
        truth = {SYM: _raw_series([100.0, 101.0, 102.0, 103.0],
                                  "2026-08-20 2026-08-21 2026-08-24 2026-08-25".split())}
        orch = FailoverOrchestrator(primary_fetcher=lambda s, a, b: truth)
        out = orch.fetch_adjusted([SYM], START, END)
        assert out[SYM].ok and out[SYM].tier_hit == "primary"
        assert out[SYM].provisional is False
        assert out[SYM].adj_close.iloc[-1] == 103.0

    def test_quota_locks_primary_for_the_day(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        calls = {"n": 0}

        def primary(symbols, start, end):
            calls["n"] += 1
            raise HexQuotaError("500009 流量超限", source="pandadata")

        bk = _patch_backup(monkeypatch, {
            "sina": {SYM: _raw_series([102.0, 103.0], ["2026-08-24", "2026-08-25"])},
        })
        orch = FailoverOrchestrator(primary_fetcher=primary, backup=bk,
                                    root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        assert out[SYM].tier_hit == "backup" and out[SYM].provisional is True
        assert out[SYM].primary_error_kind == "Quota"
        n_after_first = calls["n"]
        # 第二次调用：主源锁定，不再打主源
        out2 = orch.fetch_adjusted([SYM], START, END)
        assert calls["n"] == n_after_first  # 主源零调用
        assert out2[SYM].primary_error_kind == "QuotaLocked"

    def test_network_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def primary(symbols, start, end):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HexNetworkError("timeout", source="pandadata")
            return {SYM: _raw_series([103.0], ["2026-08-25"])}

        orch = FailoverOrchestrator(primary_fetcher=primary)
        out = orch.fetch_adjusted([SYM], START, END)
        assert out[SYM].tier_hit == "primary" and calls["n"] == 2

    def test_network_twice_then_fallback(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        calls = {"n": 0}

        def primary(symbols, start, end):
            calls["n"] += 1
            raise HexNetworkError("timeout", source="pandadata")

        bk = _patch_backup(monkeypatch, {
            "sina": {SYM: _raw_series([102.0, 103.0],
                                      ["2026-08-24", "2026-08-25"])},
        })
        orch = FailoverOrchestrator(primary_fetcher=primary, backup=bk,
                                    root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        assert calls["n"] == 2  # 恰好重试一次
        assert out[SYM].tier_hit == "backup"

    @pytest.mark.parametrize("exc,kind", [
        (HexEmptyDataError("停更", source="pandadata"), "Empty"),
        (HexStaleDataError("陈旧", source="pandadata", latest="2026-08-15", expected="2026-08-25"), "Stale"),
    ])
    def test_broken_source_no_retry(self, tmp_path, monkeypatch, exc, kind):
        _mk_lake_with_history(tmp_path)
        calls = {"n": 0}

        def primary(symbols, start, end):
            calls["n"] += 1
            raise exc

        bk = _patch_backup(monkeypatch, {
            "sina": {SYM: _raw_series([102.0, 103.0],
                                      ["2026-08-24", "2026-08-25"])},
        })
        orch = FailoverOrchestrator(primary_fetcher=primary, backup=bk,
                                    root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        assert calls["n"] == 1  # 源已坏，零重试
        assert out[SYM].tier_hit == "backup"
        assert out[SYM].primary_error_kind == kind

    def test_primary_not_wired(self):
        orch = FailoverOrchestrator(primary_fetcher=None)
        out = orch.fetch_adjusted([SYM], START, END)
        assert out[SYM].primary_error_kind == "NotWired"


# --------------------------------------------------------------------------
# Tier 2 备源 + 续接
# --------------------------------------------------------------------------
class TestBackup:
    def test_backup_graft_marks_provisional(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        bk = _patch_backup(monkeypatch, {
            "sina": {SYM: _raw_series([102.0, 103.0], ["2026-08-24", "2026-08-25"])},
        })
        orch = FailoverOrchestrator(primary_fetcher=None, backup=bk,
                                    root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        o = out[SYM]
        assert o.ok and o.tier_hit == "backup" and o.provisional is True
        assert o.new_dates == [pd.Timestamp("2026-08-25")]
        # 续接值 = 103 × (102/102) ... 锚定 08-24：k=102/102=1.0 → 103.0
        assert o.adj_close.loc["2026-08-25"] == 103.0
        # P0-9 挂标落盘
        sc = tmp_path / "processed" / SYM / "1d" / "_provisional.json"
        payload = json.loads(sc.read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-25"]

    def test_no_anchor_refuses(self, monkeypatch):
        """红线：湖内无历史 → 拒绝续接，绝不用名义价冒充后复权。"""
        bk = _patch_backup(monkeypatch, {
            "sina": {SYM: _raw_series([103.0], ["2026-08-25"])},
        })
        orch = FailoverOrchestrator(primary_fetcher=None, backup=bk, root=None)
        out = orch.fetch_adjusted([SYM], START, END)
        o = out[SYM]
        assert not o.ok
        kinds = [a.kind for a in o.attempts]
        assert "NoAnchor" in kinds
        assert o.adj_close is None

    def test_backup_exhausted_attributed(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        bk = _patch_backup(monkeypatch, {
            "sina": HexNetworkError("sina down", source="sina"),
            "akshare": HexNetworkError("akshare down", source="akshare"),
        })
        orch = FailoverOrchestrator(primary_fetcher=None, backup=bk,
                                    root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        o = out[SYM]
        assert not o.ok
        kinds = [a.kind for a in o.attempts]
        assert "Exhausted" in kinds
        # Exhausted 归因细粒度在 BackupExhaustedError.summary() 内（逐源）
        exhausted_attempt = next(a for a in o.attempts if a.kind == "Exhausted")
        assert "sina" in exhausted_attempt.error and "akshare" in exhausted_attempt.error


# --------------------------------------------------------------------------
# Tier 3 交易所官方（注入式）
# --------------------------------------------------------------------------
class TestExchangeTier:
    def test_exchange_fallback_when_all_else_fails(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        bk = _patch_backup(monkeypatch, {
            "sina": HexNetworkError("sina down", source="sina"),
            "akshare": HexNetworkError("akshare down", source="akshare"),
        })

        def exchange(symbols, start, end):
            return {SYM: _raw_series([102.0, 103.0],
                                     ["2026-08-24", "2026-08-25"])}

        orch = FailoverOrchestrator(primary_fetcher=None, backup=bk,
                                    exchange_fetcher=exchange, root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        o = out[SYM]
        assert o.ok and o.tier_hit == "exchange" and o.provisional is True

    def test_exchange_unavailable_reported(self, tmp_path, monkeypatch):
        _mk_lake_with_history(tmp_path)
        bk = _patch_backup(monkeypatch, {
            "sina": HexNetworkError("down", source="sina"),
            "akshare": HexNetworkError("down", source="akshare"),
        })
        orch = FailoverOrchestrator(primary_fetcher=None, backup=bk,
                                    exchange_fetcher=None, root=str(tmp_path))
        out = orch.fetch_adjusted([SYM], START, END)
        assert not out[SYM].ok
        assert [a.tier for a in out[SYM].attempts] == ["primary", "backup"]


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------
def test_empty_symbols_raises():
    orch = FailoverOrchestrator()
    with pytest.raises(HexDataError):
        orch.fetch_adjusted([], START, END)


def test_multi_symbol_partial_success(tmp_path, monkeypatch):
    _mk_lake_with_history(tmp_path, "cu0")
    # rb0 用独立价格尺度，证明跨品种锚点互不串扰
    _mk_lake_with_history(tmp_path, "rb0", closes=(3500.0, 3600.0, 3690.0))
    bk = _patch_backup(monkeypatch, {
        "sina": {"cu0": _raw_series([102.0, 103.0],
                                    ["2026-08-24", "2026-08-25"]),
                 "rb0": _raw_series([3690.0, 3700.0],
                                    ["2026-08-24", "2026-08-25"])},
    })
    orch = FailoverOrchestrator(primary_fetcher=None, backup=bk,
                                root=str(tmp_path))
    out = orch.fetch_adjusted(["cu0", "rb0"], START, END)
    assert out["cu0"].ok and out["rb0"].ok
    assert out["rb0"].adj_close.loc["2026-08-25"] == 3700.0
