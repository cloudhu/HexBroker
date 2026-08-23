"""特征选择裁剪消融：按重要性 top-K 裁剪冠军 v4 特征集。

基于 collect_importance_v4 输出的排名，构造 keep_features 白名单：
- 全量 25（基线，冠军 v4）
- top-10 / top-15 / top-20（按特征聚合重要性）
全部全 66 折 walk-forward，调优后 HP。若裁剪后 dir_acc 不降（>= -0.2pp）且
coverage 不恶化，则采纳（更少维度 = 更好泛化 + 更快训练）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from hexbroker.config import load_config  # noqa: E402
from hexbroker.feature import build_features  # noqa: E402
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, compute_realized_returns,
    walk_forward_lightgbm, records_to_gate, per_symbol_gate,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-16"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
IMPORTANCE_JSON = DELIVERABLE_DIR / f"lightgbm-feature-importance-v4-{REPORT_DATE}.json"
GLOBAL_CODES = ["spx", "uup"]

# 裁剪候选：top-K（特征数）
CUT_CANDIDATES = [10, 15, 20]


def load_ranking() -> list[str]:
    r = json.loads(IMPORTANCE_JSON.read_text(encoding="utf-8"))
    return [f["feature"] for f in r["ranked_features"]]


def build_cfg(keep: Optional[list[str]]) -> Any:
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    cfg.feature.keep_features = keep  # None=全量
    return cfg


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="特征选择裁剪消融")
    ap.add_argument("--n-jobs", type=int, default=int(__import__("os").cpu_count() or 1))
    args = ap.parse_args()

    print("=" * 72)
    print("特征选择裁剪消融（冠军 v4 25 特征 vs top-K 裁剪）")
    print("=" * 72)

    ranking = load_ranking()
    print(f"[OK] 重要性排名（{len(ranking)} 特征）: {ranking[:6]}...")

    cfg0 = build_cfg(None)
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | bars={bars.length}")
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))
    best_params = load_best_params()

    variants: list[tuple[str, Optional[list[str]]]] = [("full-25", None)]
    for k in CUT_CANDIDATES:
        variants.append((f"top-{k}", ranking[:k]))

    rows: list[dict] = []
    for label, keep in variants:
        print("-" * 72)
        print(f"[VARIANT] {label} (n_features={len(keep) if keep else 25})")
        cfg_v = build_cfg(keep)
        features = build_features(bars, cfg_v, global_close=gc)
        wf = walk_forward_lightgbm(
            cfg_v, bars, features, params=best_params,
            collect_models=False, splitter_overrides=None, n_jobs_folds=args.n_jobs,
        )
        gate = records_to_gate(wf.records, realized)
        per_sym = per_symbol_gate(wf.records, realized)
        row = {
            "variant": label,
            "n_features": len(features.df.columns),
            "dir_acc": gate["direction_accuracy"],
            "eff_acc": gate["effective_accuracy"],
            "coverage": gate["coverage"],
            "rank_ic": gate["rank_ic"],
            "n_signals": gate.get("n_signals", len(wf.records)),
        }
        per_sym_txt = " ".join(
            f"{s}:{per_sym[s]['direction_accuracy']*100:.2f}%" for s in per_sym
        )
        print(f"[GATE] dir_acc={row['dir_acc']*100:.2f}% eff_acc={row['eff_acc']*100:.2f}% "
              f"cov={row['coverage']*100:.2f}% rank_ic={row['rank_ic']:.4f} | {per_sym_txt}")
        rows.append(row)

    # 汇总
    base = rows[0]
    print("=" * 72)
    print("[SUMMARY] 与 full-25 对比（裁剪不降即采纳）")
    for r in rows[1:]:
        d_dir = (r["dir_acc"] - base["dir_acc"]) * 100
        d_ic = r["rank_ic"] - base["rank_ic"]
        d_cov = (r["coverage"] - base["coverage"]) * 100
        adopted = d_dir >= -0.2 and d_cov >= -1.0
        print(f"{r['variant']:<10s} n={r['n_features']:2d} dir={r['dir_acc']*100:.2f}% "
              f"(Δ{d_dir:+.2f}pp) ic={r['rank_ic']:.4f} (Δ{d_ic:+.4f}) "
              f"cov={r['coverage']*100:.2f}% (Δ{d_cov:+.2f}pp) -> {'✅采纳' if adopted else '❌放弃'}")
        r["dir_acc_delta_pp"] = round(d_dir, 2)
        r["rank_ic_delta"] = round(d_ic, 4)
        r["coverage_delta_pp"] = round(d_cov, 2)
        r["adopted"] = adopted

    report = {
        "title": "特征选择裁剪消融（冠军 v4 25 特征 vs top-K）",
        "report_date": REPORT_DATE,
        "ranking": ranking,
        "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
        "adopt_criteria": {"dir_acc_floor_pp": -0.2, "coverage_floor_pp": -1.0},
    }
    out = DELIVERABLE_DIR / f"lightgbm-feature-selection-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
