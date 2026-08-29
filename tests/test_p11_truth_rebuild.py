"""P0-11 真值重建驱动器（``scripts/p11_truth_rebuild.py``）单测。

为何单测而非跑真数据
--------------------
--apply 的目标就是生产湖 rb0/2020 分区 —— 真数据测试等于生产污染，
铁律禁止。全部用 tmp 湖 + 合成 persisted JSON 验证驱动器编排逻辑
（解析/规范化/跨界过滤/dry-run 零写盘/apply 全链路），rebuild_partition
本体语义已由 tests/test_rebuild.py 22 测试守护。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS / "p11_truth_rebuild.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("p11_truth_rebuild", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p11_truth_rebuild"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _persisted_payload(dates: list[str], base: float = 3500.0) -> dict:
    """p6_4 persisted 格式的合成真值（RB 主力，close_pcr 口径）。"""
    rows = []
    for i, d in enumerate(dates):
        rows.append([
            d.replace("-", ""), "RB_DOMINANT.SHF", "RB", "SHF", f"RB{i + 1}.SHF",
            base + i, base + i + 5, base + i - 5, base + i + 1,
            100000.0 + i, 0.0, 200000.0 + i, base + i + 1,
        ])
    return {
        "ok": True,
        "method": "get_future_daily_post",
        "params": {"underlying_symbol": ["RB"], "start_date": dates[0].replace("-", ""),
                   "end_date": dates[-1].replace("-", ""), "method": "close_pcr"},
        "result": {
            "type": "dataframe",
            "columns": ["date", "symbol", "underlying_symbol", "exchange",
                        "dominant_id", "open", "high", "low", "close",
                        "volume", "amount", "open_interest", "settle"],
            "rows": rows,
        },
    }


def _write_persisted(tmp: Path, payload: dict) -> Path:
    p = tmp / "truth.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _make_lake(tmp: Path, year: int = 2020) -> Path:
    """tmp 生产湖镜像：rb0 相邻年度分区 + _MISSING 标记，无 2020.parquet。"""
    from hexbroker.utils.io import write_parquet
    root = tmp / "raw"
    d = root / "processed" / "rb0" / "1d"
    d.mkdir(parents=True)
    for y, n in ((2019, 3), (2021, 3)):
        df = pd.DataFrame({
            "symbol": ["rb0"] * n,
            "datetime": pd.to_datetime([f"{y}-01-0{i + 1}" for i in range(n)]),
            "open": [3500.0] * n, "high": [3510.0] * n, "low": [3490.0] * n,
            "close": [3500.0] * n, "volume": [1.0] * n, "amount": [0.0] * n,
            "open_interest": [1.0] * n,
            "raw_close": [3500.0] * n, "adj_close": [3500.0] * n,
            "limit_up": False, "limit_down": False, "is_rollover": False,
        })
        write_parquet(df, d / f"{y}.parquet")
    (d / f"_MISSING_{year}.json").write_text(
        json.dumps({"status": "MISSING", "symbol": "rb0", "year": year}, ensure_ascii=False),
        encoding="utf-8")
    return root


def test_dry_run_makes_no_writes(tmp_path):
    dates = [f"2020-01-0{i}" for i in range(1, 6)]
    persisted = _write_persisted(tmp_path, _persisted_payload(dates))
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root)])

    assert rc == 0
    assert not (root / "processed" / "rb0" / "1d" / "2020.parquet").exists()
    assert (root / "processed" / "rb0" / "1d" / "_MISSING_2020.json").exists()


def test_apply_rebuilds_partition_and_clears_marker(tmp_path):
    dates = [f"2020-01-0{i}" for i in range(1, 6)]
    persisted = _write_persisted(tmp_path, _persisted_payload(dates))
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root),
                   "--apply", "--skip-nominal"])

    assert rc == 0
    d = root / "processed" / "rb0" / "1d"
    assert (d / "2020.parquet").exists()
    assert not (d / "_MISSING_2020.json").exists()  # 洞标记已清
    # manifest 已重写
    assert (d / "manifest.json").exists()

    # 读回归：全量行数 = 相邻年 + 真值年；2020 行进数据
    from hexbroker.data.store import DataLake
    lake = DataLake(root=root)
    bf = lake.load_processed("rb0", "1d", warn=False)
    assert len(bf.df) == 3 + 5 + 3
    dt = bf.df.index.get_level_values("datetime")
    assert (dt.year == 2020).sum() == 5
    # quality_notes 不再报 2020 洞
    holes = [n for n in lake.quality_notes("rb0", "1d") if n.kind == "hole"]
    assert holes == []


def test_apply_filters_cross_year_truth_rows(tmp_path):
    """真值混入非目标年度行 → 驱动器过滤（rebuild_partition 亦有双保险）。"""
    dates = ["2020-01-02", "2020-01-03", "2021-01-04"]
    payload = _persisted_payload(dates)
    persisted = _write_persisted(tmp_path, payload)
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root),
                   "--apply", "--skip-nominal"])

    assert rc == 0
    from hexbroker.data.store import DataLake
    lake = DataLake(root=root)
    bf = lake.load_processed("rb0", "1d", warn=False)
    dt = bf.df.index.get_level_values("datetime")
    # 2021 行未写入 2020 分区（跨界拒绝语义）
    assert (dt.year == 2021).sum() == 3  # 仅有 _make_lake 预置的 2021 3 行
    assert (dt.year == 2020).sum() == 2


def test_missing_persisted_file_fails_cleanly(tmp_path):
    rc = mod.main(["--persisted", str(tmp_path / "nope.json"),
                   "--data-root", str(tmp_path)])
    assert rc == 2


def test_empty_year_truth_fails_without_write(tmp_path):
    """真值与目标年度无交集 → 显式失败，零写盘。"""
    dates = ["2023-03-01", "2023-03-02"]
    persisted = _write_persisted(tmp_path, _persisted_payload(dates))
    root = _make_lake(tmp_path)

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root),
                   "--apply", "--skip-nominal"])

    assert rc == 1
    assert not (root / "processed" / "rb0" / "1d" / "2020.parquet").exists()
    assert (root / "processed" / "rb0" / "1d" / "_MISSING_2020.json").exists()


# ---------------------------------------------------------------------------
# 既有分区整年替换（主理人 2026-08-29 拍板 ③：cu0/rb0 2023 名义价冒充根治）
# ---------------------------------------------------------------------------
def _write_corrupted_partition(root: Path, dates: list[str]) -> Path:
    """写入"名义价冒充"形态的既有分区（close=3500 恒定，脏数据）。"""
    from hexbroker.utils.io import write_parquet
    n = len(dates)
    df = pd.DataFrame({
        "symbol": ["rb0"] * n,
        "datetime": pd.to_datetime(dates),
        "open": [3500.0] * n, "high": [3500.0] * n, "low": [3500.0] * n,
        "close": [3500.0] * n, "volume": [1.0] * n, "amount": [0.0] * n,
        "open_interest": [1.0] * n,
        "raw_close": [3500.0] * n, "adj_close": [3500.0] * n,
        "limit_up": False, "limit_down": False, "is_rollover": False,
    })
    d = root / "processed" / "rb0" / "1d"
    write_parquet(df, d / "2020.parquet")
    return d / "2020.parquet"


def test_apply_existing_partition_full_year_replace(tmp_path):
    """既有分区 + 真值全覆盖 → 同日期逐行替换 + 新日期追加（整年替换）。"""
    truth_dates = [f"2020-01-0{i}" for i in range(1, 6)]
    persisted = _write_persisted(tmp_path, _persisted_payload(truth_dates))
    root = _make_lake(tmp_path)
    # 去掉 _MISSING 标记并写入脏分区（前 3 个真值日期，恒定 3500）
    (root / "processed" / "rb0" / "1d" / "_MISSING_2020.json").unlink()
    target = _write_corrupted_partition(root, truth_dates[:3])
    before = target.read_bytes()

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root),
                   "--apply", "--skip-nominal"])

    assert rc == 0
    from hexbroker.data.store import DataLake
    lake = DataLake(root=root)
    bf = lake.load_processed("rb0", "1d", warn=False)
    dt = bf.df.index.get_level_values("datetime")
    # 行数 = 3 相邻年 + 5 真值行（3 替换 + 2 追加）
    assert (dt.year == 2020).sum() == 5
    y20 = bf.df[dt.year == 2020]
    # 脏行已被真值替换（close 3500 → 真值 3501+），且追加 2 行新日期
    assert (y20["close"] == 3500.0).sum() == 0
    # 真值文件/预检证据：2020.parquet 内容已变
    assert target.read_bytes() != before


def test_apply_existing_partition_uncovered_dates_aborts(tmp_path):
    """既有分区日期未被真值全覆盖 → 中止零写盘（脏行不得静默残留）。"""
    truth_dates = [f"2020-01-0{i}" for i in range(1, 4)]  # 真值只有 3 天
    persisted = _write_persisted(tmp_path, _persisted_payload(truth_dates))
    root = _make_lake(tmp_path)
    (root / "processed" / "rb0" / "1d" / "_MISSING_2020.json").unlink()
    # 脏分区含真值没有的日期 01-06、01-07
    target = _write_corrupted_partition(
        root, ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"])
    before = target.read_bytes()

    rc = mod.main(["--persisted", str(persisted), "--data-root", str(root),
                   "--apply", "--skip-nominal"])

    assert rc == 1
    # 分区逐字节未动（零写盘）
    assert target.read_bytes() == before
