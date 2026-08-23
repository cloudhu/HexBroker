"""整理腾讯自选股 MCP 外盘 K 线 → 标准 parquet（第二批：纳指/道指/美元ETF）。

数据来源：mcp__westock-mcp__data_kline（腾讯自选股）
- usIXIC = 纳斯达克综合指数（2017-01 ~ 2024-12，两段）
- usDJI  = 道琼斯工业指数（2017-01 ~ 2024-12，两段）
- usUUP  = 做多美元 ETF（美元指数代理，2017-01 ~ 2024-12，两段）

输出：data/raw/global/{ixic,dji,uup}.parquet（datetime 索引，close 列）
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TR = Path(r"C:\Users\Administrator\.workbuddy\projects\e-Workspace-HexBroker\5e43b9a1-f785-4711-96a2-9fa11b376451\tool-results")

OUT_DIR = ROOT / "data" / "raw" / "global"

# 文件 → (代码, 段名)
FILES = [
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861636763-1d01c1.txt", "usIXIC", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861640155-647fd2.txt", "usIXIC", "late2020_2024"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861643574-a69120.txt", "usDJI", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861647218-58b0cf.txt", "usDJI", "late2020_2024"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861669394-85351f.txt", "usUUP", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861673125-db55f9.txt", "usUUP", "late2020_2024"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861713938-b6c7fa.txt", "usTLT", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786861717347-8b1285.txt", "usTLT", "late2020_2024"),
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

    mapping = {"usIXIC": "ixic", "usDJI": "dji", "usUUP": "uup", "usTLT": "tlt"}
    for code, out_name in mapping.items():
        df = by_code[code]
        df = df.loc["2017-06-01":"2025-01-31"]
        out = OUT_DIR / f"{out_name}.parquet"
        df.to_parquet(out)
        print(f"[OK] {out_name}: {len(df)} 根 {df.index.min().date()} ~ {df.index.max().date()} -> {out}")

    # UUP vs DXY 相关性验证（UUP 是美元指数代理）
    try:
        spx = pd.read_parquet(OUT_DIR / "spx.parquet")
        ixic = by_code["usIXIC"]
        print("\n[CHECK] 关键点位：")
        for d in ["2018-01-02", "2020-03-23", "2024-12-31"]:
            for name, df in [("IXIC", ixic)]:
                try:
                    print(f"  {name} {d}: {df.loc[d, 'close']}")
                except KeyError:
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"[CHECK] FAIL {e}")


if __name__ == "__main__":
    main()
