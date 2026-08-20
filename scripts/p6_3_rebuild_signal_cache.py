"""P6-3 信号缓存重建（覆盖率翻倍）+ 引擎 A 重估编排脚本。

背景
----
P6-2 诊断确认信号覆盖率三重根因，其中结构性截断：
  ``walk_forward_lightgbm`` 每折测试窗 60 天 → ``_calibrate_and_split`` 在
  ``cal_split=0.5`` 时只输出测试窗后半（~16 天/折），覆盖率仅 ~25%（976 天）。

方案 A（QA 已审查）：**「校准后返回全部信号」模式**——用测试窗前 50% 拟合校准器、
对全部信号应用校准并返回全部。覆盖率翻倍（~1900 天），交易信号零泄漏
（校准只改 p_up/is_effective 不改 exp_ret；引擎 A 排序只用 exp_ret.rank）。

本脚本编排（可按阶段执行，重建耗时长建议后台跑）：
  Stage 0  验证 _calibrate_and_split 新模式（小数据断言 + pytest）
  Stage 1  触发重建：python scripts/group_modeling_v2.py --cal-return-all
           --output artifacts/signals_cache18_grouped_v3.parquet --n-jobs 6
  Stage 2  覆盖率对比 v2 vs v3 → artifacts/p6_3_cache_coverage.csv
  Stage 3  引擎 A 单引擎 + 组合重估（v2 vs v3）→ artifacts/p6_3_engineA_compare.csv

用法
----
  python scripts/p6_3_rebuild_signal_cache.py                 # 全流程
  python scripts/p6_3_rebuild_signal_cache.py --verify-only   # 仅 Stage 0
  python scripts/p6_3_rebuild_signal_cache.py --skip-rebuild  # 重建已完成后跑 2/3
  python scripts/p6_3_rebuild_signal_cache.py --coverage-only # 仅 Stage 2
  python scripts/p6_3_rebuild_signal_cache.py --eval-only     # 仅 Stage 3

口径铁律：OOS 2024-07-18 后；嵌套零泄漏（校准器拟合只用测试窗前段）；品种内时序 IC；
完整回测（滑点1tick + 费0.005% + 保证金12% + CONTRACTS18）。
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
V3_PATH = ART / "signals_cache18_grouped_v3.parquet"
COV_CSV = ART / "p6_3_cache_coverage.csv"
EVAL_CSV = ART / "p6_3_engineA_compare.csv"

# 生产配置（config.py 固化：EngineBConfig + ComboConfig）
PROD_W_A = 0.15
PROD_W_B = 0.85
PROD_VOL_TARGET = False
A25 = 0.25

# 口径常量（与 p3/p5 一致）
TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
OOS_START = "2024-07-18"
IS_END = "2022-04-21"
OOS_SUB_BOUNDS = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]
MIN_SYMBOLS_S2 = 3  # P5 终裁选中策略：截面 rank + min=3


# ---------------------------------------------------------------------------
# Stage 0：验证 _calibrate_and_split 新模式
# ---------------------------------------------------------------------------
class _Sig:
    def __init__(self, p_up: float = 0.6):
        self.ts = 0
        self.p_up = p_up
        self.exp_ret = 0.01
        self.is_effective = True
        self.eff_thr = 0.05


def verify_cal_return_all() -> None:
    """小数据验证：True 返回全部 / False 返回 sigs[k:] / 校准器只在前 k 拟合。"""
    from scripts.refine_lightgbm_champion import _calibrate_and_split

    print("-" * 72)
    print("[Stage 0] 验证 _calibrate_and_split 新模式")
    ok = True

    # 1) 长度行为
    n = 40
    sigs = [_Sig(p_up=0.9) for _ in range(n)]
    valid = np.ones(n, dtype=bool)
    y_true = np.concatenate([np.ones(20), np.zeros(20)])
    out_false = _calibrate_and_split(sigs, valid, y_true, "platt", 0.5, False)
    out_true = _calibrate_and_split([_Sig(p_up=0.9) for _ in range(n)], valid, y_true, "platt", 0.5, True)
    cond = len(out_false) == n - int(n * 0.5) and len(out_true) == n
    ok &= cond
    print(f"  [{'PASS' if cond else 'FAIL'}] False→{len(out_false)} 条（=sigs[k:]） | "
          f"True→{len(out_true)} 条（=全部 {n} 条）")

    # 2) 校准器只在前 k 上拟合（isotonic：前 k 全上涨 → 后段被映射为高概率）
    sigs2 = ([_Sig(p_up=0.8) for _ in range(10)] + [_Sig(p_up=0.95) for _ in range(10)]
             + [_Sig(p_up=0.9) for _ in range(20)])
    out = _calibrate_and_split(sigs2, valid, y_true, "isotonic", 0.5, True)
    cond2 = len(out) == n and out[30].p_up > 0.8
    ok &= cond2
    print(f"  [{'PASS' if cond2 else 'FAIL'}] 校准器只在前 k 拟合：后段(原y=0)被映射 p_up={out[30].p_up:.3f} (>0.8)")

    # 3) exp_ret 不变（引擎 A 排序零泄漏）
    sigs3 = [_Sig(p_up=0.9) for _ in range(n)]
    for i, s in enumerate(sigs3):
        s.exp_ret = float(i) * 0.001
    exp_before = [s.exp_ret for s in sigs3]
    _calibrate_and_split(sigs3, valid, y_true, "platt", 0.5, True)
    cond3 = [s.exp_ret for s in sigs3] == exp_before
    ok &= cond3
    print(f"  [{'PASS' if cond3 else 'FAIL'}] exp_ret 未被校准修改（零泄漏）")

    # 4) pytest（新增单测）
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_refine_lightgbm.py", "-q"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    cond4 = r.returncode == 0
    ok &= cond4
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    print(f"  [{'PASS' if cond4 else 'FAIL'}] pytest tests/test_refine_lightgbm.py → {tail}")

    print(f"  => 验证{'全部 PASS' if ok else '存在 FAIL，中止'}（返回码 {'0' if ok else '1'}）")
    if not ok:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Stage 1：触发重建（v3 缓存）
# ---------------------------------------------------------------------------
def run_rebuild(n_jobs: int) -> None:
    print("-" * 72)
    print(f"[Stage 1] 重建 v3 信号缓存（cal_return_all=True）n_jobs={n_jobs}")
    if V3_PATH.exists():
        print(f"  [SKIP] 已存在 {V3_PATH}（如需重跑请先删除）")
        return
    cmd = [
        sys.executable, "scripts/group_modeling_v2.py",
        "--n-jobs", str(n_jobs), "--cal-return-all",
        "--output", str(V3_PATH),
    ]
    print(f"  CMD: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("[FAIL] 重建退出码非 0")
        raise SystemExit(r.returncode)
    if not V3_PATH.exists():
        print(f"[FAIL] 未产出 {V3_PATH}")
        raise SystemExit(1)
    print(f"  [OK] v3 缓存已生成：{V3_PATH}")


# ---------------------------------------------------------------------------
# Stage 2：覆盖率对比
# ---------------------------------------------------------------------------
def coverage_stats(path: Path) -> pd.DataFrame:
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()
    years = {}
    for y, g in sig.groupby(sig["ts"].dt.year):
        c = g.groupby(g["ts"].dt.date)["symbol"].count()
        years[str(int(y))] = round(float(c.mean()), 2)
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
    }
    for y in range(2019, 2027):
        row[f"avg_sym_{y}"] = years.get(str(y), np.nan)
    return pd.DataFrame([row])


def run_coverage_compare() -> None:
    print("-" * 72)
    print("[Stage 2] 覆盖率对比 v2 vs v3")
    df = pd.concat([coverage_stats(V2_PATH), coverage_stats(V3_PATH)], ignore_index=True)
    ART.mkdir(exist_ok=True)
    df.to_csv(COV_CSV, index=False)
    cols = ["cache", "n_signals", "n_days", "avg_symbols_per_day", "min_symbols_day",
            "max_symbols_day", "pct_days_lt5", "pct_days_lt3", "n_symbols", "ts_start", "ts_end"]
    print(df[cols].to_string(index=False))
    print("\n  各年日均品种：")
    ycols = [f"avg_sym_{y}" for y in range(2019, 2027)]
    print(df[["cache"] + ycols].to_string(index=False))
    print(f"\n  [OK] → {COV_CSV}")

    v2 = df[df["cache"] == "v2"].iloc[0]
    v3 = df[df["cache"] == "v3"].iloc[0]
    print(f"  覆盖率翻倍验证：v3 行数 {v2['n_signals']} → {v3['n_signals']} "
          f"({100 * v3['n_signals'] / v2['n_signals']:.0f}%) | "
          f"天数 {v2['n_days']} → {v3['n_days']} ({100 * v3['n_days'] / v2['n_days']:.0f}%)")


# ---------------------------------------------------------------------------
# Stage 3：引擎 A 单引擎 + 组合重估（v2 vs v3）
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
    print("[Stage 3] 引擎 A 单引擎 + 组合重估（v2 vs v3 缓存）")
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
    for cache_label, cache_path in (("v2", V2_PATH), ("v3", V3_PATH)):
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
    print("\n  === v2 vs v3 对比摘要（引擎 A 单引擎 + 组合） ===")
    pivot = df[df["cache"].isin(["v2", "v3"])].copy()
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P6-3 信号缓存重建 + 引擎 A 重估")
    ap.add_argument("--verify-only", action="store_true", help="仅 Stage 0 验证新模式")
    ap.add_argument("--skip-rebuild", action="store_true", help="跳过 Stage 1（v3 已生成）")
    ap.add_argument("--coverage-only", action="store_true", help="仅 Stage 2 覆盖率对比")
    ap.add_argument("--eval-only", action="store_true", help="仅 Stage 3 引擎 A/组合重估")
    ap.add_argument("--n-jobs", type=int, default=6)
    args = ap.parse_args()

    print("=" * 72)
    print("P6-3 信号缓存重建（覆盖率翻倍）+ 引擎 A 重估")
    print("=" * 72)

    if args.coverage_only:
        run_coverage_compare()
        return
    if args.eval_only:
        run_eval()
        return

    verify_cal_return_all()
    if args.verify_only:
        print("[DONE] --verify-only：新模式验证通过，未触发重建。")
        return
    if not args.skip_rebuild:
        run_rebuild(args.n_jobs)
    run_coverage_compare()
    run_eval()

    print("=" * 72)
    print("[DONE] P6-3 全流程完成")
    print(f"  覆盖率对比: {COV_CSV}")
    print(f"  引擎 A/组合: {EVAL_CSV}")
    print("=" * 72)


if __name__ == "__main__":
    main()
