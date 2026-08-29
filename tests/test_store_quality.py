"""P0-12 缺失年度显式化测试（DataLake.load_processed / quality_notes）。"""
from __future__ import annotations

import json
import logging

import pandas as pd
import pytest

from hexbroker.data.rebuild import MISSING_GLOB as REBUILD_MISSING_GLOB
from hexbroker.data.store import MISSING_GLOB, DataLake
from hexbroker.utils.io import write_parquet

SYM = "rb0"


def _write_year(lake: DataLake, sym: str, year: int, dates: list[str]):
    df = pd.DataFrame({
        "symbol": sym,
        "datetime": pd.to_datetime(dates),
        "close": [100.0 + i for i in range(len(dates))],
        "volume": [10.0] * len(dates),
    })
    write_parquet(df, lake._path("processed", sym, "1d", year))


@pytest.fixture
def lake(tmp_path) -> DataLake:
    return DataLake(root=str(tmp_path))


def _write_mark(lake: DataLake, year: int, payload: dict):
    p = lake.root / "processed" / SYM / "1d" / f"_MISSING_{year}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestQualityNotes:
    def test_clean_lake_no_notes(self, lake):
        _write_year(lake, SYM, 2022, ["2022-01-04", "2022-12-30"])
        _write_year(lake, SYM, 2023, ["2023-01-03"])
        assert lake.quality_notes(SYM, "1d") == []

    def test_hole_without_mark_flagged_as_dangerous(self, lake):
        """rb0/2020 形态但有标记被删：洞无留痕 = 最危险。"""
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        notes = lake.quality_notes(SYM, "1d")
        assert [(n.kind, n.year) for n in notes] == [("hole", 2020)]
        assert "无 _MISSING 标记" in notes[0].detail

    def test_hole_with_mark_carries_reason(self, lake):
        """生产现状（rb0/2020 已隔离 + 有标记）：告警带标记内容。"""
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        _write_mark(lake, 2020, {
            "status": "MISSING", "year": 2020,
            "reason": "pytest 污染事故",
            "quarantined_to": "artifacts/quarantine/rb0_2020.parquet",
            "rebuild_condition": "pandadata 恢复授权后重拉",
        })
        notes = lake.quality_notes(SYM, "1d")
        assert len(notes) == 1
        assert notes[0].kind == "hole" and notes[0].year == 2020
        assert "pytest 污染事故" in notes[0].detail
        assert "artifacts/quarantine" in notes[0].detail
        assert "pandadata" in notes[0].detail

    def test_mark_year_falls_back_to_filename(self, lake):
        """payload 无 year 字段时回退到文件名解析。"""
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        p = lake.root / "processed" / SYM / "1d" / "_MISSING_2020.json"
        p.write_text(json.dumps({"reason": "仅文件名可解析"}),
                     encoding="utf-8")
        notes = lake.quality_notes(SYM, "1d")
        assert [n.year for n in notes] == [2020]
        assert "仅文件名可解析" in notes[0].detail

    def test_corrupted_mark_json_no_crash(self, lake):
        """标记 JSON 损坏：不崩溃，回退为无留痕洞告警。"""
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        p = lake.root / "processed" / SYM / "1d" / "_MISSING_2020.json"
        p.write_text("{not json", encoding="utf-8")
        notes = lake.quality_notes(SYM, "1d")
        assert [(n.kind, n.year) for n in notes] == [("hole", 2020)]

    def test_stale_mark_when_partition_exists(self, lake):
        """标记与分区并存 → 陈旧标记应清除。"""
        _write_year(lake, SYM, 2020, ["2020-01-06"])
        _write_mark(lake, 2020, {"status": "MISSING", "year": 2020})
        notes = lake.quality_notes(SYM, "1d")
        assert [(n.kind, n.year) for n in notes] == [("stale_mark", 2020)]

    def test_orphan_mark_outside_range(self, lake):
        """范围外孤儿标记（该年无分区也不在洞范围内）。"""
        _write_year(lake, SYM, 2022, ["2022-01-04"])
        _write_mark(lake, 2017, {"status": "MISSING", "year": 2017})
        notes = lake.quality_notes(SYM, "1d")
        assert [(n.kind, n.year) for n in notes] == [("missing_mark", 2017)]

    def test_unparsable_mark_filename_ignored(self, lake):
        """文件名与内容都解析不出年度 → 忽略该标记。"""
        _write_year(lake, SYM, 2022, ["2022-01-04"])
        p = lake.root / "processed" / SYM / "1d" / "_MISSING_bogus.json"
        p.write_text("{}", encoding="utf-8")
        assert lake.quality_notes(SYM, "1d") == []

    def test_absent_symbol_no_notes(self, lake):
        assert lake.quality_notes("nope", "1d") == []


class TestLoadProcessedWarn:
    def test_load_warns_on_hole(self, lake, caplog):
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        with caplog.at_level(logging.WARNING, logger="hexbroker.data.store"):
            bf = lake.load_processed(SYM, "1d")
        # 数据行为不变：洞被拼接，但已告警
        assert len(bf.df) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("2020" in r.getMessage() for r in warnings)

    def test_load_silent_when_clean(self, lake, caplog):
        _write_year(lake, SYM, 2022, ["2022-01-04"])
        _write_year(lake, SYM, 2023, ["2023-01-03"])
        with caplog.at_level(logging.WARNING, logger="hexbroker.data.store"):
            lake.load_processed(SYM, "1d")
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_load_warn_false_suppresses(self, lake, caplog):
        _write_year(lake, SYM, 2019, ["2019-12-30"])
        _write_year(lake, SYM, 2021, ["2021-01-04"])
        with caplog.at_level(logging.WARNING, logger="hexbroker.data.store"):
            lake.load_processed(SYM, "1d", warn=False)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_load_still_raises_when_empty(self, lake):
        with pytest.raises(FileNotFoundError):
            lake.load_processed("nope", "1d")


def test_missing_glob_single_source_of_truth():
    """rebuild 转出与 store 定义同源（P0-12 消除常量重复）。"""
    assert REBUILD_MISSING_GLOB == MISSING_GLOB == "_MISSING_*.json"
