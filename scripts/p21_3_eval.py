#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P21-3 评估：v8 vs v12 覆盖率对比 + 引擎 A/组合重估（生产 cap 口径）。

Stage A  覆盖率对比 v8 vs v12 → artifacts/p21_cache_coverage.csv
Stage B  引擎 A 单引擎 S2（v12 vs v8）+ 组合 A30/B70 OOS（生产口径）
         → artifacts/p21_engineA_compare.csv

口径铁律：
  - 显式 load_config("configs/base.yaml") + 显式传参（§9.18）
  - 引擎 A：top_k=0.30 / min_symbols=3 / group_cap=0.5 / group_map(base.yaml) / score_col=exp_ret
  - 引擎 B：win=252 / thr=0.70（base.yaml 显式）
  - 组合：A30/B70（base.yaml combo），vol_target=False
  - OOS 2024-07-18 后；完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）
  - 引擎 B 名义按 base.yaml notional_frac=0.30（组合口径）——引擎 B 组合 targets
    在 combo_stats_row 中只用日收益加权，名义仅影响 hand 数（floor 截断），
    与 P19/P20 生产口径一致：B 有效名义 = 1e6×0.30×0.70=210k。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import engine_b_targets
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
    seg_sharpe,
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V12_PATH = ART / "signals_cache18_grouped_v12.parquet"
COV_CSV = ART / "p21_cache_coverage.csv"
EVAL_CSV = ART / "p21_engineA_compare.csv"

OOS_START = "2024-07-18"
TOP_K = 0.30
MIN_SYMBOLS_S2 = 3
OOS_SUB_BOUNDS = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]


# ---------------------------------------------------------------------------
# Stage A：覆盖率对比
# ---------------------------------------------------------------------------
def coverage_stats(path: Path) -> pd.DataFrame:
    """v8/v12 覆盖率统计：行数/唯一信号日/日均品种/末信号日/2026-08 月信号数。"""
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()

    # 2026-08 月信号（P21 关键：新数据月）
    aug = sig[sig["ts"] >= pd.Timestamp("2026-08-01")]
    aug_cov = aug.groupby(aug["ts"].dt.date)["symbol"].count()

    # 2026-07 后（fold 截断真空段）
    post_jun = sig[sig["ts"] > pd.Timestamp("2026-06-30")]

    row = {
        "cache": path.stem.replace("signals_cache18_grouped_", ""),
        "n_signals": int(len(sig)),
        "n_days": int(len(cov)),
        "unique_signal_days": int(sig["ts"].dt.date.nunique()),
        "avg_symbols_per_day": round(float(cov.mean()), 2),
        "min_symbols_day": int(cov.min()),
        "max_symbols_day": int(cov.max()),
        "pct_days_lt3": round(float(100 * (cov < 3).mean()), 1),
        "n_symbols": int(sig["symbol"].nunique()),
        "ts_start": str(pd.Timestamp(sig["ts"].min()).date()),
        "ts_end": str(pd.Timestamp(sig["ts"].max()).date()),
        "last_signal_date": str(pd.Timestamp(sig["ts"].max()).date()),
        # 2026-08 月信号（新数据月）
        "n_sig_2026_08": int(len(aug)),
        "n_days_2026_08": int(len(aug_cov)),
        "avg_sym_2026_08": round(float(aug_cov.mean()), 2) if len(aug_cov) else np.nan,
        # 2026-07 后（fold 截断真空段）
        "n_sig_post_20260630": int(len(post_jun)),
        "n_days_post_20260630": int(post_jun["ts"].dt.date.nunique()) if len(post_jun) else 0,
    }
    return pd.DataFrame([row])


