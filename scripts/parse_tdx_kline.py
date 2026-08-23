"""解析 tdx MCP 持久化的 K 线 JSON（文本）→ 提取 OHLC 落盘 CSV。

tdx_kline 大输出自动持久化到 tool-results/*.txt（文本含 JSON）。本脚本：
1. 从文本中提取 Rows 数组（Data/Open/High/Low/Close/VolInStock/Volume）；
2. 输出标准化 CSV（date,open,high,low,close,oi,volume）；
3. 供主力拼接脚本消费。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


def extract_rows(fp: Path) -> pd.DataFrame:
    """从 tdx 持久化文本提取 K 线 DataFrame。"""
    text = fp.read_text(encoding="utf-8")
    # 定位 JSON 体（【】标题后）
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"{fp} 中未找到 JSON")
    data = json.loads(text[start : end + 1])
    rows = data.get("Rows") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["Data"], format="%Y%m%d")
    out = pd.DataFrame({
        "date": df["date"],
        "open": df["Open"].astype(float),
        "high": df["High"].astype(float),
        "low": df["Low"].astype(float),
        "close": df["Close"].astype(float),
        "oi": pd.to_numeric(df["VolInStock"], errors="coerce").fillna(0.0),
        "volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0),
    })
    return out.sort_values("date").drop_duplicates("date", keep="last")


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 tdx 持久化 K 线 JSON")
    ap.add_argument("input", type=Path, help="tdx_kline 持久化 txt 路径")
    ap.add_argument("output", type=Path, help="输出 CSV 路径")
    args = ap.parse_args()
    df = extract_rows(args.input)
    if df.empty:
        print(f"[WARN] {args.input.name}: 无 K 线数据")
        sys.exit(1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"[OK] {args.input.name}: {len(df)} 根 {df['date'].min().date()}~{df['date'].max().date()} "
          f"→ {args.output}")


if __name__ == "__main__":
    main()
