"""概率校准对比：Platt vs Isotonic vs 无校准（冠军 v4 配置，全 66 折）。

walk_forward_lightgbm 在每折测试窗做 per-fold 校准（valid>=20），
对比不同校准方法对 gate1 指标（方向准确率/RankIC/coverage/有效准确率）
以及校准质量（分箱最大偏差 ECE）的影响。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config
from hexbroker.feature import build_features
from hexbroker.forecast.calibration import calibration_error, reliability_curve
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
METHODS = ["platt", "isotonic", "none"]


def build_cfg(cal_method: str):
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = cal_method
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    return cfg


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="概率校准对比（Platt vs Isotonic vs none）")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print("概率校准对比（冠军 v4 配置，全 66 折）：platt / isotonic / none")
    print("=" * 72)

    cfg0 = build_cfg("platt")
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
    for method in METHODS:
        print("-" * 72)
        print(f"[VARIANT] calibration={method}")
        cfg_v = build_cfg(method)
        wf = walk_forward_lightgbm(
            cfg_v, bars, features, params=best_params,
            collect_models=False, splitter_overrides=None, n_jobs_folds=args.n_jobs,
        )
        gate = records_to_gate(wf.records, realized)
        per_sym = per_symbol_gate(wf.records, realized)

        # 校准质量（基于 OOS 全部信号）
        sig = pd.DataFrame(wf.records)
        sig = sig.set_index(["symbol", "ts"]).sort_index()
        sig["realized"] = realized
        sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
        valid = sig.dropna(subset=["realized"])
        ece = calibration_error(valid["p_up"].to_numpy(), (valid["realized"] > 0).astype(float).to_numpy())
        rc = reliability_curve(valid["p_up"].to_numpy(), (valid["realized"] > 0).astype(float).to_numpy())

        row = {
            "method": method,
            "dir_acc": gate["direction_accuracy"],
            "eff_acc": gate["effective_accuracy"],
            "coverage": gate["coverage"],
            "rank_ic": gate["rank_ic"],
            "n_signals": gate.get("n_signals", len(wf.records)),
            "ece_max": ece,
            "reliability": rc,
        }
        per_sym_txt = " ".join(
            f"{s}:{per_sym[s]['direction_accuracy']*100:.2f}%" for s in per_sym
        )
        print(f"[GATE] dir_acc={row['dir_acc']*100:.2f}% eff_acc={row['eff_acc']*100:.2f}% "
              f"cov={row['coverage']*100:.2f}% rank_ic={row['rank_ic']:.4f} ece={ece:.4f} | {per_sym_txt}")
        rows.append(row)

    print("=" * 72)
    base = {r["method"]: r for r in rows}["platt"]
    for r in rows:
        d_dir = (r["dir_acc"] - base["dir_acc"]) * 100
        d_ic = r["rank_ic"] - base["rank_ic"]
        d_ece = r["ece_max"] - base["ece_max"]
        print(f"{r['method']:<10s} dir={r['dir_acc']*100:.2f}% (Δ{d_dir:+.2f}pp) "
              f"ic={r['rank_ic']:.4f} (Δ{d_ic:+.4f}) cov={r['coverage']*100:.2f}% "
              f"ece={r['ece_max']:.4f} (Δ{d_ece:+.4f})")

    report = {
        "title": "概率校准对比（Platt vs Isotonic vs none）",
        "report_date": REPORT_DATE,
        "symbols": SYMBOLS,
        "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k != "reliability"} for r in rows],
        "reliability_curves": {r["method"]: r["reliability"] for r in rows},
    }
    out = DELIVERABLE_DIR / f"lightgbm-calibration-compare-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
