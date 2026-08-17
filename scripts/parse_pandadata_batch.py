"""批量解析 PandaData 持久化文件 → processed parquet（新品种扩展）。

用法：python scripts/parse_pandadata_batch.py
从 data/interim/pd_files.json 读取映射（sym → [file1, file2, ...]），
解析每个文件，按品种合并去重，落盘 data/raw/processed/{sym}/1d/{year}.parquet。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from scripts.parse_pandadata import parse_pandadata_file


def main() -> None:
    mapping_path = Path("data/interim/pd_files.json")
    if not mapping_path.exists():
        print("[FAIL] 缺少 data/interim/pd_files.json（sym → [持久化文件路径]）")
        return
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    for sym, files in mapping.items():
        frames = []
        for fp in files:
            p = Path(fp)
            if not p.exists():
                print(f"  [SKIP] {sym}: {p} 不存在")
                continue
            df = parse_pandadata_file(p)
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            frames.append(df)
        if not frames:
            print(f"  [FAIL] {sym}: 无可用文件")
            continue
        full = pd.concat(frames).drop_duplicates(subset="date").set_index("date").sort_index()
        rows = []
        for idx, r in full.iterrows():
            rows.append({
                "symbol": sym, "datetime": idx,
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "volume": r["volume"], "amount": r.get("amount", 0.0) or 0.0,
                "open_interest": r.get("open_interest", 0.0) or 0.0,
                "raw_close": r["close"], "adj_close": r["close"],
                "limit_up": False, "limit_down": False, "is_rollover": False,
            })
        out = pd.DataFrame(rows)
        out_dir = Path(f"data/raw/processed/{sym}/1d")
        out_dir.mkdir(parents=True, exist_ok=True)
        for yr, grp in out.groupby(out["datetime"].dt.year):
            grp.to_parquet(out_dir / f"{yr}.parquet", index=False)
        print(f"[OK] {sym}: {len(out)} 根 {out['datetime'].min().date()}~{out['datetime'].max().date()} → 落盘")


if __name__ == "__main__":
    main()
