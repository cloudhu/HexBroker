"""从 wind 持久化 JSON 提取美债 10Y 序列，补齐 t10y.parquet 到 2026-08。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

SRC = r"C:/Users/Administrator/.workbuddy/projects/e-Workspace-HexBroker/5e43b9a1-f785-4711-96a2-9fa11b376451/tool-results/call_00_ET_BnbMy9gN3eR8Bhv2zDnV8187.txt"
OUT = Path("data/raw/global/t10y.parquet")


def main() -> None:
    raw = json.loads(Path(SRC).read_text(encoding="utf-8"))
    series = None
    for d in raw["data"]["data"]:
        if d["meta"]["code"] == "G0000891":
            series = pd.Series(d["value"], index=pd.to_datetime(d["date"], format="%Y%m%d"))
            break
    if series is None:
        print("[FAIL] 未找到 G0000891")
        return
    series = series.sort_index()
    series = series[~series.index.duplicated(keep="last")]
    print(f"[OK] wind 美债10Y: {len(series)} 根 {series.index.min().date()}~{series.index.max().date()}")

    if OUT.exists():
        old = pd.read_parquet(OUT)
        old_idx = pd.to_datetime(old.index)
        old_s = old["close"].astype(float)
        old_s.index = old_idx
        print(f"[OK] 原 t10y: {len(old_s)} 根 {old_s.index.min().date()}~{old_s.index.max().date()}")
        # 合并：wind 覆盖更全，直接以 wind 为准（同源美债收益率），保留旧数据尾部以补 wind 缺失
        merged = series.combine_first(old_s).sort_index()
    else:
        merged = series
    out = pd.DataFrame({"close": merged.astype(float)})
    out.index.name = "datetime"
    out.to_parquet(OUT)
    print(f"[OK] t10y.parquet 已更新: {len(out)} 根 {out.index.min().date()}~{out.index.max().date()}")


if __name__ == "__main__":
    main()