def run_coverage_compare() -> None:
    print("-" * 72)
    print("[Stage A] 覆盖率对比 v8 vs v12")
    if not V12_PATH.exists():
        print(f"[FAIL] {V12_PATH} 不存在（先运行 scripts/p21_cache_rebuild.py）")
        raise SystemExit(1)
    df = pd.concat([coverage_stats(V8_PATH), coverage_stats(V12_PATH)], ignore_index=True)
    ART.mkdir(exist_ok=True)
    df.to_csv(COV_CSV, index=False)
    cols = ["cache", "n_signals", "n_days", "avg_symbols_per_day", "min_symbols_day",
            "max_symbols_day", "n_symbols", "ts_start", "ts_end", "last_signal_date"]
    print(df[cols].to_string(index=False))
    print("\n  2026-08 月（新数据月）:")
    print(df[["cache", "n_sig_2026_08", "n_days_2026_08", "avg_sym_2026_08"]].to_string(index=False))
    print("\n  fold 截断真空段（>2026-06-30）:")
    print(df[["cache", "n_sig_post_20260630", "n_days_post_20260630"]].to_string(index=False))
    print(f"\n  [OK] → {COV_CSV}")

    v8 = df[df["cache"] == "v8"].iloc[0]
    v12 = df[df["cache"] == "v12"].iloc[0]
    print(f"  覆盖率对比：v12 行数 {v8['n_signals']} → {v12['n_signals']} "
          f"({100 * v12['n_signals'] / v8['n_signals']:.0f}%) | "
          f"末信号日 {v8['last_signal_date']} → {v12['last_signal_date']} | "
          f"2026-08 月信号 {v8['n_sig_2026_08']} → {v12['n_sig_2026_08']}")


