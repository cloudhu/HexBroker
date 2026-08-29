"""Dominant 日历快照（``hexbroker/data/dominant.py`` + p41 驱动器）单测。

夹具按 pandadata ``get_future_daily_post`` 真实列契约构造
（date=YYYYMMDD 无横线、dominant_id、不复权 open_interest）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data import dominant as dom

# ---- 真实契约夹具 -----------------------------------------------------------

PANDADATA_COLUMNS = ["date", "symbol", "underlying_symbol", "exchange",
                     "dominant_id", "open", "high", "low", "close", "volume",
                     "open_interest", "amount", "settlement", "pre_settlement",
                     "limit_up", "limit_down", "day_session_open", "method"]


def _pandadata_rows(dates_ids_oi: list[tuple[str, str, float]],
                    symbol: str = "RB") -> list[list]:
    return [[d, symbol, symbol, "SHF", did, 0.0, 0.0, 0.0, 0.0, 0, oi, 0.0,
             0.0, 0.0, 0, 0, 0.0, "close_pcr"] for d, did, oi in dates_ids_oi]


def _persisted_json(path: Path, rows: list[list]) -> None:
    path.write_text(json.dumps({
        "ok": True, "method": "get_future_daily_post",
        "params": {},
        "result": {"type": "dataframe", "columns": PANDADATA_COLUMNS,
                   "rows": rows},
    }, ensure_ascii=False), encoding="utf-8")


def _cal(dates_ids: list[tuple[str, str]]) -> pd.DataFrame:
    return dom.extract_calendar(pd.DataFrame(
        _pandadata_rows([(d, did, 100.0) for d, did in dates_ids]),
        columns=PANDADATA_COLUMNS))


# ---- extract_calendar -------------------------------------------------------

def test_extract_normalizes_dates_and_sorts():
    cal = _cal([("20230104", "RB2305.SHF"), ("20230103", "RB2305.SHF"),
                ("20230103", "RB2305.SHF")])  # 乱序 + 重复日期
    assert list(cal.index) == [pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-04")]
    assert set(cal.columns) == {"dominant_id", "open_interest"}
    assert cal["dominant_id"].iloc[0] == "RB2305.SHF"


def test_extract_missing_required_col_raises():
    with pytest.raises(ValueError, match="dominant_id"):
        dom.extract_calendar(pd.DataFrame([{"date": "20230103"}]))


# ---- save / load / upsert ---------------------------------------------------

def test_save_load_roundtrip_and_upsert(tmp_path):
    cal = _cal([("20230103", "RB2305.SHF"), ("20230104", "RB2305.SHF")])
    stat = dom.save_calendar(tmp_path, "rb0", cal)
    assert (stat["total"], stat["appended"], stat["overwritten"]) == (2, 2, 0)
    # 幂等：同数据再存 → 全覆盖零新增
    stat2 = dom.save_calendar(tmp_path, "rb0", cal)
    assert (stat2["total"], stat2["appended"], stat2["overwritten"]) == (2, 0, 2)
    # upsert：同日期值变化（覆盖）+ 新日期（追加）
    cal2 = _cal([("20230104", "RB2405.SHF"), ("20230105", "RB2405.SHF")])
    stat3 = dom.save_calendar(tmp_path, "rb0", cal2)
    assert (stat3["total"], stat3["appended"], stat3["overwritten"]) == (3, 1, 1)
    loaded = dom.load_calendar(tmp_path, "rb0")
    assert loaded.loc[pd.Timestamp("2023-01-04"), "dominant_id"] == "RB2405.SHF"
    assert loaded.loc[pd.Timestamp("2023-01-03"), "dominant_id"] == "RB2305.SHF"


def test_load_missing_returns_none(tmp_path):
    assert dom.load_calendar(tmp_path, "rb0") is None


# ---- detect_switches / rollover_dates ---------------------------------------

def test_detect_switches_and_rollover_window(tmp_path):
    cal = _cal([("20230103", "RB2305.SHF"), ("20230104", "RB2305.SHF"),
                ("20230105", "RB2405.SHF"), ("20230106", "RB2405.SHF")])
    dom.save_calendar(tmp_path, "rb0", cal)
    sw = dom.detect_switches(cal)
    assert list(sw["to_id"]) == ["RB2305.SHF", "RB2405.SHF"]
    assert sw["from_id"].iloc[1] == "RB2305.SHF"
    assert pd.isna(sw["from_id"].iloc[0])  # 首个合约无 from（pandas 将 None 规范化为 NaN）
    # 窗口 (start, end]：含 01-05 切换日
    assert dom.rollover_dates(tmp_path, "rb0", "2023-01-04", "2023-01-05") \
        == [pd.Timestamp("2023-01-05")]
    # 窗口外为空
    assert dom.rollover_dates(tmp_path, "rb0", "2023-01-06", "2023-01-08") == []


def test_rollover_missing_snapshot_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        dom.rollover_dates(tmp_path, "rb0", "2023-01-01", "2023-12-31")


# ---- p41 驱动器 ---------------------------------------------------------------

def _load_p41():
    spec = importlib.util.spec_from_file_location(
        "p41_dominant_snapshot",
        Path(__file__).resolve().parents[1] / "scripts" / "p41_dominant_snapshot.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p41_dominant_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_p41_dry_run_and_apply(tmp_path):
    mod = _load_p41()
    lake = tmp_path / "raw"
    jdir = tmp_path / "persisted"
    jdir.mkdir()
    _persisted_json(jdir / "rb_2023.json",
                    _pandadata_rows([("20230103", "RB2305.SHF", 100.0),
                                     ("20230104", "RB2305.SHF", 101.0)]))
    before = {str(p) for p in lake.rglob("*")}
    # dry-run：零写盘，品种从行内 symbol 列推断 RB → rb0
    rc = mod.main(["--input-dir", str(jdir), "--data-root", str(lake)])
    assert rc == 0
    assert {str(p) for p in lake.rglob("*")} == before
    # apply：快照落盘
    rc = mod.main(["--input-dir", str(jdir), "--data-root", str(lake), "--apply"])
    assert rc == 0
    cal = dom.load_calendar(lake, "rb0")
    assert len(cal) == 2
    # 幂等复跑：统计零新增
    rc = mod.main(["--input-dir", str(jdir), "--data-root", str(lake), "--apply"])
    assert rc == 0


def test_p41_sym_map_override(tmp_path):
    mod = _load_p41()
    lake = tmp_path / "raw"
    jdir = tmp_path / "persisted"
    jdir.mkdir()
    _persisted_json(jdir / "cu_2023.json",
                    _pandadata_rows([("20230103", "CU2305.SHF", 100.0)],
                                    symbol="CU"))
    rc = mod.main(["--input-dir", str(jdir), "--data-root", str(lake),
                   "--apply", "--sym-map", "CU:cu0"])
    assert rc == 0
    assert dom.load_calendar(lake, "cu0") is not None


def test_p41_explicit_sym_normalizes_to_sym0(tmp_path):
    """--sym RB（品种）→ rb0（连续符号）；已是 rb0 则原样。"""
    mod = _load_p41()
    lake = tmp_path / "raw"
    jdir = tmp_path / "persisted"
    jdir.mkdir()
    _persisted_json(jdir / "rb_2023.json",
                    _pandadata_rows([("20230103", "RB2305.SHF", 100.0)]))
    rc = mod.main(["--input", str(jdir / "rb_2023.json"), "--sym", "RB",
                   "--data-root", str(lake), "--apply"])
    assert rc == 0
    cal = dom.load_calendar(lake, "rb0")
    assert cal is not None and len(cal) == 1
    assert dom.load_calendar(lake, "rb") is None  # 不得落成裸品种名
