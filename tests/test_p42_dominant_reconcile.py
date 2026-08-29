"""P1 收口：p42 dominant×因子 对账探针单测。

夹具手工构造湖内 processed parquet（datetime/raw_close/adj_close 列）
+ dominant 快照，覆盖四类判定：同日命中 / 交易日偏移命中 / ni0 型
（dominant 切了因子没动）/ 单日回落毛刺 / 持续阶梯 / 快照接缝排除。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data import dominant as dom


def _load_p42():
    spec = importlib.util.spec_from_file_location(
        "p42_dominant_reconcile",
        Path(__file__).resolve().parents[1] / "scripts" / "p42_dominant_reconcile.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p42_dominant_reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- 夹具 --------------------------------------------------------------------

def _biz_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def _mk_lake(root: Path, sym0: str, dates: pd.DatetimeIndex,
             raw: list[float], adj: list[float]) -> None:
    """单年分区 processed parquet（datetime 列布局，与生产一致）。"""
    df = pd.DataFrame({
        "symbol": [sym0.upper()] * len(dates),
        "datetime": dates,
        "raw_close": raw,
        "adj_close": adj,
    })
    d = root / "processed" / sym0 / "1d"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / f"{dates[0].year}.parquet")


def _mk_snapshot(root: Path, sym0: str, dates_ids: list[tuple[str, str]]) -> None:
    rows = [{"date": d, "dominant_id": did, "open_interest": 100.0}
            for d, did in dates_ids]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    cal = df.set_index("date").rename_axis("datetime")
    dom.save_calendar(root, sym0, cal)


# ---- 单元 --------------------------------------------------------------------

def test_matched_same_day_and_offset(tmp_path):
    mod = _load_p42()
    dates = _biz_days("2023-01-02", 10)  # 01-02..01-13
    # 因子两段：01-06 切 5%（105），01-09 再切 5%（110.25）
    raw = [100.0] * 10
    adj = [100.0] * 4 + [105.0] + [110.25] * 5
    _mk_lake(tmp_path, "cu0", dates, raw, adj)
    # dominant 切换日：01-06（与因子同日）、01-10（因子已提前 1 交易日至 01-09）
    _mk_snapshot(tmp_path, "cu0",
                 [("20230104", "CU2305.SHF"), ("20230106", "CU2306.SHF"),
                  ("20230110", "CU2307.SHF"), ("20230113", "CU2307.SHF")])
    r = mod.reconcile_sym(tmp_path, "cu0", tol=1e-3, window=3,
                          start=None, end=None)
    assert r["status"] == "ok"
    assert r["n_switches_a"] == 2  # 段首 01-04 不计
    assert len(r["matched"]) == 2
    off = {k: v["offset"] for k, v in r["matched"].items()}
    assert off["2023-01-06"] == 0   # 同日命中
    assert off["2023-01-10"] == -1  # A 晚于因子 1 个交易日
    assert r["unmatched_a"] == []


def test_ni0_type_unmatched_a(tmp_path):
    mod = _load_p42()
    dates = _biz_days("2023-01-02", 10)
    raw = [100.0] * 10
    adj = [150.0] * 10  # 因子全程不动（k 恒定）
    _mk_lake(tmp_path, "cu0", dates, raw, adj)
    _mk_snapshot(tmp_path, "cu0",
                 [("20230103", "CU2305.SHF"), ("20230106", "CU2306.SHF"),
                  ("20230109", "CU2306.SHF")])
    r = mod.reconcile_sym(tmp_path, "cu0", tol=1e-3, window=3,
                          start=None, end=None)
    assert r["n_switches_a"] == 1
    assert r["unmatched_a"] == ["2023-01-06"]  # dominant 切了、因子没动
    assert len(r["matched"]) == 0


def test_spike_vs_step_classification(tmp_path):
    mod = _load_p42()
    dates = _biz_days("2023-01-02", 10)  # idx: 0=01-02 .. 9=01-13
    # 基线比值 1.5（adj=150, raw=100）
    # 单日毛刺：01-05 raw 误含复权价（=150 → 比值 1.0），次日回落 1.5
    # 持续阶梯：01-10 起因子 1.5 → 1.65（adj=165）
    raw = [100.0] * 3 + [150.0] + [100.0] * 6
    adj = [150.0] * 6 + [165.0] * 4
    _mk_lake(tmp_path, "cu0", dates, raw, adj)
    _mk_snapshot(tmp_path, "cu0",
                 [("20230103", "CU2305.SHF"), ("20230113", "CU2305.SHF")])
    r = mod.reconcile_sym(tmp_path, "cu0", tol=1e-3, window=3,
                          start=None, end=None)
    assert r["n_switches_a"] == 0
    # 单日毛刺天然产生"下跳(01-05) + 回复(01-06)"两个突变，成对归入毛刺
    assert r["unmatched_b_spikes"] == ["2023-01-05", "2023-01-06"]
    assert r["unmatched_b_steps"] == ["2023-01-10"]   # 持续阶梯 → 待复核


def test_seam_switch_excluded(tmp_path):
    mod = _load_p42()
    # 两段快照：2020 段 + 2023 段（间隔 >45 日历日 → 接缝伪切换）
    _mk_snapshot(tmp_path, "rb0",
                 [("20200102", "RB2005.SHF"), ("20200106", "RB2010.SHF"),
                  ("20230103", "RB2305.SHF"), ("20230106", "RB2310.SHF")])
    dates = _biz_days("2020-01-02", 10)
    raw = [100.0] * 10
    adj = [100.0] * 4 + [102.0] * 6  # 01-06 因子切换，与 A 01-06 同日
    _mk_lake(tmp_path, "rb0", dates, raw, adj)
    r = mod.reconcile_sym(tmp_path, "rb0", tol=1e-3, window=3,
                          start=None, end=None)
    # 段首 2020-01-02 + 接缝 2023-01-03 均不计入 A；
    # 真切换 = 2020-01-06（命中）+ 2023-01-06（湖内无 2023 数据 → 未命中）
    assert r["n_switches_a"] == 2
    all_a = list(r["matched"]) + r["unmatched_a"]
    assert "2023-01-03" not in all_a  # 接缝伪切换被排除
    assert "2020-01-06" in r["matched"]
    assert r["unmatched_a"] == ["2023-01-06"]


def test_skipped_when_missing(tmp_path):
    mod = _load_p42()
    # 无快照
    r = mod.reconcile_sym(tmp_path, "cu0", tol=1e-3, window=3,
                          start=None, end=None)
    assert r["status"] == "skipped"
    # 有快照无湖内数据
    _mk_snapshot(tmp_path, "cu0", [("20230103", "CU2305.SHF")])
    r2 = mod.reconcile_sym(tmp_path, "cu0", tol=1e-3, window=3,
                           start=None, end=None)
    assert r2["status"] == "skipped" and "processed" in r2["reason"]


def test_main_end_to_end_with_json(tmp_path, capsys):
    mod = _load_p42()
    dates = _biz_days("2023-01-02", 10)
    _mk_lake(tmp_path, "cu0", dates, [100.0] * 10,
             [100.0] * 4 + [105.0] * 6)
    _mk_snapshot(tmp_path, "cu0",
                 [("20230103", "CU2305.SHF"), ("20230106", "CU2306.SHF"),
                  ("20230113", "CU2306.SHF")])
    out = tmp_path / "report.json"
    rc = mod.main(["--sym", "cu0", "--data-root", str(tmp_path),
                   "--json", str(out)])
    assert rc == 0
    assert out.exists()
    reports = json.loads(out.read_text(encoding="utf-8"))
    assert reports[0]["sym0"] == "cu0"
    assert reports[0]["status"] == "ok"
    # 纯只读探针：除显式 --json 外不写盘（无 --json 时不产生文件）
    rc2 = mod.main(["--sym", "cu0", "--data-root", str(tmp_path)])
    assert rc2 == 0


def test_main_spike_scan(tmp_path, capsys):
    mod = _load_p42()
    dates = _biz_days("2023-01-02", 10)
    # 单日毛刺：01-05 raw 误含复权价，次日回落
    _mk_lake(tmp_path, "cu0", dates,
             [100.0] * 3 + [150.0] + [100.0] * 6, [150.0] * 10)
    out = tmp_path / "spikes.json"
    rc = mod.main(["--spike-scan", "--data-root", str(tmp_path),
                   "--json", str(out)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "SPIKE-SCAN" in captured and "cu0" in captured
    found = json.loads(out.read_text(encoding="utf-8"))
    assert found[0]["sym0"] == "cu0"
    assert found[0]["spikes"] == ["2023-01-05", "2023-01-06"]
