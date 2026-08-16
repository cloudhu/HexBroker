"""数据源对比/校验：本地免费源（pytdx/sina）vs MCP 基准（data/raw/processed 已验证数据）。

指标：共同交易日、缺失交易日、OHLC 对齐率（相对误差 <0.1%）、close 相对误差、
volume 相关性、换月/跳空差异。输出 markdown 报告。
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.data.sources.pytdx_source import PytdxSource  # noqa: E402
from hexbroker.data.sources.sina_source import SinaSource  # noqa: E402

PROCESSED_DIR = _ROOT / "data" / "raw" / "processed"
TARGETS = ["au0", "ag0", "m0"]  # 研究基准标的（沪金/沪银/豆粕）
SOURCE_MAP = {
    "au0": "SHFE.au",
    "ag0": "SHFE.ag",
    "m0": "DCE.m",
}
OUT_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
REPORT_DATE = "2026-08-16"


def load_baseline(sym: str, start: str, end: str) -> pd.DataFrame:
    """读 MCP 基准 parquet（按年分片合并），并按窗口裁剪。"""
    parts = []
    for fp in sorted(glob.glob(str(PROCESSED_DIR / sym / "1d" / "*.parquet"))):
        parts.append(pd.read_parquet(fp))
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    # 去重（跨年分片可能重叠边界）
    df = df[~df.index.duplicated(keep="last")]
    return df.loc[str(start) : str(end)]


def fetch_source(source_name: str, cfg_sym: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        if source_name == "pytdx":
            src = PytdxSource()
        elif source_name == "sina":
            src = SinaSource()
        else:
            return None
        bars = src.fetch_bars([cfg_sym], start=start, end=end, freq="1d")
        df = bars.df.copy()
        df = df.reset_index()
        if "datetime" not in df.columns:
            df = df.rename(columns={"date": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] {source_name} 拉取失败: {str(exc)[:100]}")
        return None


def compare_one(sym: str, base: pd.DataFrame, src_df: pd.DataFrame | None, source_name: str) -> dict:
    """对比基准与单源的 OHLCV 一致性。"""
    if src_df is None or src_df.empty:
        return {"symbol": sym, "source": source_name, "error": "源无数据"}

    common = base.index.intersection(src_df.index)
    missing = base.index.difference(src_df.index)
    extra = src_df.index.difference(base.index)
    row = {
        "symbol": sym,
        "source": source_name,
        "n_base": len(base),
        "n_src": len(src_df),
        "n_common": len(common),
        "n_missing": len(missing),
        "n_extra": len(extra),
        "error": "",
    }
    if len(common) < 10:
        row["error"] = f"共同交易日过少({len(common)})"
        return row

    # close 相对误差
    bc = base.loc[common, "close"].astype(float)
    sc = src_df.loc[common, "close"].astype(float)
    rel_err = (sc / bc - 1.0).abs()
    row["close_rel_err_mean"] = float(rel_err.mean())
    row["close_rel_err_max"] = float(rel_err.max())
    row["close_aligned_pct"] = float((rel_err < 1e-3).mean())  # 0.1% 内对齐率

    # OHLC 四价对齐率（0.1% 内）
    aligned = []
    for col in ("open", "high", "low"):
        if col in base.columns and col in src_df.columns:
            r = (src_df.loc[common, col].astype(float) / base.loc[common, col].astype(float) - 1.0).abs()
            aligned.append(float((r < 1e-3).mean()))
    row["ohlc_aligned_pct"] = float(np.mean(aligned)) if aligned else float("nan")

    # volume 相关性
    if "volume" in base.columns and "volume" in src_df.columns:
        bv = base.loc[common, "volume"].astype(float)
        sv = src_df.loc[common, "volume"].astype(float)
        if bv.std() > 0 and sv.std() > 0:
            row["volume_corr"] = float(np.corrcoef(bv, sv)[0, 1])
        else:
            row["volume_corr"] = float("nan")

    # 缺失交易日样例（前 5 个）
    row["missing_sample"] = [str(d.date()) for d in missing[:5]]
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="数据源对比（pytdx/sina vs MCP 基准）")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--symbols", default=",".join(TARGETS))
    args = ap.parse_args()

    print("=" * 72)
    print(f"数据源对比：MCP 基准(data/raw/processed) vs pytdx/sina（{args.start}~{args.end}）")
    print("=" * 72)

    rows: list[dict] = []
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        base = load_baseline(sym, args.start, args.end)
        print(f"\n[{sym}] 基准 {len(base)} 根（{base.index.min().date()}~{base.index.max().date()}）")
        for src_name in ("pytdx", "sina"):
            print(f"  拉取 {src_name} ...")
            cfg_sym = SOURCE_MAP.get(sym, sym)
            src_df = fetch_source(src_name, cfg_sym, args.start, args.end)
            r = compare_one(sym, base, src_df, src_name)
            rows.append(r)
            if r.get("error"):
                print(f"  [结果] {src_name}: {r['error']}")
            else:
                print(f"  [结果] {src_name}: 共同 {r['n_common']} 缺失 {r['n_missing']} "
                      f"close对齐 {r['close_aligned_pct']*100:.2f}% 最大误差 {r['close_rel_err_max']*100:.3f}% "
                      f"vol_corr {r.get('volume_corr', float('nan')):.4f}")

    # 汇总表
    print("\n" + "=" * 72)
    print("[SUMMARY]")
    for r in rows:
        if r.get("error"):
            print(f"  {r['symbol']:5s} {r['source']:6s} ERROR: {r['error']}")
            continue
        print(f"  {r['symbol']:5s} {r['source']:6s} 共同={r['n_common']:4d} 缺失={r['n_missing']:3d} "
              f"close对齐={r['close_aligned_pct']*100:6.2f}% 最大err={r['close_rel_err_max']*100:.3f}% "
              f"vol_corr={r.get('volume_corr', float('nan')):.4f}")

    out = OUT_DIR / f"data-source-compare-{REPORT_DATE}.md"
    lines = ["# 数据源对比报告（MCP 基准 vs pytdx/sina）", "",
             f"- 区间：{args.start} ~ {args.end}", f"- 基准：data/raw/processed（MCP 已验证）", ""]
    lines += ["| 标的 | 源 | 基准根数 | 源根数 | 共同 | 缺失 | close对齐(<0.1%) | 最大相对误差 | volume相关 | 判定 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['symbol']} | {r['source']} | {r.get('n_base','-')} | {r.get('n_src','-')} | - | - | - | - | - | ❌ {r['error']} |")
        else:
            ok = r["close_aligned_pct"] > 0.99 and r["n_missing"] <= 5
            lines.append(f"| {r['symbol']} | {r['source']} | {r['n_base']} | {r['n_src']} | {r['n_common']} | {r['n_missing']} "
                         f"| {r['close_aligned_pct']*100:.2f}% | {r['close_rel_err_max']*100:.3f}% "
                         f"| {r.get('volume_corr', float('nan')):.4f} | {'✅' if ok else '⚠️'} |")
    lines += ["", "判定标准：close 对齐率>99% 且缺失≤5 个交易日 = ✅"]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[OK] 报告：{out}")


if __name__ == "__main__":
    main()
