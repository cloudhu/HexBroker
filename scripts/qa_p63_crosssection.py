"""QA 复核 P6-3：v2 日期上的 v3 截面差异分析（修正版）。"""
from __future__ import annotations

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

v2 = pd.read_parquet("artifacts/signals_cache18_grouped_v2.parquet")
v3 = pd.read_parquet("artifacts/signals_cache18_grouped_v3.parquet")
v2["ts"] = pd.to_datetime(v2["ts"]); v3["ts"] = pd.to_datetime(v3["ts"])

d2 = set(v2["ts"].dt.date.unique())
print("v2 日期数:", len(d2))

# v3 落在 v2 日期上的所有行
v3_on_v2 = v3[v3["ts"].dt.date.isin(d2)]
print("v3 落在 v2 日期的行数:", len(v3_on_v2), "（v2 自身 7952）")

# 按日比较 symbol 集合
g2 = v2.groupby(v2["ts"].dt.date)["symbol"].apply(set)
g3 = v3_on_v2.groupby(v3_on_v2["ts"].dt.date)["symbol"].apply(set)
days = sorted(d2)
bigger = sum(len(g3[d]) > len(g2[d]) for d in days)
equal = sum(len(g3[d]) == len(g2[d]) for d in days)
smaller = sum(len(g3[d]) < len(g2[d]) for d in days)
print(f"v2 日期上 v3 品种数 > v2: {bigger} 天, ==: {equal} 天, <: {smaller} 天")

n2 = v2.groupby(v2["ts"].dt.date)["symbol"].count()
n3 = v3_on_v2.groupby(v3_on_v2["ts"].dt.date)["symbol"].count()
print(f"v2 日期日均品种: v2={n2.mean():.2f}  v3_on_v2={n3.mean():.2f}")
print(f"v2 日期 <3 品种天数: v2={100*(n2<3).mean():.1f}%  v3_on_v2={100*(n3<3).mean():.1f}%")
print(f"v2 日期 <5 品种天数: v2={100*(n2<5).mean():.1f}%  v3_on_v2={100*(n3<5).mean():.1f}%")

# 新增 symbol 行（v2 日期上 v3 有而 v2 无的 symbol）
v2k = set(zip(v2["symbol"], v2["ts"].dt.date))
v3k = set(zip(v3_on_v2["symbol"], v3_on_v2["ts"].dt.date))
added = v3k - v2k
print("v2 日期上 v3 新增 (symbol,day) 行:", len(added), "（v2 日期上原 7952）")

# 在 v2 日期上，v3 新增 symbol 的 exp_ret 分布 vs 原有
mk3 = v3_on_v2.set_index(["symbol", v3_on_v2["ts"].dt.date])
mk2 = v2.set_index(["symbol", v2["ts"].dt.date])
added_df = mk3.loc[sorted(added)]
orig_df = mk2
print("\n[v2 日期上] 原有 symbol exp_ret: mean=%.4f std=%.4f n=%d" % (
    orig_df["exp_ret"].mean(), orig_df["exp_ret"].std(), len(orig_df)))
print("[v2 日期上] 新增 symbol exp_ret: mean=%.4f std=%.4f n=%d" % (
    added_df["exp_ret"].mean(), added_df["exp_ret"].std(), len(added_df)))

# rank 相关性：同一 (day) 上 v2 rank 与 v3 rank（公共 symbol 的 rank_pct 变化）
for label, df in (("v2", v2), ("v3", v3_on_v2)):
    d = df.copy()
    d["rank_pct"] = d.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    d["_d"] = d["ts"].dt.date
    if label == "v2":
        v2r = d.set_index(["symbol", "_d"])["rank_pct"]
    else:
        v3r = d.set_index(["symbol", "_d"])["rank_pct"]
common_idx = v2r.index.intersection(v3r.index)
r = np.corrcoef(v2r.loc[common_idx].values, v3r.loc[common_idx].values)[0, 1]
print(f"\n公共 (symbol,day) 上 v2 vs v3 rank_pct 相关性: {r:.4f} (n={len(common_idx)})")
delt = (v3r.loc[common_idx] - v2r.loc[common_idx]).abs()
print(f"公共行 rank_pct 绝对变化: mean={delt.mean():.4f} max={delt.max():.4f} >0.1占比={100*(delt>0.1).mean():.1f}%")

# 覆盖率机制：单品种覆盖 vs 全局
all_days = v3["ts"].dt.date.unique()
tr = pd.bdate_range(v3["ts"].min(), v3["ts"].max())
print(f"\n区间工作日 {len(tr)} | v3 覆盖 {len(all_days)} ({100*len(all_days)/len(tr):.1f}%)")
per_sym = v3.groupby("symbol")["ts"].apply(lambda s: s.dt.date.nunique())
print(f"单品种覆盖天数: mean={per_sym.mean():.0f} min={per_sym.min()} max={per_sym.max()} "
      f"→ 平均覆盖 {100*per_sym.mean()/len(tr):.0f}%")
