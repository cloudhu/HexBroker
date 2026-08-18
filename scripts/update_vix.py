"""P1 VIX 落盘：从 wind EDB 持久化文件解析 CBOE VIX（G0003892）→ data/raw/global/vix.parquet。

格式对齐现有全局数据（t10y.parquet）：DatetimeIndex + close 列。
2026-08-18 执行。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

WIND_FILE = Path(
    r"C:/Users/Administrator/.workbuddy/projects/e-Workspace-HexBroker/"
    r"5e43b9a1-f785-4711-96a2-9fa11b376451/tool-results/"
    r"mcp-connector-proxy-wind-finance_natural_language_get_edb_data-1787038968778-8ab1a6.txt"
)
OUT = Path("data/raw/global/vix.parquet")


def main() -> None:
    d = json.load(open(WIND_FILE, encoding="utf-8"))
    for it in d["data"]["data"]:
        if it["meta"]["code"] == "G0003892":
            dates = pd.to_datetime(it["date"], format="%Y%m%d")
            vals = it["value"]
            df = pd.DataFrame({"close": vals}, index=dates)
            df.index.name = "datetime"
            df = df[~df.index.duplicated(keep="last")].sort_index()
            df.to_parquet(OUT)
            print(f"[OK] VIX 落盘 {OUT}: {len(df)} 根 {df.index.min().date()}~{df.index.max().date()}")
            print(f"      末值 close={df['close'].iloc[-1]:.2f} | NaN={int(df['close'].isna().sum())}")
            return
    raise SystemExit("G0003892 未找到")


if __name__ == "__main__":
    main()
