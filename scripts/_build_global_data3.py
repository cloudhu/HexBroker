"""整理腾讯自选股 MCP 美债 ETF K 线 → 标准 parquet（第三批：IEF 7-10年美债）。

IEF = iShares 7-10 Year Treasury Bond ETF，久期最贴近 10Y 收益率的价格代理
（收益率升 → IEF 跌，负相关）。数据 2017-2024 全覆盖。
输出：data/raw/global/ief.parquet（datetime 索引，close 列）
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TR = Path(r"C:\Users\Administrator\.workbuddy\projects\e-Workspace-HexBroker\5e43b9a1-f785-4711-96a2-9fa11b376451\tool-results")

OUT_DIR = ROOT / "data" / "raw" / "global"

FILES = [
    ("mcp-connector-proxy-westock-mcp_data_kline-1786864181717-24ef4a.txt", "usIEF", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786864185127-8670c0.txt", "usIEF", "late2020_2024"),
]


def load_nodes(fn: str) -> pd.DataFrame:
    raw = (TR / fn).read_text(encoding="utf-8")
    s = raw.strip()
    st, en = s.find("{"), s.rfind("}")
    data = json.loads(s[st : en + 1])
    nodes = data["data"]["nodes"]
    df = pd.DataFrame(nodes)
    df["datetime"] = pd.to_datetime(df["date"])
    df = df.set_index("datetime").sort_index()
    df = df[["open", "last", "high", "low"]].rename(columns={"last": "close"})
    return df


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_code: dict[str, pd.DataFrame] = {}
    for fn, code, seg in FILES:
        df = load_nodes(fn)
        print(f"[LOAD] {code} {seg}: {len(df)} 根 {df.index.min().date()} ~ {df.index.max().date()}")
        by_code[code] = pd.concat([by_code.get(code, df), df]).sort_index()
        by_code[code] = by_code[code][~by_code[code].index.duplicated(keep="last")]

    df = by_code["usIEF"].loc["2017-06-01":"2025-01-31"]
    out = OUT_DIR / "ief.parquet"
    df.to_parquet(out)
    print(f"[OK] ief: {len(df)} 根 {df.index.min().date()} ~ {df.index.max().date()} -> {out}")


if __name__ == "__main__":
    main()
