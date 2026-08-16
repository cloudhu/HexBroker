"""嵌套验证：量化 per-fold 校准的乐观偏差（冠军 v4 配置，全 66 折）。

方法学问题：现口径用本折测试窗标签拟合 Platt 校准器后，方向准确率又在
同一批测试标签上评估——等效「用测试标签挑每折决策阈值」，指标系统性偏乐观。

本脚本对比：
- cal_split=None（现口径，应复现 70.23%）
- cal_split=0.5 / 0.33 / 0.67（嵌套：只用测试窗前比例校准、剩余评估，评估与校准不重叠）

输出：各口径 dir_acc/RankIC/coverage/有效准确率 + 与现口径的偏差。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

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
GLOBAL_CODES = ["spx", "uup"]
# (label, cal_split)
VARIANTS = [
    ("现口径(全测试窗校准)", None),
    ("嵌套 0.33(前1/3校准)", 0.33),
    ("嵌套 0.50(前半校准)", 0.50),
    ("嵌套 0.67(前2/3校准)", 0.67),
]


def build_cfg():
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
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="嵌套验证量化校准乐观偏差")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print("嵌套验证：量化 per-fold 校准乐观偏差（冠军 v4 配置，全 66 折）")
    print("=" * 72)

    cfg0 = build_cfg()
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | bars={bars.length}")
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))
    best_params = load_best_params()

    rows: list[dict] = []
    for label, cal_split in VARIANTS:
        print("-" * 72)
        print(f"[VARIANT] {label} (cal_split={cal_split})")
        wf = walk_forward_lightgbm(
            cfg0, bars, features, params=best_params,
            collect_models=False, splitter_overrides=None,
            n_jobs_folds=args.n_jobs, cal_split=cal_split,
        )
        gate = records_to_gate(wf.records, realized)
        per_sym = per_symbol_gate(wf.records, realized)
        row = {
            "variant": label,
            "cal_split": cal_split,
            "dir_acc": gate["direction_accuracy"],
            "eff_acc": gate["effective_accuracy"],
            "coverage": gate["coverage"],
            "rank_ic": gate["rank_ic"],
            "n_signals": gate.get("n_signals", len(wf.records)),
        }
        per_sym_txt = " ".join(
            f"{s}:{per_sym[s]['direction_accuracy']*100:.2f}%" for s in per_sym
        )
        print(f"[GATE] dir={row['dir_acc']*100:.2f}% eff={row['eff_acc']*100:.2f}% "
              f"cov={row['coverage']*100:.2f}% ic={row['rank_ic']:.4f} n={row['n_signals']} | {per_sym_txt}")
        rows.append(row)

    print("=" * 72)
    print("[SUMMARY] 与现口径对比")
    base = rows[0]
    for r in rows:
        d_dir = (r["dir_acc"] - base["dir_acc"]) * 100
        d_ic = r["rank_ic"] - base["rank_ic"]
        d_cov = (r["coverage"] - base["coverage"]) * 100
        print(f"{r['variant']:<18s} dir={r['dir_acc']*100:.2f}% (Δ{d_dir:+.2f}pp) "
              f"ic={r['rank_ic']:.4f} (Δ{d_ic:+.4f}) cov={r['coverage']*100:.2f}% (Δ{d_cov:+.2f}pp) "
              f"n={r['n_signals']}")

    # 乐观偏差 = 现口径 - 嵌套 0.50（主要对照）
    nested = next(r for r in rows if r["cal_split"] == 0.5)
    bias = base["dir_acc"] - nested["dir_acc"]
    print("=" * 72)
    print(f"[结论] 现口径 vs 嵌套 0.50 的方向准确率偏差：{bias*100:+.2f}pp "
          f"（正值=现口径偏乐观；偏乐观幅度 {bias/base['dir_acc']*100:+.1f}% 相对值）")

    report = {
        "title": "嵌套验证：per-fold 校准乐观偏差量化",
        "report_date": REPORT_DATE,
        "symbols": SYMBOLS,
        "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
        "optimistic_bias_pp": round(bias * 100, 2),
        "conclusion": (
            "嵌套 0.50 与现口径之差即校准乐观偏差幅度；"
            "若嵌套口径仍显著高于 0.5 随机线则模型 alpha 真实存在"
        ),
    }
    out = DELIVERABLE_DIR / f"lightgbm-calibration-leakage-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
