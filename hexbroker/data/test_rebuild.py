"""P0-9 provisional 标记与真值重建流水线测试。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from hexbroker.data import rebuild
from hexbroker.data.rebuild import (
    RebuildNeeded,
    clear_provisional,
    mark_provisional,
    rebuild_partition,
    rebuild_pipeline,
    scan_rebuild_needed,
)
from hexbroker.data.store import DataLake

SYM = "cu0"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _mk_lake(tmp_path, sym=SYM, year=2026, close=100.0, dates=None):
    """建一个最小年度分区（(symbol,datetime) MultiIndex）。"""
    lake = DataLake(tmp_path)
    idx = pd.DatetimeIndex(dates or ["2026-08-20", "2026-08-21", "2026-08-22"])
    df = pd.DataFrame(
        {"symbol": sym, "close": [close, close, close],
         "volume": [10, 20, 30]},
        index=idx,
    )
    df.index.name = "datetime"
    df = df.set_index(pd.MultiIndex.from_product([[sym], idx], names=["symbol", "datetime"]))
    df = df.drop(columns="symbol")
    lake.save_processed(_SimpleFrame(df, "1d"), symbol=sym)
    return lake


class _SimpleFrame:
    def __init__(self, df, freq):
        self.df = df
        self.freq = freq
        self.symbols = list(df.index.get_level_values("symbol").unique())

    def by_symbol(self, sym):
        return self.df.loc[[sym]]


def _truth(rows, sym=SYM, cols=("close", "volume")):
    """rows: list[(date, close, volume)]；空 rows 返回带正确表头的空表。"""
    df = pd.DataFrame(
        [{"symbol": sym, "datetime": pd.Timestamp(d), "close": c, "volume": v}
         for d, c, v in rows],
        columns=["symbol", "datetime", "close", "volume"],
    )
    return df[["symbol", "datetime", *cols]]


def _read_year(tmp_path, sym=SYM, year=2026):
    p = tmp_path / "processed" / sym / "1d" / f"{year}.parquet"
    df = pd.read_parquet(p)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


# --------------------------------------------------------------------------
# 标记持久化
# --------------------------------------------------------------------------
class TestMarkProvisional:
    def test_mark_creates_sidecar(self, tmp_path):
        p = mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        assert p.exists()
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-23"]
        assert payload["years"]["2026"]["method"] == "graft"

    def test_mark_merges_dates_idempotent(self, tmp_path):
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026,
                         ["2026-08-23", "2026-08-24"])
        payload = json.loads(
            (tmp_path / "processed" / SYM / "1d" / "_provisional.json")
            .read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-23", "2026-08-24"]

    def test_clear_partial_dates_keeps_rest(self, tmp_path):
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026,
                         ["2026-08-23", "2026-08-24"])
        changed = clear_provisional(tmp_path, "processed", SYM, "1d", 2026,
                                    dates=["2026-08-23"])
        assert changed
        payload = json.loads(
            (tmp_path / "processed" / SYM / "1d" / "_provisional.json")
            .read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-24"]

    def test_clear_year_then_all_removes_file(self, tmp_path):
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        assert clear_provisional(tmp_path, "processed", SYM, "1d", 2026)
        assert not (tmp_path / "processed" / SYM / "1d" / "_provisional.json").exists()
        # 再清一次：无文件 -> False
        assert not clear_provisional(tmp_path, "processed", SYM, "1d", 2026)

    def test_mark_corrupt_sidecar_rebuilds(self, tmp_path):
        p = tmp_path / "processed" / SYM / "1d" / "_provisional.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{broken json", encoding="utf-8")
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-23"]


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
class TestScan:
    def test_scan_finds_provisional(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        items = scan_rebuild_needed(tmp_path)
        assert len(items) == 1
        it = items[0]
        assert (it.symbol, it.freq, it.year, it.kind) == (SYM, "1d", 2026, "provisional")
        assert it.dates == ["2026-08-23"]

    def test_scan_finds_missing(self, tmp_path):
        d = tmp_path / "processed" / SYM / "1d"
        d.mkdir(parents=True)
        (d / "_MISSING_2020.json").write_text(json.dumps(
            {"status": "MISSING", "symbol": SYM, "freq": "1d", "year": 2020,
             "quarantined_to": "artifacts/quarantine/x.parquet"}), encoding="utf-8")
        items = scan_rebuild_needed(tmp_path)
        assert len(items) == 1
        it = items[0]
        assert (it.symbol, it.year, it.kind) == (SYM, 2020, "missing")
        assert it.detail["quarantined_to"] == "artifacts/quarantine/x.parquet"

    def test_scan_merges_both_markers(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-23"])
        d = tmp_path / "processed" / SYM / "1d"
        (d / "_MISSING_2020.json").write_text(
            json.dumps({"year": 2020, "symbol": SYM, "freq": "1d"}),
            encoding="utf-8")
        items = scan_rebuild_needed(tmp_path)
        assert len(items) == 2
        kinds = {(i.year, i.kind) for i in items}
        assert kinds == {(2026, "provisional"), (2020, "missing")}

    def test_scan_empty_root(self, tmp_path):
        assert scan_rebuild_needed(tmp_path) == []


# --------------------------------------------------------------------------
# 分区重建 —— provisional
# --------------------------------------------------------------------------
class TestRebuildProvisional:
    def test_replace_and_clear(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        item = RebuildNeeded(SYM, "1d", 2026, "provisional", dates=["2026-08-22"])
        res = rebuild_partition(tmp_path, item,
                                _truth([("2026-08-22", 999.0, 77)]))
        assert res.status == "OK"
        assert res.replaced == ["2026-08-22"]
        assert res.uncovered == [] and res.sidecar_cleared
        row = _read_year(tmp_path).iloc[-1]
        assert row["close"] == 999.0 and row["volume"] == 77
        assert not (tmp_path / "processed" / SYM / "1d" / "_provisional.json").exists()
        # manifest 已重算（经 save_processed，"最近一次写入"语义）
        m = json.loads((tmp_path / "processed" / SYM / "1d" / "manifest.json")
                       .read_text(encoding="utf-8"))
        assert m["source"] == "lake"

    def test_append_new_dates(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-25"])
        item = RebuildNeeded(SYM, "1d", 2026, "provisional", dates=["2026-08-25"])
        res = rebuild_partition(tmp_path, item,
                                _truth([("2026-08-25", 55.0, 5)]))
        assert res.appended == ["2026-08-25"]
        df = _read_year(tmp_path)
        assert len(df) == 4 and df.iloc[-1]["close"] == 55.0

    def test_partial_cover_keeps_mark(self, tmp_path):
        """真值缺一天 -> 那天保持挂标（不静默转正）。"""
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026,
                         ["2026-08-22", "2026-08-25"])
        item = RebuildNeeded(SYM, "1d", 2026, "provisional",
                             dates=["2026-08-22", "2026-08-25"])
        res = rebuild_partition(tmp_path, item, _truth([("2026-08-22", 999.0, 1)]))
        assert res.replaced == ["2026-08-22"]
        assert res.uncovered == ["2026-08-25"]
        assert not res.sidecar_cleared
        payload = json.loads(
            (tmp_path / "processed" / SYM / "1d" / "_provisional.json")
            .read_text(encoding="utf-8"))
        assert payload["years"]["2026"]["dates"] == ["2026-08-25"]

    def test_no_overlap_keeps_all_marks(self, tmp_path):
        """真值一行没对上 -> 全部标记保留（静默转正回归门禁）。"""
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        item = RebuildNeeded(SYM, "1d", 2026, "provisional", dates=["2026-08-22"])
        res = rebuild_partition(tmp_path, item, _truth([]))  # 空真值
        assert res.status == "OK"
        assert res.replaced == [] and res.appended == []
        assert res.uncovered == ["2026-08-22"]
        assert not res.sidecar_cleared
        assert (tmp_path / "processed" / SYM / "1d" / "_provisional.json").exists()
        assert len(_read_year(tmp_path)) == 3  # 分区未被破坏

    def test_schema_mismatch_skips(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        item = RebuildNeeded(SYM, "1d", 2026, "provisional", dates=["2026-08-22"])
        bad = _truth([("2026-08-22", 999.0, 1)])
        bad["oi"] = 5.0  # 多出的列
        res = rebuild_partition(tmp_path, item, bad)
        assert res.status == "SKIPPED"
        assert "列集合" in res.reason
        assert _read_year(tmp_path).iloc[-1]["close"] == 100.0  # 未动

    def test_missing_min_cols_skips(self, tmp_path):
        _mk_lake(tmp_path)
        item = RebuildNeeded(SYM, "1d", 2026, "provisional", dates=["2026-08-22"])
        res = rebuild_partition(tmp_path, item,
                                pd.DataFrame([{"close": 1.0}]))
        assert res.status == "SKIPPED"


# --------------------------------------------------------------------------
# 分区重建 —— missing
# --------------------------------------------------------------------------
class TestRebuildMissing:
    def test_missing_partition_created(self, tmp_path):
        d = tmp_path / "processed" / SYM / "1d"
        d.mkdir(parents=True)
        (d / "_MISSING_2020.json").write_text(
            json.dumps({"year": 2020, "symbol": SYM, "freq": "1d"}),
            encoding="utf-8")
        item = RebuildNeeded(SYM, "1d", 2020, "missing")
        res = rebuild_partition(
            tmp_path, item,
            _truth([("2020-03-02", 3500.0, 100), ("2020-03-03", 3510.0, 110)],
                   cols=("close", "volume")))
        assert res.status == "OK"
        assert res.rows_before == 0 and res.rows_after == 2
        assert not (d / "_MISSING_2020.json").exists()
        df = _read_year(tmp_path, year=2020)
        assert list(df["close"]) == [3500.0, 3510.0]
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        assert m["n_rows"] == 2

    def test_missing_truth_no_year_rows_skipped(self, tmp_path):
        d = tmp_path / "processed" / SYM / "1d"
        d.mkdir(parents=True)
        marker = d / "_MISSING_2020.json"
        marker.write_text(json.dumps({"year": 2020}), encoding="utf-8")
        item = RebuildNeeded(SYM, "1d", 2020, "missing")
        res = rebuild_partition(tmp_path, item, _truth([("2026-08-20", 1.0, 1)]))
        assert res.status == "SKIPPED"
        assert "拒绝凭空建分区" in res.reason
        assert marker.exists()  # 标记保留


# --------------------------------------------------------------------------
# 流水线
# --------------------------------------------------------------------------
class TestPipeline:
    def test_end_to_end(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        d = tmp_path / "processed" / "rb0" / "1d"
        d.mkdir(parents=True)
        (d / "_MISSING_2020.json").write_text(
            json.dumps({"year": 2020, "symbol": "rb0", "freq": "1d"}),
            encoding="utf-8")

        def fetch(item: RebuildNeeded):
            if item.symbol == SYM:
                return _truth([("2026-08-22", 999.0, 1)])
            return _truth([("2020-03-02", 3500.0, 100)], sym="rb0")

        rep = rebuild_pipeline(tmp_path, fetch)
        assert rep.ok and len(rep.results) == 2
        kinds = {r.kind for r in rep.results}
        assert kinds == {"provisional", "missing"}
        assert not (tmp_path / "processed" / SYM / "1d" / "_provisional.json").exists()
        assert not (d / "_MISSING_2020.json").exists()

    def test_fetch_error_isolated(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        d = tmp_path / "processed" / "rb0" / "1d"
        d.mkdir(parents=True)
        (d / "_MISSING_2020.json").write_text(
            json.dumps({"year": 2020, "symbol": "rb0", "freq": "1d"}),
            encoding="utf-8")

        def fetch(item):
            if item.symbol == "rb0":
                raise RuntimeError("pandadata token 失效")
            return _truth([("2026-08-22", 999.0, 1)])

        rep = rebuild_pipeline(tmp_path, fetch)
        # cu0 成功、rb0 进 skipped，互不影响
        assert [r.symbol for r in rep.results] == [SYM]
        assert len(rep.skipped) == 1 and "rb0" in rep.skipped[0][0]
        assert (d / "_MISSING_2020.json").exists()  # rb0 标记保留
        # cu0 已重建
        assert _read_year(tmp_path).iloc[-1]["close"] == 999.0

    def test_empty_truth_skipped(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        rep = rebuild_pipeline(tmp_path, lambda item: pd.DataFrame())
        assert rep.results == []
        assert len(rep.skipped) == 1 and "空真值" in rep.skipped[0][1]

    def test_symbols_filter(self, tmp_path):
        _mk_lake(tmp_path)
        mark_provisional(tmp_path, "processed", SYM, "1d", 2026, ["2026-08-22"])
        rep = rebuild_pipeline(tmp_path, lambda item: _truth([]), symbols=["rb0"])
        assert rep.results == [] and rep.skipped == []  # 过滤后无事可做


def test_rebuild_needed_defaults():
    it = RebuildNeeded("cu0", "1d", 2026, "provisional")
    assert it.dates == [] and it.detail == {}
