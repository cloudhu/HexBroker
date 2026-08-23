"""特征工程迭代：单特征增益消融评估（第二轮特征）。

对 LightGBM 冠军模型（au/ag/m 日线，全 66 折 walk-forward，调优后 HP）逐个评估
``hexbroker/feature/iterative.py`` 新增的 8 个候选特征：

    base（现有 18 特征） vs base + {f_range_pos_20, f_intraday_ret, f_autocorr_20,
    f_skew_20, f_kurt_20, f_streak_dir, f_vol_ratio_5_20, f_ret_vol_corr_20}

口径与 R7 / refine 脚本严格一致：
- horizon=5, n_mc_samples=30, per-fold Platt 校准
- 调优后 HP 从 configs/forecast/lightgbm_champion.yaml 读取
- 特征变换：technical + microstructure（+ 单个 iterative 特征）→ 滚动 z-score（normalize）

判定标准（单特征通过线）：
- RankIC 提升 >= +0.01 且方向准确率不降；或
- 方向准确率提升 >= +0.3pp（且 coverage 不明显恶化）

用法：
    python scripts/ablate_features.py [--n-jobs N] [--limit-feature NAME] [--out MD]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omegaconf import OmegaConf  # noqa: E402

from hexbroker.config import load_config  # noqa: E402
from hexbroker.feature import build_features  # noqa: E402

# 复用 refine 脚本的 walk-forward 实现（已修复 per-fold 校准 + 折级并行）
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS,
    FREQ,
    DATA_START,
    DATA_END,
    build_source_plan,
    fetch_with_failover,
    compute_realized_returns,
    walk_forward_lightgbm,
    records_to_gate,
    per_symbol_gate,
)

REPORT_DATE = "2026-08-16"
CHAMPION_CFG = _ROOT / "configs" / "forecast" / "lightgbm_champion.yaml"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"

# 候选新特征（iterative.py 中定义）
CANDIDATES = [
    "f_range_pos_20",      # 20 日高低区间内收盘位置
    "f_intraday_ret",      # 日内收益（隔夜/日内收益分解）
    "f_autocorr_20",       # 收益 lag-1 自相关
    "f_skew_20",           # 20 日收益偏度
    "f_kurt_20",           # 20 日收益峰度
    "f_streak_dir",        # 带方向连续同向天数
    "f_vol_ratio_5_20",    # 波动率结构比（短期/长期）
    "f_ret_vol_corr_20",   # 收益-成交量相关（量价配合）
]

# 候选周线多尺度特征（weekly.py 中定义）
WEEKLY_CANDIDATES = [
    "f_week_ret_acc",   # 周内累计收益（本周动量进度）
    "f_week_pos",       # 周内位置（0~1）
    "f_week_vol",       # 周内已实现波动率
    "f_week_prev_ret",  # 上一完整周收益（周间动量）
]

# 候选跨品种特征（cross.py 中定义）：内盘两两比值，统一列名 f_xr_{a}_{b}（字典序）
CROSS_CANDIDATES = [
    "f_xr_au_ag",     # 金银比（经典套利信号）
    "f_xr_au_m",      # 金豆比（贵金属 vs 农产品）
    "f_xr_ag_m",      # 银豆比（贵金属 vs 农产品）
]

# 候选外盘参照特征（cross.py::add_cross_global）：每外盘代码生成 3 个特征
# （比值 f_xr_{code}_ratio、外盘动量 f_xr_{code}_mom、外盘波动率 f_xr_{code}_vol）
GLOBAL_CANDIDATES = [
    "spx",   # 标普500（data/raw/global/spx.parquet）
    "wti",   # WTI 原油（data/raw/global/wti.parquet）
    "ixic",  # 纳斯达克综合（data/raw/global/ixic.parquet）
    "dji",   # 道琼斯工业（data/raw/global/dji.parquet）
    "uup",   # 做多美元 ETF = 美元指数代理（data/raw/global/uup.parquet）
    "tlt",   # 20+年美债 ETF = 10Y 收益率代理（负相关，data/raw/global/tlt.parquet）
    "t10y",  # 真实 10Y 美债收益率 DGS10（FRED，data/raw/global/t10y.parquet）
    "ief",   # 7-10 年美债 ETF = 10Y 收益率价格代理（data/raw/global/ief.parquet）
]

# 外盘加载/对齐逻辑下沉至 hexbroker 供生产复用（见 hexbroker/feature/global_ref.py）
from hexbroker.feature.global_ref import (  # noqa: E402
    align_global_to_inner,
    load_global_close,
)

# 通过线（d_dir 已换算为 pp 单位，见 SUMMARY 段）
PASS_RANKIC_DELTA = 0.01       # RankIC 提升阈值
PASS_DIRACC_DELTA_PP = 0.3     # 方向准确率提升阈值（pp 单位，与 d_dir 一致）
PASS_COVERAGE_FLOOR = 0.80     # coverage 不应显著恶化


def load_best_params() -> dict:
    """从 lightgbm_champion.yaml 读取调优后 HP（forecast.lgbm_*）。"""
    c = OmegaConf.load(str(CHAMPION_CFG))
    f = c.forecast
    return {k: float(v) for k, v in f.items() if k.startswith("lgbm_")}


def build_variant_cfg(base_cfg: Any, extra_feature: Optional[str], base_feature: Optional[str] = None,
                      global_codes: Optional[list] = None) -> Any:
    """构造特征变体 cfg：base_feature（冠军基线）+ extra_feature（候选）。

    base_feature：所有变体共有的基线特征（如冠军 v2 的 f_range_pos_20）；
    extra_feature=None 表示仅基线；``f_xr_`` 前缀走 cross；GLOBAL_CANDIDATES 中的
    外盘代码走 cross global 组模式（global_codes）；其余走 iterative。
    global_codes：本次消融启用的外盘代码集合（来自 --global-code）。
    """
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30

    tf = ["technical", "microstructure"]
    iter_feats: list[str] = []
    cross_feats: list[str] = []
    week_feats: list[str] = []
    gcodes: list[str] = []
    if base_feature:
        for f in base_feature.split(","):
            f = f.strip()
            if f in GLOBAL_CANDIDATES:
                gcodes.append(f)
            elif f.startswith("f_xr_"):
                cross_feats.append(f)
            elif f.startswith("f_week_"):
                week_feats.append(f)
            else:
                iter_feats.append(f)
    if extra_feature:
        for f in extra_feature.split(","):
            f = f.strip()
            if f in GLOBAL_CANDIDATES:
                gcodes.append(f)
            elif f.startswith("f_xr_"):
                cross_feats.append(f)
            elif f.startswith("f_week_"):
                week_feats.append(f)
            else:
                iter_feats.append(f)
    if iter_feats:
        tf.append("iterative")
        cfg.feature.iterative_params = {"include": iter_feats}
    if week_feats:
        tf.append("weekly")
        cfg.feature.weekly_params = {"include": week_feats}
    if cross_feats or gcodes:
        tf.append("cross")
        cp: dict = {}
        if cross_feats:
            cp["include"] = cross_feats
        if gcodes:
            cp["global_codes"] = gcodes
        cfg.feature.cross_params = cp
    tf.append("normalize")
    cfg.feature.transformers = tf
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="特征消融：LightGBM 冠军模型单特征增益评估")
    ap.add_argument("--n-jobs", type=int, default=int(__import__("os").cpu_count() or 1))
    ap.add_argument("--limit-feature", type=str, default=None,
                    help="只评估指定特征（调试用）")
    ap.add_argument("--combine", type=str, default=None,
                    help="组合特征（逗号分隔），一次性加入多个（如 'f_range_pos_20,f_kurt_20'）")
    ap.add_argument("--base-feature", type=str, default=None,
                    help="所有变体共有的基线特征（如冠军 v2 的 f_range_pos_20）")
    ap.add_argument("--global-code", type=str, default=None,
                    help="外盘参照代码（逗号分隔，如 spx,wti）：启用 cross global 特征")
    ap.add_argument("--global-only", action="store_true",
                    help="候选只取外盘参照组（GLOBAL_CANDIDATES），跳过 iterative/内盘比值")
    ap.add_argument("--weekly-only", action="store_true",
                    help="候选只取周线多尺度组（WEEKLY_CANDIDATES）")
    ap.add_argument("--out", type=str, default=str(
        DELIVERABLE_DIR / f"lightgbm-feature-ablation-{REPORT_DATE}.md"))
    args = ap.parse_args()

    print("=" * 72)
    print("特征工程迭代：单特征增益消融（冠军基线 + 候选特征）")
    print("=" * 72)

    # 一次加载数据
    cfg0 = load_config()
    cfg0.data.symbols = list(SYMBOLS)
    cfg0.data.freq = FREQ
    cfg0.data.start = DATA_START
    cfg0.data.end = DATA_END
    cfg0.forecast.horizon = 5
    cfg0.forecast.n_mc_samples = 30
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | symbols={bars.symbols} bars={bars.length}")
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))

    # 外盘上下文（可选）：shift(1)+asof 时差安全对齐到内盘交易日
    # --global-only 时若未显式传 --global-code，自动加载全部 GLOBAL_CANDIDATES，
    # 防止候选含外盘代码但 global_close 为空 → 静默生成 0 新特征（n_cols 不变的失真消融）
    if args.global_only and not args.global_code:
        args.global_code = ",".join(GLOBAL_CANDIDATES)
        print(f"[INFO] --global-only 未指定 --global-code，自动加载全部外盘候选：{args.global_code}")
    global_close: dict[str, pd.Series] = {}
    if args.global_code:
        inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
        for code in [c.strip() for c in args.global_code.split(",") if c.strip()]:
            g = load_global_close(code)
            g_aligned = align_global_to_inner(g, inner_dates)
            global_close[code] = g_aligned
            print(f"[OK] 外盘 {code}: {len(g)} 根 -> 对齐 {g_aligned.notna().sum()} 个内盘交易日")

    best_params = load_best_params()
    print(f"[OK] 调优 HP：{best_params}")
    base_feature = args.base_feature
    if base_feature:
        print(f"[OK] 冠军基线特征：{base_feature}")

    variants: list[tuple[str, Optional[str]]] = [("base", None)]
    if args.global_only:
        cands = GLOBAL_CANDIDATES
    elif args.weekly_only:
        cands = WEEKLY_CANDIDATES
    else:
        cands = [args.limit_feature] if args.limit_feature else (CANDIDATES + CROSS_CANDIDATES + WEEKLY_CANDIDATES + GLOBAL_CANDIDATES)
    for feat in cands:
        short = feat.split(",")[0] if "," in feat else feat
        variants.append((f"+{short}", feat))
    # 组合变体：--combine "f1,f2" 一次性加入多个特征
    if args.combine:
        feats = [f.strip() for f in args.combine.split(",") if f.strip()]
        variants.append((f"+{'+'.join(feats)}", ",".join(feats)))

    rows: list[dict] = []
    base_cols: Optional[int] = None
    for label, extra in variants:
        print("-" * 72)
        print(f"[VARIANT] {label}")
        cfg_v = build_variant_cfg(cfg0, extra, base_feature=base_feature, global_codes=list(global_close.keys()))
        features = build_features(bars, cfg_v, global_close=global_close)
        if label == "base":
            base_cols = len(features.df.columns)
        n_cols = len(features.df.columns)
        print(f"[OK] 特征：n_cols={n_cols} "
              f"({features.symbols})")
        # 防静默失真：非 base 变体必须比 base 多至少 1 列，否则候选特征未生效（消融结论无效）
        if label != "base" and base_cols is not None and n_cols <= base_cols:
            raise RuntimeError(
                f"[FATAL] 变体 {label} 特征列数未增加（base={base_cols} -> {n_cols}），"
                f"候选特征未生效——检查 --global-code/特征名是否正确加载。"
            )
        wf = walk_forward_lightgbm(
            cfg_v, bars, features, params=best_params,
            collect_models=False, splitter_overrides=None,
            n_jobs_folds=args.n_jobs,
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

    # 汇总表
    base = rows[0]
    print("=" * 72)
    print("[SUMMARY] 与 base 对比")
    summary_lines = []
    for r in rows[1:]:
        d_dir = (r["dir_acc"] - base["dir_acc"]) * 100
        d_ic = r["rank_ic"] - base["rank_ic"]
        d_cov = (r["coverage"] - base["coverage"]) * 100
        passed = (
            (d_ic >= PASS_RANKIC_DELTA and d_dir >= -0.001)
            or (d_dir >= PASS_DIRACC_DELTA_PP and r["coverage"] >= PASS_COVERAGE_FLOOR)
        )
        mark = "PASS" if passed else "----"
        line = (f"{r['variant']:<24s} dir_acc={r['dir_acc']*100:6.2f}% (Δ{d_dir:+5.2f}pp) "
                f"rank_ic={r['rank_ic']:.4f} (Δ{d_ic:+.4f}) cov={r['coverage']*100:5.2f}% "
                f"(Δ{d_cov:+.2f}pp) -> {mark}")
        print(line)
        summary_lines.append({
            "variant": r["variant"],
            "dir_acc_pct": round(r["dir_acc"] * 100, 2),
            "dir_acc_delta_pp": round(d_dir, 2),
            "eff_acc_pct": round(r["eff_acc"] * 100, 2),
            "coverage_pct": round(r["coverage"] * 100, 2),
            "rank_ic": round(r["rank_ic"], 4),
            "rank_ic_delta": round(d_ic, 4),
            "n_signals": r["n_signals"],
            "passed": passed,
        })

    # 写报告
    report = {
        "title": "LightGBM 冠军模型特征工程迭代（单特征消融）",
        "report_date": REPORT_DATE,
        "data_source": chosen,
        "symbols": SYMBOLS,
        "horizon": 5,
        "n_mc_samples": 30,
        "best_params": best_params,
        "base": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in base.items()},
        "variants": summary_lines,
        "pass_criteria": {
            "rank_ic_delta": PASS_RANKIC_DELTA,
            "dir_acc_delta_pp": PASS_DIRACC_DELTA_PP,
            "coverage_floor": PASS_COVERAGE_FLOOR,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(_render_markdown(report))
    with open(out.with_suffix(".json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 消融报告：{out}")
    print("[DONE]")


def _render_markdown(r: dict) -> str:
    lines = [
        f"# {r['title']}",
        "",
        f"> 报告日期：**{r['report_date']}** | 数据源：{r['data_source']} | "
        f"品种：{r['symbols']} | horizon={r['horizon']} n_mc={r['n_mc_samples']}",
        "",
        "## 1. 消融结果（全 66 折，调优后 HP）",
        "",
        "| 变体 | 特征数 | 方向准确率 | Δpp | 有效准确率 | coverage | RankIC | ΔRankIC | 信号数 | 判定 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    b = r["base"]
    lines.append(
        f"| base（18 特征） | {b['n_features']} | {b['dir_acc']*100:.2f}% | — | "
        f"{b['eff_acc']*100:.2f}% | {b['coverage']*100:.2f}% | {b['rank_ic']:.4f} | — | "
        f"{b['n_signals']} | 基准 |"
    )
    for v in r["variants"]:
        mark = "✅ PASS" if v["passed"] else "❌ 未过线"
        lines.append(
            f"| {v['variant']} | — | {v['dir_acc_pct']:.2f}% | {v['dir_acc_delta_pp']:+.2f} | "
            f"{v['eff_acc_pct']:.2f}% | {v['coverage_pct']:.2f}% | {v['rank_ic']:.4f} | "
            f"{v['rank_ic_delta']:+.4f} | {v['n_signals']} | {mark} |"
        )
    lines += [
        "",
        "## 2. 通过线",
        "",
        f"- RankIC 提升 ≥ {r['pass_criteria']['rank_ic_delta']} 且方向准确率不降；或方向准确率提升 ≥ "
        f"{r['pass_criteria']['dir_acc_delta_pp']}pp（coverage 不低于 {r['pass_criteria']['coverage_floor']*100:.0f}%）。",
        "",
        "## 3. 调优后 HP",
        "",
        "```yaml",
    ]
    for k, v in r["best_params"].items():
        lines.append(f"{k}: {v}")
    lines += ["```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
