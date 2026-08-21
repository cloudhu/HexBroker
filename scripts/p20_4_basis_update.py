#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P20-4 基差增量补齐：PandaData get_future_basis → 合并写回 basis_{SYM}.parquet。

- 数据源：artifacts/p20_4_raw/seg_20260820_basis.json（result[] 数组格式，
  字段：basis / basis_ratio / date(YYYYMMDD) / spot_price / underlying_symbol）
- 写回：data/raw/fundamental/basis_{SYM}.parquet（写前备份 → artifacts/backup_p204/）
- 登记：artifacts/p20_4_basis_applied.json（幂等）
- 验证：18 品种基差最新日期（SC 无 08-18~20 数据，保持 04-30 如实标注）
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FUND_DIR = ROOT / "data" / "raw" / "fundamental"
ART_DIR = ROOT / "artifacts"
RAW_DIR = ART_DIR / "p20_4_raw"
BACKUP_DIR = ART_DIR / "backup_p204"
APPLIED_PATH = ART_DIR / "p20_4_basis_applied.json"

SYMBOLS18 = ["AU", "AG", "M", "CU", "RB", "I", "AL", "ZN", "NI", "HC",
             "Y", "P", "J", "JM", "SR", "CF", "TA", "SC"]
SEG = "20260818_20260820"
SRC_FILE = RAW_DIR / "seg_20260820_basis.json"

REQUIRED_COLS = ["date", "basis_ratio", "basis", "spot_price"]


def load_applied() -> list[dict]:
    if APPLIED_PATH.exists():
        try:
            data = json.loads(APPLIED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
    return []


def save_applied(records: list[dict]) -> None:
    ART_DIR.mkdir(parents=True, exist_ok=True)
    APPLIED_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_result_rows(path: Path) -> pd.DataFrame:
    data = json.loads(path.read_text(encoding="utf-8"))
    res = data.get("result")
    if not isinstance(res, list):
        raise ValueError(f"{path.name}: result 非数组")
    return pd.DataFrame(res)


def backup(fpath: Path) -> Path | None:
    if not fpath.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"basis_{fpath.stem}_prefill_{ts}.parquet"
    shutil.copy2(fpath, dest)
    return dest


def normalize_new(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """过滤品种、归一化 schema（与既有 basis_*.parquet 对齐）。"""
    sub = df[df["underlying_symbol"].astype(str).str.upper() == sym].copy()
    if sub.empty:
        return pd.DataFrame(columns=REQUIRED_COLS)
    sub["date"] = pd.to_datetime(sub["date"].astype(str), format="%Y%m%d",
                                 errors="coerce").dt.normalize()
    sub = sub.dropna(subset=["date"])
    for col in ("basis_ratio", "basis", "spot_price"):
        if col not in sub.columns:
            sub[col] = 0.0
        sub[col] = pd.to_numeric(sub[col], errors="coerce").fillna(0.0)
    return sub[REQUIRED_COLS].copy()


def coerce_schema(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for col in ("basis_ratio", "basis", "spot_price"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df[REQUIRED_COLS]


def main() -> int:
    if not SRC_FILE.exists():
        print(f"[FAIL] 持久化文件不存在: {SRC_FILE}")
        return 1
    df = load_result_rows(SRC_FILE)
    print(f"[LOAD] {SRC_FILE.name}: {len(df)} 行，品种 {sorted(df['underlying_symbol'].unique())}")

    applied = load_applied()
    records: list[dict] = []
    summary: dict[str, str] = {}

    for sym in SYMBOLS18:
        fpath = FUND_DIR / f"basis_{sym}.parquet"
        new_rows = normalize_new(df, sym)
        if new_rows.empty:
            summary[sym] = "NO_NEW_DATA"
            print(f"[SKIP] {sym}: 无 08-18~20 基差数据（保持既有最新）")
            continue

        # 幂等：同 (sym, seg) 已应用则跳过（--force 无，本脚本幂等按日期去重天然安全）
        if any(a.get("sym") == sym and a.get("seg") == SEG for a in applied):
            print(f"[SKIP] {sym} {SEG} 已应用")
            summary[sym] = "ALREADY_APPLIED"
            continue

        if fpath.exists():
            old = coerce_schema(pd.read_parquet(fpath))
        else:
            old = pd.DataFrame(columns=REQUIRED_COLS)
        bkp = backup(fpath)

        merged = coerce_schema(pd.concat([old, new_rows], ignore_index=True))
        merged = (
            merged.drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        # 原子写回
        fpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = fpath.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(fpath)

        rows_before = int(len(old))
        rows_added = int(len(merged) - len(old.drop_duplicates(subset="date", keep="last")))
        records.append({
            "sym": sym, "seg": SEG,
            "persisted_file": str(SRC_FILE),
            "backup": str(bkp) if bkp else None,
            "rows_before": rows_before,
            "rows_after": int(len(merged)),
            "rows_added": rows_added,
            "new_date_min": new_rows["date"].min().date().isoformat(),
            "new_date_max": new_rows["date"].max().date().isoformat(),
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        })
        summary[sym] = f"+{rows_added}d"
        print(f"[MERGE] {sym}: {rows_before} → {len(merged)} 行 "
              f"(+{rows_added}，备份 {bkp.name if bkp else '无'})")

    applied.extend(records)
    save_applied(applied)

    # 验证：18 品种基差最新日期
    print("\n[VERIFY] 18 品种基差最新日期：")
    for sym in SYMBOLS18:
        fpath = FUND_DIR / f"basis_{sym}.parquet"
        if not fpath.exists():
            print(f"  {sym}: 无文件")
            continue
        cur = coerce_schema(pd.read_parquet(fpath))
        last = cur["date"].max().date()
        flag = "OK" if last == pd.Timestamp("2026-08-20").date() else f"!! (止于 {last})"
        print(f"  {sym}: {last} {flag}")

    print(f"\n[DONE] 合并 {len(records)} 个品种 → {APPLIED_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
