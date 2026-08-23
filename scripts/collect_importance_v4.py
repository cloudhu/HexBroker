"""收集冠军 v4 特征重要性（全 66 折，作为特征选择裁剪依据）。

复用 refine 脚本的 walk_forward_lightgbm + aggregate_importances；
特征配置 = 冠军 v4（base 18 + f_range_pos_20 + SPX 组 + UUP 组 = 25 特征）。
输出：deliverables/.../lightgbm-feature-importance-v4-YYYY-MM-DD.{md,json}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config
from hexbroker.feature import build_features
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, compute_realized_returns,
    walk_forward_lightgbm, records_to_gate, aggregate_importances,
)
from scripts.ablate_features import (
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-16"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]


def build_v4_cfg():
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
    return cfg


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="冠军 v4 特征重要性收集")
    ap.add_argument("--n-jobs", type=int, default=int(__import__("os").cpu_count() or 1))
    args = ap.parse_args()

    print("=" * 72)
    print("冠军 v4 特征重要性（全 66 折，25 特征）")
    print("=" * 72)

    cfg = build_v4_cfg()
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | bars={bars.length}")

    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg, global_close=gc)
    print(f"[OK] 特征：n_cols={len(features.df.columns)} n_rows={features.length}")
    realized = compute_realized_returns(bars, int(cfg.forecast.horizon))

    best_params = load_best_params()
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=best_params,
        collect_models=True, splitter_overrides=None, n_jobs_folds=args.n_jobs,
    )
    gate = records_to_gate(wf.records, realized)
    print(f"[GATE] dir_acc={gate['direction_accuracy']*100:.2f}% "
          f"rank_ic={gate['rank_ic']:.4f} cov={gate['coverage']*100:.2f}%")

    imp = aggregate_importances(wf)
    ranked = imp.get("ranked_features", [])
    print(f"[IMPORT] n_features={imp.get('n_features')} n_folds={imp.get('n_folds')}")
    for r in ranked[:10]:
        print(f"  {r['feature']:<20s} {r['importance']*100:5.2f}%")
    print("  ...")
    for r in ranked[-5:]:
        print(f"  {r['feature']:<20s} {r['importance']*100:5.2f}%")

    # 落盘
    DELIVERABLE_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "title": "冠军 v4 特征重要性（特征选择裁剪依据）",
        "report_date": REPORT_DATE,
        "symbols": SYMBOLS,
        "n_features": imp.get("n_features"),
        "n_folds": imp.get("n_folds"),
        "lookback": imp.get("lookback"),
        "gate": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in gate.items()},
        "ranked_features": ranked,
        "top_feature_lag": imp.get("top_feature_lag"),
    }
    out = DELIVERABLE_DIR / f"lightgbm-feature-importance-v4-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 落盘：{out}")


if __name__ == "__main__":
    main()
