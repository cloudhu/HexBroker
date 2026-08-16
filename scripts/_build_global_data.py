"""整理腾讯自选股 MCP 外盘 K 线 → 标准 parquet。

数据来源：mcp__westock-mcp__data_kline（腾讯自选股）
- usINX = 标普500（2017-01 ~ 2024-12，两段）
- usCL  = WTI 原油（2017-08 ~ 2024-12，两段）

输出：data/raw/global/{spx,wti}.parquet（datetime 索引，close 列）
注意：本脚本只处理已落盘的 tool-results 文件；美元指数 fxDINIW 仅覆盖
2023-12 起（end 参数不生效），无法覆盖 2018-2024 回测窗，暂不使用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TR = Path(r"C:\Users\Administrator\.workbuddy\projects\e-Workspace-HexBroker\5e43b9a1-f785-4711-96a2-9fa11b376451\tool-results")

OUT_DIR = ROOT / "data" / "raw" / "global"

# 文件 → (代码, 段名)
FILES = [
    ("mcp-connector-proxy-westock-mcp_data_kline-1786859107456-afef94.txt", "usINX", "late2019_2024"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786859221662-e6bb0c.txt", "usINX", "early2017_2019"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786859182467-90b693.txt", "usCL", "late2020_2024"),
    ("mcp-connector-proxy-westock-mcp_data_kline-1786859245477-82088d.txt", "usCL", "early2017_2019"),
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

    mapping = {"usINX": "spx", "usCL": "wti"}
    for code, out_name in mapping.items():
        df = by_code[code]
        # 只保留 2018-2024 回测窗（前后留 buffer 供 lookback/前向收益）
        df = df.loc["2017-06-01":"2025-01-31"]
        out = OUT_DIR / f"{out_name}.parquet"
        df.to_parquet(out)
        print(f"[OK] {out_name}: {len(df)} 根 {df.index.min().date()} ~ {df.index.max().date()} -> {out}")

    # 交叉验证：与东财早前拿到的 SPX 样本对比（2018-01-02 close）
    spx = by_code["usINX"]
    print("\n[CHECK] 标普500 关键日期收盘：")
    for d in ["2018-01-02", "2020-03-23", "2024-12-31"]:
        try:
            v = spx.loc[d, "close"]
            print(f"  {d}: {v}")
        except KeyError:
            print(f"  {d}: (无数据)")


if __name__ == "__main__":
    main()
