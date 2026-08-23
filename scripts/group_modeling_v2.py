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
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.group_modeling import build_group_signals

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


def aggregate_importance(importances: list, feat_names: list) -> dict:
    """聚合每折扁平化 feature_importances_ → {特征: 相对重要性占比}（降序）。

    importances : list[(sym, fold_idx, np.ndarray)]；数组为
      ``build_windows`` 行优先 reshape 的 ``lookback * n_feat`` 扁平化重要性。
    """
    if not importances or not feat_names:
        return {"n_folds": 0, "n_features": len(feat_names), "ranked": []}
    n_feat = len(feat_names)
    lookback = max(1, int(importances[0][2].shape[0]) // n_feat)
    per_feature = np.zeros(n_feat, dtype=float)
    n_folds = 0
    for _sym, _fi, imp in importances:
        if imp.shape[0] != lookback * n_feat:
            continue
        n_folds += 1
        for j, v in enumerate(imp):
            per_feature[j % n_feat] += float(v)
    total = per_feature.sum()
    ranked = sorted(
        zip(feat_names, (per_feature / total if total > 0 else np.zeros(n_feat))),
        key=lambda kv: -float(kv[1]),
    )
    return {
        "n_folds": n_folds,
        "n_features": n_feat,
        "lookback": lookback,
        "ranked": [{"feature": f, "importance": float(v)} for f, v in ranked],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔）")
    ap.add_argument("--cal-return-all", action="store_true",
                    help="校准后返回全部信号（用前 50% 拟合校准器、评估全部，覆盖率翻倍）")
    ap.add_argument("--label-mode", type=str, default="absolute",
                    choices=["absolute", "cross_rank", "cross_z", "cross_demean"],
                    help="训练标签口径（P8-3）：absolute=回归绝对 fwd 收益（默认，现行为）；"
                         "cross_rank/cross_z/cross_demean=当日截面化标签（按组内品种当日分组、无前视）")
    ap.add_argument("--label-pool", type=str, default="group", choices=["group", "all"],
                    help="截面化范围（P8-4）：group=组内截面（默认，P8-3 行为）；"
                         "all=全 18 品种统一截面（单品种组 m0 也参与全品种当日截面，根治组规模偏置）")
    ap.add_argument("--output", type=str, default="artifacts/signals_cache18_grouped_v2.parquet",
                    help="输出 parquet 路径（默认 v2 路径，向后兼容；重建 v5 传 v5 路径）")
    ap.add_argument("--collect-importance", action="store_true",
                    help="收集每折特征重要性（P8-2 验证基差特征是否被模型使用）")
    ap.add_argument("--importance-output", type=str,
                    default="artifacts/p8_basis_importance.json",
                    help="特征重要性聚合结果输出路径")
    args = ap.parse_args()

    print("=" * 72)
    print("P1 组内细分建模（v2 分组 + P8-2 基差特征）")
    print(f"cal_return_all={args.cal_return_all} collect_importance={args.collect_importance} "
          f"label_mode={args.label_mode} label_pool={args.label_pool} output={args.output}")
    print("=" * 72)
    only = set(args.only.split(",")) if args.only else None

    frames = []
    group_importance: dict[str, dict] = {}  # gname -> {"feat_names": [...], "importances": [...]}
    for gname, gcfg in GROUPS_V2.items():
        if only is not None and gname not in only:
            continue
        res = build_group_signals(gcfg["syms"], gcfg["global"], args.n_jobs,
                                  cal_return_all=args.cal_return_all,
                                  collect_models=args.collect_importance,
                                  label_mode=args.label_mode,
                                  label_pool=args.label_pool)
        if args.collect_importance:
            sig, wf = res
            if wf.importances:
                group_importance[gname] = {
                    "feat_names": list(wf.feat_names or []),
                    "importances": wf.importances,
                }
        else:
            sig = res
        frames.append(sig)
    if not frames:
        print("[FAIL] 无信号产出")
        return
    all_sig = pd.concat(frames, ignore_index=True)
    Path("artifacts").mkdir(exist_ok=True)
    all_sig.to_parquet(args.output, index=False)
    print(f"[OK] v2 分组信号合并 {len(all_sig)} 条 → {args.output}")
    print(f"  品种覆盖: {sorted(all_sig['symbol'].unique())}")

    if args.collect_importance:
        # 各组 global_codes 不同 → 跨品种特征列不同，不能跨组合并重要性；
        # 按组聚合（同组内品种共享同一特征列集合），再汇总基差特征排名。
        per_group = {
            gname: aggregate_importance(gd["importances"], gd["feat_names"])
            for gname, gd in group_importance.items()
        }
        basis_rows = []
        for gname, agg in per_group.items():
            for i, row in enumerate(agg["ranked"], 1):
                if row["feature"].startswith("f_basis"):
                    basis_rows.append({
                        "group": gname,
                        "feature": row["feature"],
                        "rank": i,
                        "importance": row["importance"],
                        "n_features": agg["n_features"],
                        "n_folds": agg["n_folds"],
                    })
        out = {
            "n_groups": len(per_group),
            "per_group": per_group,
            "basis_summary": basis_rows,
        }
        Path(args.importance_output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.importance_output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[OK] 特征重要性（{len(per_group)} 组）→ {args.importance_output}")
        for gname, agg in per_group.items():
            print(f"  [{gname}] 折数={agg['n_folds']} 特征数={agg['n_features']}")
            for i, row in enumerate(agg["ranked"], 1):
                mark = " ◀ basis" if row["feature"].startswith("f_basis") else ""
                print(f"      {i:>2}. {row['feature']:<32} {row['importance']*100:6.2f}%{mark}")
        print("  [basis_summary] 基差特征在各组的排名（验证模型确实用上基差）")
        for r in sorted(basis_rows, key=lambda x: (x["group"], x["feature"])):
            print(f"      {r['group']:<16} {r['feature']:<24} rank={r['rank']:>2}/{r['n_features']} "
                  f"imp={r['importance']*100:5.2f}%")


if __name__ == "__main__":
    main()
