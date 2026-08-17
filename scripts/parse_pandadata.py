"""解析 PandaData MCP 持久化 JSON（dataframe 格式）→ CSV。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def parse_pandadata_file(fp: Path) -> pd.DataFrame:
    data = json.loads(fp.read_text(encoding="utf-8"))
    res = data.get("result") or {}
    if res.get("type") != "dataframe":
        raise ValueError(f"非 dataframe 结果: {res.get('type')}")
    df = pd.DataFrame(res["rows"], columns=res["columns"])
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 PandaData 持久化 dataframe JSON")
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    df = parse_pandadata_file(args.input)
    if df.empty:
        print(f"[WARN] {args.input.name}: 空数据")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"[OK] {df['symbol'].iloc[0] if 'symbol' in df else args.input.stem}: "
          f"{len(df)} 根 {df['date'].iloc[0]}~{df['date'].iloc[-1]} → {args.output}")


if __name__ == "__main__":
    main()
