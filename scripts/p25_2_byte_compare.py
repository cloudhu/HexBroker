#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P25-2 字节级对比：v13 vs v8（同口径重建是否逐字节一致）。

P21 教训：v12==v8 逐字节（fold 截断结构性）。本脚本对 v13 与 v8 做同样的
逐列 byte 比较，输出到 artifacts/p25_byte_compare.txt。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V13_PATH = ART / "signals_cache18_grouped_v13.parquet"
OUT = ART / "p25_byte_compare.txt"


def main() -> None:
    if not V13_PATH.exists():
        print(f"[FAIL] {V13_PATH} 不存在（先运行 scripts/p25_cache_rebuild.py）")
        raise SystemExit(1)
    v8 = pd.read_parquet(V8_PATH)
    v13 = pd.read_parquet(V13_PATH)

    lines = []
    lines.append("=" * 72)
    lines.append("P25-2 字节级对比 v13 vs v8（同口径重建）")
    lines.append("=" * 72)
    lines.append(f"v8 rows: {len(v8)} | v13 rows: {len(v13)}")
    lines.append(f"v8 ts_end: {pd.to_datetime(v8['ts']).max().date()} | "
                 f"v13 ts_end: {pd.to_datetime(v13['ts']).max().date()}")

    m8 = v8.set_index(["symbol", "ts"]).sort_index()
    m13 = v13.set_index(["symbol", "ts"]).sort_index()

    if m8.index.equals(m13.index):
        lines.append("INDEX: identical (symbol, ts)")
    else:
        only_v8 = m8.index.difference(m13.index)
        only_v13 = m13.index.difference(m8.index)
        lines.append(f"INDEX: DIFF (only_v8={len(only_v8)} only_v13={len(only_v13)})")
        if len(only_v8):
            lines.append(f"  only_v8 sample: {list(only_v8[:5])}")
        if len(only_v13):
            lines.append(f"  only_v13 sample: {list(only_v13[:5])}")

    cols = ["p_up", "exp_ret", "is_effective"]
    byte_identical = True
    for col in cols:
        if col in m8.columns and col in m13.columns:
            common = m8.index.intersection(m13.index)
            a = m8.loc[common, col].astype(float)
            b = m13.loc[common, col].astype(float)
            md = float((a - b).abs().max()) if len(common) else float("nan")
            same = bool((a == b).all()) if len(common) else False
            lines.append(f"{col}: shared={len(common)} max_abs_diff={md:.3e} identical={same}")
            if not same:
                byte_identical = False
        else:
            lines.append(f"{col}: 缺失列 v8={col in m8.columns} v13={col in m13.columns}")
            byte_identical = False

    lines.append("")
    lines.append(f"BYTE_IDENTICAL: {'YES' if byte_identical else 'NO'}")
    text = "\n".join(lines)
    print(text)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"\n[OK] → {OUT}")


if __name__ == "__main__":
    main()
