"""P0 基本面数据质量校验：K线对齐率 / 异常值 / 覆盖度 / 分段拼接检查。

用法：
  python scripts/check_fundamental_quality.py
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import numpy as np

FUND = Path("data/raw/fundamental")
KLINE = Path("data/raw/processed")

SYMS = ["ag", "al", "au", "cf", "cu", "hc", "i", "j", "jm", "m", "ni", "p", "rb", "sc", "sr", "ta", "y", "zn"]


def load_kline_dates(sym: str) -> pd.DatetimeIndex:
    """加载主力连续日线日期（拼接年度 parquet）。"""
    frames = []
    d = KLINE / f"{sym}0" / "1d"
    if not d.exists():
        return pd.DatetimeIndex([])
    for f in sorted(d.glob("*.parquet")):
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue
    if not frames:
        return pd.DatetimeIndex([])
    kdf = pd.concat(frames, ignore_index=True)
    col = "datetime" if "datetime" in kdf.columns else ("date" if "date" in kdf.columns else "ts")
    return pd.to_datetime(kdf[col]).drop_duplicates()


def main() -> None:
    rows = []
    for sym in SYMS:
        kdates = load_kline_dates(sym)
        for metric in ("basis", "warehouse"):
            f = FUND / f"{metric}_{sym.upper()}.parquet"
            if not f.exists():
                rows.append((sym, metric, 0, 0, 0, 0, 0, "缺文件"))
                continue
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            n = len(df)
            if n == 0:
                rows.append((sym, metric, 0, 0, 0, 0, 0, "空"))
                continue
            overlap = df["date"].isin(kdates).sum() if len(kdates) else 0
            align = overlap / min(n, len(kdates)) * 100 if min(n, len(kdates)) else 0
            # 分段拼接连续性：检查最大日期间隙
            gaps = df["date"].sort_values().diff().dt.days
            max_gap = int(gaps.max()) if len(gaps) else 0
            # 异常值
            if metric == "basis":
                br = df["basis_ratio"].dropna()
                extreme = (br.abs() > 30).sum() if len(br) else 0
                z = (br - br.mean()) / br.std() if len(br) > 1 else pd.Series(dtype=float)
                zext = (z.abs() > 5).sum() if len(z) else 0
                note = f"ratio极端>30:{extreme} z>5:{zext} ratio范围[{br.min():.1f},{br.max():.1f}]"
            else:
                wq = df["wr_quantity"].dropna()
                neg = (wq < 0).sum() if len(wq) else 0
                note = f"仓单负值:{neg} 范围[{wq.min():.0f},{wq.max():.0f}]"
            rows.append((sym, metric, n, overlap, align, max_gap, len(kdates), note))

    out = pd.DataFrame(rows, columns=["sym", "metric", "rows", "overlap", "align%", "max_gap_d", "kline_days", "note"])
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 60)
    print(out.to_string(index=False))
    print()
    print(f"== 汇总 ==")
    print(out.groupby("metric")["rows"].sum())
    print(f"平均对齐率: {out[out['align%']>0]['align%'].mean():.1f}%")


if __name__ == "__main__":
    main()
