"""诊断 raw p_up 的真实 alpha 稳健性（嵌套验证发现 70.23% 含幻觉后）。

跑全 66 折 cal_method=none（raw p_up，不经校准），从 7 个维度判定
raw 方向预测是否含真实信息，还是统计噪声：

A. 总体显著性：方向准确率 + 二项检验 p 值 + 95% Wilson CI
B. 分品种：au/ag/m 各自准确率与检验
C. 分年度：2018-2024 每年准确率（alpha 是否时间持续）
D. 阈值分层：raw p_up 分箱内的方向准确率（高置信区间是否更高 → 真实排序信息）
E. exp_ret 分层：期望收益分组对应的实际收益
F. RankIC：raw p_up vs realized 的 Spearman（分年度）
G. 随机对照：shuffle realized 模拟零假设分布，看观测值是否在 95% 之外
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
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, compute_realized_returns,
    walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-16"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]


def wilson_ci(n: int, p: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间。"""
    if n == 0:
        return (0.0, 0.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def binomial_p(n: int, k: int, p0: float = 0.5) -> float:
    """单侧二项检验：观测 k/n 显著高于 p0 的 p 值。"""
    if n == 0:
        return 1.0
    return float(stats.binomtest(k, n, p0, alternative="greater").pvalue)


def build_cfg():
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "none"  # raw p_up，不经校准
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="诊断 raw p_up 真实 alpha 稳健性")
    ap.add_argument("--n-jobs", type=int, default=12)
    ap.add_argument("--n-shuffle", type=int, default=200, help="随机对照 shuffle 次数")
    args = ap.parse_args()

    print("=" * 72)
    print("诊断 raw p_up 真实 alpha（calibration=none，全 66 折）")
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

    wf = walk_forward_lightgbm(
        cfg0, bars, features, params=best_params,
        collect_models=False, splitter_overrides=None, n_jobs_folds=args.n_jobs,
    )
    sig = pd.DataFrame(wf.records)
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig = sig.dropna(subset=["realized"])
    n = len(sig)
    print(f"[OK] raw 信号 {n} 条")

    report: dict = {}
    p_up = sig["p_up"].to_numpy(dtype=float)
    y = (sig["realized"].to_numpy(dtype=float) > 0).astype(int)
    pred = (p_up > 0.5).astype(int)

    # A. 总体显著性
    acc = float((pred == y).mean())
    k = int((pred == y).sum())
    pval = binomial_p(n, k)
    lo, hi = wilson_ci(n, acc)
    print(f"\n[A] 总体：dir_acc={acc*100:.2f}% (n={n}, k={k}) "
          f"二项检验 p={pval:.4f} 95%CI=[{lo*100:.2f}%,{hi*100:.2f}%]")
    report["overall"] = {"dir_acc": acc, "n": n, "binom_p": pval, "ci95": [lo, hi]}

    # B. 分品种
    per_sym = {}
    for sym in sig["symbol"].unique():
        sub = sig[sig["symbol"] == sym]
        a = float(((sub["p_up"] > 0.5).astype(int) == (sub["realized"] > 0).astype(int)).mean())
        pv = binomial_p(len(sub), int(((sub["p_up"] > 0.5).astype(int) == (sub["realized"] > 0).astype(int)).sum()))
        per_sym[sym] = {"dir_acc": a, "n": int(len(sub)), "binom_p": pv}
        print(f"[B] {sym}: dir_acc={a*100:.2f}% (n={len(sub)}) p={pv:.4f}")
    report["per_symbol"] = per_sym

    # C. 分年度
    sig["year"] = pd.to_datetime(sig["ts"]).dt.year
    per_year = {}
    for yr, sub in sig.groupby("year"):
        a = float(((sub["p_up"] > 0.5).astype(int) == (sub["realized"] > 0).astype(int)).mean())
        pv = binomial_p(len(sub), int(((sub["p_up"] > 0.5).astype(int) == (sub["realized"] > 0).astype(int)).sum()))
        per_year[int(yr)] = {"dir_acc": a, "n": int(len(sub)), "binom_p": pv}
        print(f"[C] {yr}: dir_acc={a*100:.2f}% (n={len(sub)}) p={pv:.4f}")
    report["per_year"] = per_year

    # D. 阈值分层（p_up 分箱）
    bins = [0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 1.0]
    labels = ["<0.40", "0.40-0.45", "0.45-0.50", "0.50-0.55", "0.55-0.60", ">0.60"]
    sig["bucket"] = pd.cut(p_up, bins=bins, labels=labels, include_lowest=True)
    bucket_rows = []
    for lab in labels:
        sub = sig[sig["bucket"] == lab]
        if len(sub) == 0:
            continue
        a = float(((sub["p_up"] > 0.5).astype(int) == (sub["realized"] > 0).astype(int)).mean())
        up_ratio = float((sub["realized"] > 0).mean())
        bucket_rows.append({"bucket": lab, "n": int(len(sub)), "dir_acc": a, "up_ratio": up_ratio})
        print(f"[D] {lab}: n={len(sub):4d} dir_acc={a*100:.2f}% 实际上涨比例={up_ratio*100:.2f}%")
    report["threshold_buckets"] = bucket_rows

    # E. exp_ret 分层（期望收益分组 → 实际收益）
    sig["er_q"] = pd.qcut(sig["exp_ret"], 5, labels=False, duplicates="drop")
    er_rows = []
    for q in sorted(sig["er_q"].dropna().unique()):
        sub = sig[sig["er_q"] == q]
        mean_real = float(sub["realized"].mean())
        er_rows.append({"er_quintile": int(q), "n": int(len(sub)), "mean_realized": mean_real})
        print(f"[E] exp_ret 分位 {int(q)}: n={len(sub):4d} 实际平均收益={mean_real*100:.3f}%")
    report["exp_ret_quintiles"] = er_rows

    # F. RankIC（raw p_up vs realized）分年度
    ic_rows = []
    for yr, sub in sig.groupby("year"):
        if len(sub) < 5:
            continue
        ic = float(stats.spearmanr(sub["p_up"], sub["realized"]).statistic)
        ic_rows.append({"year": int(yr), "rank_ic": ic, "n": int(len(sub))})
        print(f"[F] {yr}: raw RankIC={ic:+.4f}")
    overall_ic = float(stats.spearmanr(p_up, sig["realized"].to_numpy(dtype=float)).statistic)
    ic_rows.append({"year": "all", "rank_ic": overall_ic, "n": n})
    print(f"[F] 全期 raw RankIC={overall_ic:+.4f}")
    report["rank_ic"] = ic_rows

    # G. 随机对照（shuffle realized 零假设分布）
    rng = np.random.default_rng(0)
    null_accs = []
    for _ in range(args.n_shuffle):
        ys = rng.permutation(y)
        null_accs.append(float((pred == ys).mean()))
    null_arr = np.array(null_accs)
    pct = float((null_arr >= acc).mean())
    print(f"\n[G] 随机对照（n={args.n_shuffle} 次 shuffle）：零假设 dir_acc "
          f"均值={null_arr.mean()*100:.2f}% std={null_arr.std()*100:.2f}% "
          f"95%分位={np.percentile(null_arr, 95)*100:.2f}% | 观测 {acc*100:.2f}% 位于 {pct*100:.1f}% 分位")
    report["null_dist"] = {
        "n_shuffle": args.n_shuffle, "null_mean": float(null_arr.mean()),
        "null_std": float(null_arr.std()), "null_p95": float(np.percentile(null_arr, 95)),
        "obs_pctile": pct,
    }

    # 综合判定
    verdicts = []
    if pval < 0.05:
        verdicts.append(f"总体二项检验显著（p={pval:.4f}）")
    else:
        verdicts.append(f"总体二项检验不显著（p={pval:.4f}）")
    n_sig_years = sum(1 for v in per_year.values() if v["binom_p"] < 0.05)
    verdicts.append(f"显著年份 {n_sig_years}/{len(per_year)}")
    n_sig_syms = sum(1 for v in per_sym.values() if v["binom_p"] < 0.05)
    verdicts.append(f"显著品种 {n_sig_syms}/{len(per_sym)}")
    # 阈值分层单调性：>0.6 分箱方向准确率 vs 全体
    if bucket_rows:
        top_bucket = bucket_rows[-1]
        monotone = top_bucket["dir_acc"] > acc + 0.02
        verdicts.append(f"高置信分箱(>{labels[-1][1:]}) dir_acc={top_bucket['dir_acc']*100:.2f}% {'单调提升' if monotone else '无提升'}")
    if pct < 0.95:
        verdicts.append("随机对照：观测在零假设 95% 之内（不排除噪声）")
    else:
        verdicts.append("随机对照：观测超出零假设 95% 分位（有真实信息）")
    print("\n[判定] " + "；".join(verdicts))
    report["verdict"] = verdicts

    out = DELIVERABLE_DIR / f"raw-alpha-diagnosis-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
