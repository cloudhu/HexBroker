"""P1 组内细分：农产品/黑色组细分独立训练（修复 p0 恶化）。

v1 问题：agri(m/y/p/sr/cf) 同组训练 → p0 被 m/y 强信号压制（IC -0.14）。
v2 细分：
  agri_oil      油脂    y/p              global=['spx']
  agri_protein  蛋白粕  m                global=['spx']   （单品种=纯时序）
  agri_soft     软商品  sr/cf            global=['spx']
  ferrous_steel 钢材    rb/hc            global=['spx','t10y']
  ferrous_raw   原料    i/j/jm           global=['spx','t10y']
  precious      贵金属  au/ag            global=['spx','uup','t10y']
  industrial    有色    cu/al/zn/ni      global=['spx','uup']
  chem_energy   化工能源 ta/sc           global=['spx']
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from scripts.group_modeling import LOCAL_MAP, build_group_signals

GROUPS_V2 = {
    "precious": {"syms": ["au0", "ag0"], "global": ["spx", "uup", "t10y"]},
    "ferrous_steel": {"syms": ["rb0", "hc0"], "global": ["spx", "t10y"]},
    "ferrous_raw": {"syms": ["i0", "j0", "jm0"], "global": ["spx", "t10y"]},
    "industrial": {"syms": ["cu0", "al0", "zn0", "ni0"], "global": ["spx", "uup"]},
    "agri_oil": {"syms": ["y0", "p0"], "global": ["spx"]},
    "agri_protein": {"syms": ["m0"], "global": ["spx"]},
    "agri_soft": {"syms": ["sr0", "cf0"], "global": ["spx"]},
    "chem_energy": {"syms": ["ta0", "sc0"], "global": ["spx"]},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔）")
    args = ap.parse_args()

    print("=" * 72)
    print("P1 组内细分建模（v2 分组）")
    print("=" * 72)
    only = set(args.only.split(",")) if args.only else None

    frames = []
    for gname, gcfg in GROUPS_V2.items():
        if only is not None and gname not in only:
            continue
        sig = build_group_signals(gcfg["syms"], gcfg["global"], args.n_jobs)
        frames.append(sig)
    if not frames:
        print("[FAIL] 无信号产出")
        return
    all_sig = pd.concat(frames, ignore_index=True)
    Path("artifacts").mkdir(exist_ok=True)
    all_sig.to_parquet("artifacts/signals_cache18_grouped_v2.parquet", index=False)
    print(f"[OK] v2 分组信号合并 {len(all_sig)} 条 → artifacts/signals_cache18_grouped_v2.parquet")
    print(f"  品种覆盖: {sorted(all_sig['symbol'].unique())}")


if __name__ == "__main__":
    main()