# ---------------------------------------------------------------------------
# Stage B：引擎 A/组合重估（生产 cap 口径）
# ---------------------------------------------------------------------------
def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def run_eval() -> None:
    print("-" * 72)
    print("[Stage B] 引擎 A 单引擎 + 组合重估（v8 vs v12，生产 cap 口径）")
    cfg = load_config("configs/base.yaml")  # §9.18：显式 base.yaml
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    combo = cfg.backtest.combo
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"  prices: {len(prices)} 行 | 口径: 滑点1tick+费0.005%+保证金12%+CONTRACTS18")
    print(f"  引擎A: top_k={ea.top_k} min={ea.min_symbols} cap={ea.group_cap}")
    print(f"  引擎B: win={eb.win} thr={eb.thr} notional_frac={eb.notional_frac}")
    print(f"  组合: A{combo.w_engine_a}/B{combo.w_engine_b} vol_target={combo.vol_target}")

    # ---- 引擎 B 基准（与缓存无关） ----
    tgt_b = engine_b_targets(prices, win=eb.win, thr=eb.thr)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    rows: list[dict] = []
    for cache_label, cache_path in (("v8", V8_PATH), ("v12", V12_PATH)):
        print(f"\n  --- cache={cache_label} ({cache_path.name}) ---")
        # 单引擎 A：S2（P5 终裁策略，生产 min=3）
        tgt_a = engine_a_targets_cs(
            prices, top_k=ea.top_k, min_symbols=ea.min_symbols,
            cache_path=cache_path, group_cap=ea.group_cap,
            group_map=ea.group_map, score_col="exp_ret",
        )
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt_a, f"A-S2-{cache_label}")
        n_long = int((tgt_a["target"] > 0).sum())
        seg1 = seg_sharpe(eq, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
        seg2 = seg_sharpe(eq, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
        rows.append({
            "cache": cache_label, "kind": "engineA_single", "config": "S2",
            "min_symbols": MIN_SYMBOLS_S2,
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "oos_seg1_sharpe": seg1, "oos_seg2_sharpe": seg2,
            "long_day_ratio": long_ratio, "n_long_rows": n_long,
            "oos_n": m_oos.n_bars if m_oos else 0,
        })
        print(f"    A-S2({cache_label}): Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={m_oos.sharpe:.3f} "
              f"OOS MaxDD={m_oos.max_drawdown*100:.1f}% | 做多行={n_long}")

        # 组合：生产 A30/B70（vol_target=False）
        ret_a_s2 = ret
        ra, rb = _align(ret_a_s2, ret_b)
        r = combo_stats_row(ra, rb, float(combo.w_engine_a), bool(combo.vol_target))
        rows.append({
            "cache": cache_label, "kind": "combo", "config": f"A{combo.w_engine_a:.0f}B{combo.w_engine_b:.0f}",
            "min_symbols": MIN_SYMBOLS_S2,
            "w_a": r["w_a"], "w_b": r["w_b"], "vol_target": r["vol_target"],
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"], "oos_sharpe": r["oos_sharpe"],
            "oos_maxdd": r["oos_maxdd"], "oos_ret": r["oos_ret"],
            "oos_seg1_sharpe": np.nan, "oos_seg2_sharpe": np.nan,
            "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r["oos_n"],
        })
        print(f"    combo A30/B70: Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f} "
              f"OOS ret={r['oos_ret']*100:+.1f}%")

    # 纯 B 参考（生产基座）
    ret_b_pure, eq_b_pure, _, _, _ = run_engine_row(cfg, cost, prices, tgt_b, "B")
    ra_ref, rb_ref = _align(ret_b_pure, ret_b_pure)
    r_pure = combo_stats_row(ra_ref, rb_ref, 1.0, False)
    rows.append({
        "cache": "ref", "kind": "engineB", "config": "pureB",
        "min_symbols": "", "w_a": 1.0, "w_b": 0.0, "vol_target": False,
        "sharpe_full": r_pure["sharpe_full"], "ann_ret_full": r_pure["ann_ret_full"],
        "maxdd_full": r_pure["maxdd_full"], "oos_sharpe": r_pure["oos_sharpe"],
        "oos_maxdd": r_pure["oos_maxdd"], "oos_ret": r_pure["oos_ret"],
        "oos_seg1_sharpe": seg_sharpe(eq_b_pure, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1]),
        "oos_seg2_sharpe": seg_sharpe(eq_b_pure, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1]),
        "long_day_ratio": np.nan, "n_long_rows": np.nan,
        "oos_n": r_pure["oos_n"],
    })
    print(f"    pureB 参考: Sharpe={r_pure['sharpe_full']:.3f} | OOS Sharpe={r_pure['oos_sharpe']:.3f}")

    df = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    df.to_csv(EVAL_CSV, index=False)
    print(f"\n  [OK] → {EVAL_CSV}")

    # ---- 对比摘要 ----
    print("\n  === v8 vs v12 对比摘要（引擎 A 单引擎 + 组合） ===")
    pivot = df[df["cache"].isin(["v8", "v12"])].copy()
    for _, r in pivot[pivot["kind"] == "engineA_single"].iterrows():
        print(f"    [{r['cache']}] 引擎A S2: 全样本 {r['sharpe_full']:.3f} | "
              f"OOS {r['oos_sharpe']:.3f} (seg1 {r['oos_seg1_sharpe']:.3f} / "
              f"seg2 {r['oos_seg2_sharpe']:.3f}) | OOS ret {r['oos_ret']*100:+.1f}%")
    for _, r in pivot[pivot["kind"] == "combo"].iterrows():
        if r["vol_target"]:
            continue
        print(f"    [{r['cache']}] combo A{r['w_a']:.2f}/B{r['w_b']:.2f} (vol=N): "
              f"全样本 {r['sharpe_full']:.3f} | OOS {r['oos_sharpe']:.3f} | "
              f"OOS ret {r['oos_ret']*100:+.1f}%")
    print(f"    [ref] 纯 B 参考: 全样本 {r_pure['sharpe_full']:.3f} | OOS {r_pure['oos_sharpe']:.3f}")

    # ---- 关键判断 ----
    s2 = pivot[(pivot["kind"] == "engineA_single") & (pivot["config"] == "S2")].set_index("cache")
    if "v12" in s2.index and "v8" in s2.index:
        d_sh = s2.loc["v12", "oos_sharpe"] - s2.loc["v8", "oos_sharpe"]
        print(f"\n  === 关键判断：引擎 A S2 OOS Sharpe 变化 {d_sh:+.3f} "
              f"({s2.loc['v8','oos_sharpe']:.3f} → {s2.loc['v12','oos_sharpe']:.3f}) ===")
        print(f"  v12 {'改善' if d_sh > 0 else '未改善'}引擎 A OOS —— "
              f"{'说明数据刷新带来增益' if d_sh > 0 else 'v12 与 v8 同构/无新增信号 → 不升级候选'}")
    else:
        print("\n  === 关键判断：v12 缺失，无法对比 ===")


def main() -> None:
    run_coverage_compare()
    run_eval()
    print("=" * 72)
    print("[DONE] P21-3 评估完成")


if __name__ == "__main__":
    main()
