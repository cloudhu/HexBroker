"""P6-3b 数据补齐后重建信号缓存（v4）+ 引擎 A 重估编排脚本。

背景
----
P6-4 已补齐 18 品种 K 线至 2026-08-17（闭合率 100%，2022/2024 历史缺口全闭），
au0/ag0/m0 为未复权原始口径（RAW_SCALE_FIX 已换算），直接读 parquet 即可。

v2 缓存（artifacts/signals_cache18_grouped_v2.parquet）是 P7 基线：
  7952 行 / 976 天 / 日均 8.15 品种；2026 尾部因数据缺口仅 2.2 品种/天、ts_end=2026-06-11。
P6-3 的 v3 方案（cal_return_all=True 覆盖率翻倍但信号质量稀释）OOS 恶化，未采纳；
当时数据不完整（2026 尾部真空）。本脚本为 P6-3 方案 B 的关键验证：
**只换数据、不改信号生产逻辑**（v2 口径：GROUPS_V2、cal_split=0.5、cal_return_all=False、
walk_forward 默认参数），重建 v4 缓存 → 对比覆盖率 → 引擎 A 单引擎 + 组合重估。

关键判断：数据补齐（尤其 2026 尾部 15 品种 31 天→147 天）是否让引擎 A OOS 改善？
如未改善 → 引擎 A 瓶颈在信号质量而非覆盖率（如实报告）。

Stage 1  重建 v4：python scripts/group_modeling_v2.py --n-jobs 6
          --output artifacts/signals_cache18_grouped_v4.parquet（无 --cal-return-all）
Stage 2  覆盖率对比 v2 vs v4 → artifacts/p6_3b_cache_coverage.csv
Stage 3  引擎 A 单引擎 + 组合重估（v2 vs v4）→ artifacts/p6_3b_engineA_compare.csv

用法
----
  python scripts/p6_3b_rebuild_v4.py                 # 全流程（重建耗时长建议后台）
  python scripts/p6_3b_rebuild_v4.py --skip-rebuild  # v4 已生成后跑 2/3
  python scripts/p6_3b_rebuild_v4.py --coverage-only # 仅 Stage 2
  python scripts/p6_3b_rebuild_v4.py --eval-only     # 仅 Stage 3
  python scripts/p6_3b_rebuild_v4.py --force         # 强制重跑 Stage 1（删除已有 v4）

口径铁律：OOS 2024-07-18 后；嵌套零泄漏；完整回测（滑点1tick+费0.005%+保证金12%+
CONTRACTS18）；引擎 A 排序只用 exp_ret.rank（按日截面）；不改 hexbroker/数据/v2 缓存。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

ART = ROOT / "artifacts"
V2_PATH = ART / "signals_cache18_grouped_v2.parquet"
V4_PATH = ART / "signals_cache18_grouped_v4.parquet"
LOG_PATH = ART / "p6_3b_rebuild_v4.log"
COV_CSV = ART / "p6_3b_cache_coverage.csv"
EVAL_CSV = ART / "p6_3b_engineA_compare.csv"

# 生产配置（config.py 固化：EngineBConfig + ComboConfig，同 P7 基线）
PROD_W_A = 0.15
PROD_W_B = 0.85
A25 = 0.25

# 口径常量（与 p3/p5/p6_3 一致）
TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
OOS_START = "2024-07-18"
IS_END = "2022-04-21"
OOS_SUB_BOUNDS = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]
MIN_SYMBOLS_S2 = 3  # P5 终裁选中策略：截面 rank + min=3

# v2 缓存 ts_end（P6-2/P6-3 记录的 2026 尾部真空起点）——2026 尾部 = ts 严格晚于此
V2_TAIL_CUT = "2026-06-11"


# ---------------------------------------------------------------------------
# Stage 1：重建 v4 缓存（v2 口径，只换数据）
# ---------------------------------------------------------------------------
def run_rebuild(n_jobs: int, force: bool) -> None:
    print("-" * 72)
    print(f"[Stage 1] 重建 v4 信号缓存（v2 口径：cal_return_all=False）n_jobs={n_jobs}")
    if V4_PATH.exists() and not force:
        print(f"  [SKIP] 已存在 {V4_PATH.name}（如需重跑请加 --force）")
        return
    if V4_PATH.exists():
        V4_PATH.unlink()
        print(f"  [RM] 删除旧 {V4_PATH.name}（--force）")
    cmd = [
        sys.executable, "-u", "scripts/group_modeling_v2.py",
        "--n-jobs", str(n_jobs),
        "--output", str(V4_PATH),
    ]
    print(f"  CMD: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("[FAIL] 重建退出码非 0")
        raise SystemExit(r.returncode)
    if not V4_PATH.exists():
        print(f"[FAIL] 未产出 {V4_PATH}")
        raise SystemExit(1)
    print(f"  [OK] v4 缓存已生成：{V4_PATH}")


# ---------------------------------------------------------------------------
# Stage 2：覆盖率对比
# ---------------------------------------------------------------------------
def coverage_stats(path: Path) -> pd.DataFrame:
    """与 p6_3 完全一致的覆盖率统计 + 2026 尾部细化指标。"""
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()
    years = {}
    for y, g in sig.groupby(sig["ts"].dt.year):
        c = g.groupby(g["ts"].dt.date)["symbol"].count()
        years[str(int(y))] = round(float(c.mean()), 2)

    tail = sig[sig["ts"] > pd.Timestamp(V2_TAIL_CUT)]
    tail_cov = tail.groupby(tail["ts"].dt.date)["symbol"].count()
    row = {
        "cache": path.stem.replace("signals_cache18_grouped_", ""),
        "n_signals": int(len(sig)),
        "n_days": int(len(cov)),
        "avg_symbols_per_day": round(float(cov.mean()), 2),
        "min_symbols_day": int(cov.min()),
        "max_symbols_day": int(cov.max()),
        "pct_days_lt5": round(float(100 * (cov < 5).mean()), 1),
        "pct_days_lt3": round(float(100 * (cov < 3).mean()), 1),
        "n_symbols": int(sig["symbol"].nunique()),
        "ts_start": str(pd.Timestamp(sig["ts"].min()).date()),
        "ts_end": str(pd.Timestamp(sig["ts"].max()).date()),
        # 2026 尾部（>v2 ts_end 2026-06-11）：P6-4 数据补齐的直接受益段
        "n_sig_2026_tail": int(len(tail)),
        "n_days_2026_tail": int(len(tail_cov)),
        "avg_sym_2026_tail": round(float(tail_cov.mean()), 2) if len(tail_cov) else np.nan,
    }
    for y in range(2019, 2027):
        row[f"avg_sym_{y}"] = years.get(str(y), np.nan)
    return pd.DataFrame([row])


def run_coverage_compare() -> None:
    print("-" * 72)
    print("[Stage 2] 覆盖率对比 v2 vs v4")
    df = pd.concat([coverage_stats(V2_PATH), coverage_stats(V4_PATH)], ignore_index=True)
    ART.mkdir(exist_ok=True)
    df.to_csv(COV_CSV, index=False)
    cols = ["cache", "n_signals", "n_days", "avg_symbols_per_day", "min_symbols_day",
            "max_symbols_day", "pct_days_lt5", "pct_days_lt3", "n_symbols", "ts_start", "ts_end"]
    print(df[cols].to_string(index=False))
    print("\n  各年日均品种：")
    ycols = [f"avg_sym_{y}" for y in range(2019, 2027)]
    print(df[["cache"] + ycols].to_string(index=False))
    print("\n  2026 尾部（> 2026-06-11，P6-4 补齐段）:")
    print(df[["cache", "n_sig_2026_tail", "n_days_2026_tail", "avg_sym_2026_tail"]].to_string(index=False))
    print(f"\n  [OK] → {COV_CSV}")

    v2 = df[df["cache"] == "v2"].iloc[0]
    v4 = df[df["cache"] == "v4"].iloc[0]
    print(f"  覆盖率对比：v4 行数 {v2['n_signals']} → {v4['n_signals']} "
          f"({100 * v4['n_signals'] / v2['n_signals']:.0f}%) | "
          f"天数 {v2['n_days']} → {v4['n_days']} | "
          f"2026 日均品种 {v2['avg_sym_2026']} → {v4['avg_sym_2026']} | "
          f"2026 尾部日均品种 {v2['avg_sym_2026_tail']} → {v4['avg_sym_2026_tail']}")


# ---------------------------------------------------------------------------
# Stage 3：引擎 A 单引擎 + 组合重估（v2 vs v4）
# ---------------------------------------------------------------------------
def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def run_eval() -> None:
    from hexbroker.backtest.cost import CostModel
    from hexbroker.config import load_config
    from scripts.build_signals18 import CONTRACTS18
    from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
    from scripts.p3_combo_backtest import engine_b_targets
    from scripts.p5_engineA_cross_section import (
        combo_stats_row,
        engine_a_targets_cs,
        run_engine_row,
        seg_sharpe,
    )

    print("-" * 72)
    print("[Stage 3] 引擎 A 单引擎 + 组合重估（v2 vs v4 缓存）")
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"  prices: {len(prices)} 行 | 口径: 滑点1tick+费0.005%+保证金12%+CONTRACTS18")

    # ---- 引擎 B 基准（与缓存无关） ----
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    rows: list[dict] = []
    for cache_label, cache_path in (("v2", V2_PATH), ("v4", V4_PATH)):
        print(f"\n  --- cache={cache_label} ({cache_path.name}) ---")
        # 单引擎 A：S1(min=None) + S2(min=3，P5 终裁策略)
        for sname, mn in (("S1", None), ("S2", MIN_SYMBOLS_S2)):
            tgt_a = engine_a_targets_cs(prices, TOP_K, mn, cache_path=cache_path)
            ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt_a, f"A-{sname}")
            n_long = int((tgt_a["target"] > 0).sum())
            seg1 = seg_sharpe(eq, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
            seg2 = seg_sharpe(eq, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
            rows.append({
                "cache": cache_label, "kind": "engineA_single", "config": sname,
                "min_symbols": mn if mn is not None else "None",
                "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
                "maxdd_full": m.max_drawdown,
                "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
                "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
                "oos_ret": m_oos.total_return if m_oos else np.nan,
                "oos_seg1_sharpe": seg1, "oos_seg2_sharpe": seg2,
                "long_day_ratio": long_ratio, "n_long_rows": n_long,
                "oos_n": m_oos.n_bars if m_oos else 0,
            })
            print(f"    A-{sname}(min={mn}): Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
                  f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={m_oos.sharpe:.3f} "
                  f"OOS MaxDD={m_oos.max_drawdown*100:.1f}% | 做多行={n_long}")

        # 组合：生产配置 A15/B85 + A25/B75（vol_target 均按生产 False，另附 True 参考）
        tgt_a_s2 = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cache_path)
        ret_a_s2 = run_engine_row(cfg, cost, prices, tgt_a_s2, "A-S2")[0]
        for w_a, label in ((PROD_W_A, "prod_A15B85"), (A25, "A25B75")):
            for vt in (False, True):
                ra, rb = _align(ret_a_s2, ret_b)
                r = combo_stats_row(ra, rb, w_a, vt)
                rows.append({
                    "cache": cache_label, "kind": "combo", "config": label,
                    "min_symbols": MIN_SYMBOLS_S2,
                    "w_a": w_a, "w_b": round(1.0 - w_a, 4), "vol_target": vt,
                    "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
                    "maxdd_full": r["maxdd_full"], "oos_sharpe": r["oos_sharpe"],
                    "oos_maxdd": r["oos_maxdd"], "oos_ret": r["oos_ret"],
                    "oos_seg1_sharpe": np.nan, "oos_seg2_sharpe": np.nan,
                    "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r["oos_n"],
                })
                print(f"    combo {label} A{w_a:.2f}/B{1-w_a:.2f} vol={'Y' if vt else 'N'}: "
                      f"Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f}")

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
    print("\n  === v2 vs v4 对比摘要（引擎 A 单引擎 + 组合） ===")
    pivot = df[df["cache"].isin(["v2", "v4"])].copy()
    for _, r in pivot[pivot["kind"] == "engineA_single"].iterrows():
        print(f"    [{r['cache']}] 引擎A {r['config']} (min={r['min_symbols']}): "
              f"全样本 {r['sharpe_full']:.3f} | OOS {r['oos_sharpe']:.3f} "
              f"(seg1 {r['oos_seg1_sharpe']:.3f} / seg2 {r['oos_seg2_sharpe']:.3f})")
    for _, r in pivot[pivot["kind"] == "combo"].iterrows():
        if r["vol_target"]:
            continue
        print(f"    [{r['cache']}] combo A{r['w_a']:.2f}/B{r['w_b']:.2f} (vol=N): "
              f"全样本 {r['sharpe_full']:.3f} | OOS {r['oos_sharpe']:.3f}")
    print(f"    [ref] 纯 B 参考: 全样本 {r_pure['sharpe_full']:.3f} | OOS {r_pure['oos_sharpe']:.3f}")

    # ---- 关键判断 ----
    v2_s2 = pivot[(pivot["kind"] == "engineA_single") & (pivot["config"] == "S2")].set_index("cache")
    v4_s2 = v2_s2.loc["v4"]
    d_sh = v4_s2["oos_sharpe"] - v2_s2.loc["v2", "oos_sharpe"]
    print(f"\n  === 关键判断：引擎 A S2 OOS Sharpe 变化 {d_sh:+.3f} "
          f"({v2_s2.loc['v2','oos_sharpe']:.3f} → {v4_s2['oos_sharpe']:.3f}) ===")
    print(f"  数据补齐 {'改善' if d_sh > 0 else '未改善'}引擎 A OOS —— "
          f"{'说明覆盖率提升带来增益' if d_sh > 0 else '引擎 A 瓶颈在信号质量而非覆盖率（v4 未采纳理由）'}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P6-3b 数据补齐后重建 v4 信号缓存 + 引擎 A 重估")
    ap.add_argument("--skip-rebuild", action="store_true", help="跳过 Stage 1（v4 已生成）")
    ap.add_argument("--coverage-only", action="store_true", help="仅 Stage 2 覆盖率对比")
    ap.add_argument("--eval-only", action="store_true", help="仅 Stage 3 引擎 A/组合重估")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="强制重跑 Stage 1")
    args = ap.parse_args()

    print("=" * 72)
    print("P6-3b 数据补齐后重建信号缓存（v4）+ 引擎 A 重估")
    print("=" * 72)

    if args.coverage_only:
        run_coverage_compare()
        return
    if args.eval_only:
        run_eval()
        return

    if not args.skip_rebuild:
        run_rebuild(args.n_jobs, args.force)
    run_coverage_compare()
    run_eval()

    print("=" * 72)
    print("[DONE] P6-3b 全流程完成")
    print(f"  覆盖率对比: {COV_CSV}")
    print(f"  引擎 A/组合: {EVAL_CSV}")
    print("=" * 72)


if __name__ == "__main__":
    main()
