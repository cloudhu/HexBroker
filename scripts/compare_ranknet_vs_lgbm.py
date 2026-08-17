"""SENTINEL Phase 1 验证：RankNet 强度排序器 vs LightGBM 回归（嵌套口径）。

跨品种合并训练：共享 RankNet，group = 交易日（同一天 3 品种构成截面 pairwise），
目标 = 品种内 z-score 的 5 日实现收益（去除品种基准差异，保留截面相对强度）。

与 walk_forward 相同 66 折切分；test 评估子窗（嵌套 cal_split=0.5）。
对比：LightGBM exp_ret vs RankNet 分数的 Q4-Q0 价差 / 排序 IC。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config  # noqa: E402
from hexbroker.feature import build_features  # noqa: E402
from hexbroker.forecast.intensity import IntensityRanker  # noqa: E402
from hexbroker.data.splitter import WalkForwardSplitter  # noqa: E402
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, compute_realized_returns,
    walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-17"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]
TOP_FRAC = 0.20
CAL_SPLIT = 0.5


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
    ap = argparse.ArgumentParser(description="RankNet vs LightGBM（嵌套口径，跨品种）")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--epochs", type=int, default=25)
    args = ap.parse_args()

    print("=" * 72)
    print("SENTINEL Phase 1：跨品种 RankNet vs LightGBM exp_ret（嵌套口径）")
    print("=" * 72)

    cfg0 = build_cfg()
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))
    best_params = load_best_params()

    # ---- LightGBM 基线（嵌套口径 exp_ret） ----
    wf = walk_forward_lightgbm(
        cfg0, bars, features, params=best_params,
        collect_models=False, splitter_overrides=None,
        n_jobs_folds=args.n_jobs, cal_split=CAL_SPLIT,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index().dropna(subset=["realized"])
    lgbm_q = _quintile_metrics(sig["exp_ret"].to_numpy(), sig["realized"].to_numpy())
    lgbm_ic = stats.spearmanr(sig["exp_ret"], sig["realized"]).statistic
    print(f"[LightGBM exp_ret] Q4-Q0={lgbm_q*100:.3f}%/5日 RankIC={lgbm_ic:+.4f} (n={len(sig)})")

    # ---- 特征长表（三品种合并，f_* 列，跨品种可比） ----
    fcols = [c for c in features.df.columns if c.startswith("f_")]
    feat_df = features.df[fcols].copy()
    feat_df["_sym"] = features.df.index.get_level_values(0)
    feat_df["_ts"] = features.df.index.get_level_values(1)
    # realized 对齐
    feat_df["_realized"] = realized
    feat_df = feat_df.dropna(subset=["_realized"]).sort_values("_ts")
    # 品种内 z-score 目标（跨品种基准差异去除）
    feat_df["_y"] = feat_df.groupby("_sym")["_realized"].transform(
        lambda s: (s - s.mean()) / (s.std() + 1e-9)
    )
    ts_all = feat_df["_ts"].unique()
    ts_idx = pd.Index(ts_all)

    splitter = WalkForwardSplitter(
        train_len=int(cfg0.data.train_len), test_len=int(cfg0.data.test_len),
        purge=int(cfg0.data.purge), embargo=int(cfg0.data.embargo),
        mode=str(cfg0.data.mode),
    )
    # split 输入：按 ts 位置索引（与 walk_forward 对齐的 66 折近似）
    folds = splitter.split(np.arange(len(ts_idx)))

    eval_rows: list[pd.Series] = []
    for fi, fold in enumerate(folds):
        tr_end = int(fold.train_max_pos)
        te_s, te_e = int(fold.test_start), int(fold.test_end)
        if tr_end <= 0 or te_e <= te_s:
            continue
        tr_ts = ts_idx[: tr_end + 1]
        Xtr = feat_df[feat_df["_ts"].isin(tr_ts)]
        Xte = feat_df[feat_df["_ts"].isin(ts_idx[te_s:te_e])]
        if len(Xtr) < 60 or len(Xte) < 10:
            continue
        model = IntensityRanker(in_dim=len(fcols), hidden=32, lr=3e-3,
                                epochs=args.epochs, seed=0)
        group = pd.factorize(Xtr["_ts"])[0]
        model.fit(Xtr[fcols].to_numpy(dtype=float), Xtr["_y"].to_numpy(dtype=float), group)
        # 嵌套：只评估 test 后半（评估子窗）
        te_ts = ts_idx[te_s:te_e]
        k = int(len(te_ts) * CAL_SPLIT)
        Xeval = Xte[Xte["_ts"].isin(te_ts[k:])]
        if len(Xeval) == 0:
            continue
        preds = model.predict(Xeval[fcols].to_numpy(dtype=float))
        eval_rows.append(pd.DataFrame({
            "symbol": Xeval["_sym"].to_numpy(),
            "score": preds,
            "realized": Xeval["_realized"].to_numpy(),
        }))
        if (fi + 1) % 11 == 0:
            print(f"  ... {fi+1} 折完成")

    if eval_rows:
        ev = pd.concat(eval_rows, ignore_index=True)
        rn_q = _quintile_metrics(ev["score"].to_numpy(), ev["realized"].to_numpy())
        rn_ic = stats.spearmanr(ev["score"], ev["realized"]).statistic
        print(f"[RankNet 分数] Q4-Q0={rn_q*100:.3f}%/5日 RankIC={rn_ic:+.4f} (n={len(ev)})")
    else:
        rn_q, rn_ic = float("nan"), float("nan")
        print("[RankNet] 无有效预测")

    verdict = "✅ 排序目标更优 → L1 路线成立" if rn_q > lgbm_q else "⚠️ 回归目标仍优 → L1 路线需调整"
    print(f"\n[判定] RankNet Q4-Q0={rn_q*100:.3f}% vs LightGBM Q4-Q0={lgbm_q*100:.3f}%：{verdict}")

    report = {
        "lgbm_exp_ret": {"q4_q0": lgbm_q, "rank_ic": lgbm_ic, "n": int(len(sig))},
        "ranknet": {"q4_q0": rn_q, "rank_ic": rn_ic, "n": int(len(eval_rows) and len(ev)) if eval_rows else 0},
        "verdict": verdict,
    }
    out = DELIVERABLE_DIR / f"ranknet-vs-lgbm-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


def _quintile_metrics(scores: np.ndarray, realized: np.ndarray) -> float:
    thr_hi = np.quantile(scores, 1 - TOP_FRAC)
    thr_lo = np.quantile(scores, TOP_FRAC)
    q4 = realized[scores >= thr_hi].mean()
    q0 = realized[scores <= thr_lo].mean()
    return float(q4 - q0)


if __name__ == "__main__":
    main()
