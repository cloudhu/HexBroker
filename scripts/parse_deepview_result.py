"""解析 PandaData DeepView 持久化响应（result[] 数组格式）→ 按品种落盘 parquet。

适用方法：get_future_basis / get_future_warehouse_receipt
（响应为 {"ok":true,"method":...,"result":[{...underlying_symbol...}, ...]} 的数组格式，
 区别于 parse_pandadata.py 处理的 dataframe 类型）。

用法：
  python scripts/parse_deepview_result.py <持久化文件> --metric basis --seg 2018_2022
  python scripts/parse_deepview_result.py --merge   # 合并两段 → {metric}_{sym}.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

OUT_DIR = Path("data/raw/fundamental")

# 段标签 → 文件名后缀（与 fetch_deepview_data.py 的 SEGMENTS 对应）
SEG_LABEL = {"20180101_20220101": "2018_2022", "20220101_20260817": "2022_2026"}


def parse_result_array(fp: Path) -> pd.DataFrame:
    data = json.loads(fp.read_text(encoding="utf-8"))
    res = data.get("result")
    if not isinstance(res, list):
        raise ValueError(f"{fp.name}: result 非数组（type={data.get('result', {}).get('type') if isinstance(data.get('result'), dict) else type(res)}）")
    df = pd.DataFrame(res)
    if df.empty:
        raise ValueError(f"{fp.name}: 空 result")
    if "underlying_symbol" not in df.columns:
        raise ValueError(f"{fp.name}: 缺 underlying_symbol 列，实际列={list(df.columns)}")
    return df


def parse_and_save(fp: Path, metric: str, seg: str) -> None:
    df = parse_result_array(fp)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    syms = sorted(df["underlying_symbol"].unique())
    total = 0
    for sym in syms:
        sub = df[df["underlying_symbol"] == sym].drop(columns=["underlying_symbol"])
        sub = sub.drop_duplicates(subset="date").sort_values("date")
        out = OUT_DIR / f"{metric}_{sym}_{seg}.parquet"
        sub.to_parquet(out, index=False)
        total += len(sub)
    print(f"[OK] {metric} {seg}: {len(syms)} 品种 {total} 行 "
          f"{df['date'].min().date()}~{df['date'].max().date()} → {OUT_DIR}")


def merge_segments(metric: str, sym: str) -> None:
    files = sorted(OUT_DIR.glob(f"{metric}_{sym}_*.parquet"))
    if not files:
        return
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    out = OUT_DIR / f"{metric}_{sym}.parquet"
    df.to_parquet(out, index=False)
    print(f"[MERGED] {sym} {metric}: {len(df)} 行 {df['date'].min().date()}~{df['date'].max().date()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="解析 DeepView result[] 持久化 → 分品种 parquet")
    ap.add_argument("input", type=Path, nargs="?", help="持久化 JSON 文件")
    ap.add_argument("--metric", choices=["basis", "warehouse"], required="--merge" not in sys.argv and "--merge" not in sys.argv)
    ap.add_argument("--seg", choices=["2018_2022", "2022_2026"], required="--merge" not in sys.argv and "--merge" not in sys.argv)
    ap.add_argument("--merge", action="store_true", help="合并全部已落盘分段")
    args = ap.parse_args()

    if args.merge:
        # 分段文件名形如 basis_AG_2018_2022.parquet（尾部为段标签）；
        # 已合并文件 basis_AG.parquet 无段标签后缀，需跳过。
        seg_suffixes = ("2018_2022", "2022_2026")
        for f in sorted(OUT_DIR.glob("*_*.parquet")):
            stem = f.stem
            seg = next((s for s in seg_suffixes if stem.endswith(s)), None)
            if seg is None:
                continue
            metric, sym = stem[: -len(seg) - 1].rsplit("_", 1)
            merge_segments(metric, sym)
        print("[OK] 合并完成")
        return

    if args.input is None or args.metric is None or args.seg is None:
        ap.error("input/--metric/--seg 必填（或使用 --merge）")
    if not args.input.exists():
        print(f"[FAIL] {args.input} 不存在")
        sys.exit(1)
    parse_and_save(args.input, args.metric, args.seg)


if __name__ == "__main__":
    main()
