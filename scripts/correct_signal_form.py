"""信号形态矫正实验（嵌套口径，2026-08-16）。

背景：raw alpha 诊断发现模型真实信息在 exp_ret 强度排序（Q0→Q4 单调 0.9pp/5日），
但 p_up>0.5 二元方向判断是弱投影（看空反指、阈值截断丢信息）。

本脚本在**嵌套口径**（cal_split=0.5：校准用测试窗前 50%，评估子窗=后 50%，零重叠）下
对比三种信号形态，得到矫正后的真实可交易水平：
- 形态 A：p_up>0.5 二元方向（方向准确率，嵌套基线）
- 形态 B：exp_ret 分位多空（Q4 做多 / Q0 做空，看多空价差）
- 形态 C：单边多头（exp_ret 前 20% 做多、其余空仓）
并验证 exp_ret 分位单调性在嵌套口径下是否保持。
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
    walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-16"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]
TRADES_PER_YEAR = 252 // 5  # 5 日 horizon 每年约 50 次换手


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
    ap = argparse.ArgumentParser(description="信号形态矫正实验（嵌套口径三形态对比）")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print("信号形态矫正实验：嵌套口径（cal_split=0.5）三形态对比")
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
        collect_models=False, splitter_overrides=None,
        n_jobs_folds=args.n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records)
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig = sig.dropna(subset=["realized"])
    n = len(sig)
    print(f"[OK] 嵌套评估子窗信号 {n} 条")

    report: dict = {"n": n, "horizon_days": 5}

    # ---- exp_ret 分位单调性（嵌套口径验证） ----
    sig["er_q"] = pd.qcut(sig["exp_ret"], 5, labels=False, duplicates="drop")
    q_rows = []
    for q in sorted(sig["er_q"].dropna().unique()):
        sub = sig[sig["er_q"] == q]
        q_rows.append({"q": int(q), "n": int(len(sub)), "mean_realized": float(sub["realized"].mean())})
        print(f"[Q{q}] n={len(sub):4d} 实际平均收益={sub['realized'].mean()*100:.3f}%")
    report["exp_ret_quintiles"] = q_rows
    q_vals = np.array([r["mean_realized"] for r in q_rows])
    monotone = float(np.corrcoef(np.arange(len(q_rows)), q_vals)[0, 1])
    report["quintile_monotone_corr"] = monotone
    print(f"[单调性] 分位-收益相关系数={monotone:+.3f}")

    # ---- 形态 A：p_up>0.5 二元方向 ----
    p_up = sig["p_up"].to_numpy(dtype=float)
    y = (sig["realized"].to_numpy(dtype=float) > 0).astype(int)
    acc_a = float(((p_up > 0.5).astype(int) == y).mean())
    report["formA_dir_acc"] = acc_a
    print(f"\n[形态A] p_up>0.5 方向准确率（嵌套）= {acc_a*100:.2f}%")

    # ---- 形态 B：exp_ret 分位多空（Q4-Q0 价差） ----
    q0 = q_rows[0]
    q4 = q_rows[-1]
    spread = q4["mean_realized"] - q0["mean_realized"]
    report["formB_spread_per_trade"] = spread
    report["formB_spread_annualized"] = spread * TRADES_PER_YEAR
    print(f"[形态B] exp_ret Q4-Q0 价差 = {spread*100:.3f}%/5日（年化×{TRADES_PER_YEAR} = {spread*TRADES_PER_YEAR*100:.1f}%）")

    # ---- 形态 C：单边多头（exp_ret 前 20% 做多） ----
    top_q = q_rows[-1]
    report["formC_top_quintile_ret"] = top_q["mean_realized"]
    report["formC_top_annualized"] = top_q["mean_realized"] * TRADES_PER_YEAR
    print(f"[形态C] 单边多头（exp_ret 前 20%）平均收益 = {top_q['mean_realized']*100:.3f}%/5日"
          f"（年化 {top_q['mean_realized']*TRADES_PER_YEAR*100:.1f}%）")

    # ---- 形态 C'：单边多头高置信（exp_ret 前 10%） ----
    q10 = pd.qcut(sig["exp_ret"], 10, labels=False, duplicates="drop")
    top10_mask = q10 == q10.max()
    ret_c2 = float(sig.loc[top10_mask, "realized"].mean())
    report["formC2_top_decile_ret"] = ret_c2
    report["formC2_top_decile_annualized"] = ret_c2 * TRADES_PER_YEAR
    print(f"[形态C'] 单边多头（exp_ret 前 10%）平均收益 = {ret_c2*100:.3f}%/5日"
          f"（年化 {ret_c2*TRADES_PER_YEAR*100:.1f}%，n={int(top10_mask.sum())}）")

    # ---- 分品种 exp_ret 价差（品种分化验证） ----
    per_sym = {}
    for sym in sig["symbol"].unique():
        sub = sig[sig["symbol"] == sym]
        qs = pd.qcut(sub["exp_ret"], 5, labels=False, duplicates="drop")
        means = sub.groupby(qs, observed=True)["realized"].mean()
        if len(means) >= 2:
            spread_s = float(means.iloc[-1] - means.iloc[0])
            per_sym[sym] = {"spread": spread_s, "n": int(len(sub))}
            print(f"[品种] {sym}: exp_ret Q4-Q0 价差={spread_s*100:.3f}%/5日 (n={len(sub)})")
    report["per_symbol_spread"] = per_sym

    # 判定
    verdict = []
    if acc_a <= 0.52:
        verdict.append(f"形态A 方向准确率 {acc_a*100:.2f}% 仍接近随机（嵌套口径）——方向形态确认为弱信号")
    if spread > 0.002:
        verdict.append(f"形态B 多空价差 {spread*100:.3f}%/5日 可交易（年化 {spread*TRADES_PER_YEAR*100:.1f}%）")
    if top_q["mean_realized"] > 0.002:
        verdict.append(f"形态C 单边多头 {top_q['mean_realized']*100:.3f}%/5日 可交易")
    if per_sym:
        worst = min(per_sym.values(), key=lambda v: v["spread"])
        verdict.append(f"品种分化：{worst['spread']*100:.2f}pp 最弱品种需单独评估")
    print("\n[判定] " + "；".join(verdict))
    report["verdict"] = verdict

    out = DELIVERABLE_DIR / f"signal-form-correction-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
