"""多模型集成对比：LightGBM 单模型 vs Ensemble（LGB+XGB）vs Ensemble3（+CatBoost，可选）。

全 66 折 walk-forward，冠军 v4 特征集（25 特征），per-fold Platt 校准。
对比 dir_acc / RankIC / coverage / eff_acc。
用法：
  python scripts/compare_ensemble.py --n-jobs 12 [--with-catboost]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config
from hexbroker.feature import build_features
from hexbroker.forecast.baselines import EnsembleForecast, LightGBMForecast
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
GLOBAL_CODES = ["spx", "uup"]


def build_cfg(ensemble_members: str | None):
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    if ensemble_members:
        cfg.forecast.ensemble_members = ensemble_members
    return cfg


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="多模型集成对比")
    ap.add_argument("--n-jobs", type=int, default=12)
    ap.add_argument("--with-catboost", action="store_true",
                    help="尝试三成员集成（CatBoost 在本环境 var 拟合可能原生崩溃，谨慎）")
    args = ap.parse_args()

    print("=" * 72)
    print("多模型集成对比（冠军 v4 特征集，全 66 折）")
    print("=" * 72)

    cfg0 = build_cfg(None)
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | bars={bars.length}")
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))
    best_params = load_best_params()

    variants: list[tuple[str, Any, str | None]] = [
        ("lightgbm", LightGBMForecast, None),
        ("ensemble-lgbxgb", EnsembleForecast, "lgb,xgb"),
    ]
    if args.with_catboost:
        variants.append(("ensemble-3", EnsembleForecast, "lgb,xgb,cat"))

    rows: list[dict] = []
    for label, model_cls, members in variants:
        print("-" * 72)
        print(f"[VARIANT] {label}")
        cfg_v = build_cfg(members)
        wf = walk_forward_lightgbm(
            cfg_v, bars, features, params=best_params,
            collect_models=False, splitter_overrides=None,
            n_jobs_folds=args.n_jobs, model_cls=model_cls,
        )
        gate = records_to_gate(wf.records, realized)
        per_sym = per_symbol_gate(wf.records, realized)
        row = {
            "variant": label,
            "model": model_cls.__name__,
            "members": members or "single",
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

    print("=" * 72)
    base = rows[0]
    for r in rows[1:]:
        d_dir = (r["dir_acc"] - base["dir_acc"]) * 100
        d_ic = r["rank_ic"] - base["rank_ic"]
        d_cov = (r["coverage"] - base["coverage"]) * 100
        print(f"{r['variant']:<18s} dir={r['dir_acc']*100:.2f}% (Δ{d_dir:+.2f}pp) "
              f"ic={r['rank_ic']:.4f} (Δ{d_ic:+.4f}) cov={r['coverage']*100:.2f}% (Δ{d_cov:+.2f}pp)")

    report = {
        "title": "多模型集成对比（LightGBM vs Ensemble）",
        "report_date": REPORT_DATE,
        "symbols": SYMBOLS,
        "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
    }
    out = DELIVERABLE_DIR / f"lightgbm-ensemble-compare-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
