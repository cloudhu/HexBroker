#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P24-2 rt30 正式升级评估：当前生产口径下 rt30 vs v8 完整对比 + 块自助检验。

背景
----
P23 QA L3 升级项：rt30 候选（signals_cache18_grouped_v2_rt30.parquet，更频繁重训
test_len=30）在 fresh K 线下 p12 monitor 显示 UPGRADE_TRIGGER（IC 连续胜 7/5）——
按 P12 规则正式升级为 QA + 主理人终裁评估议题。本脚本按 P7 先例 + 当前生产口径
（P19 固化：v8 缓存 + top30% + S2 min=3 + cap 0.5；引擎 B win252/thr0.7 + 名义
0.30；组合 A30/B70）完整评估：

  1. 覆盖率对比（v8 vs rt30）：行数/日期/日均品种/OOS 段/2026-07~08 尾部信号差异
  2. 完整回测对比（生产 cap 口径，显式 base.yaml + 显式传参 group_cap=0.5/
     group_map；修复后 broker；复利口径 OOS 2024-07-18 后）：
       - 引擎 A S2 单引擎：v8（基线 OOS 1.064）vs rt30
       - 组合 A30/B70：v8 vs rt30
  3. QA 块自助检验（P7 先例方法）：rt30 vs v8 的 OOS 日收益差做块自助
     （block≈21，B≥2000）→ p 值（rt30 是否显著优于 v8）
  4. 升级裁决建议

口径铁律：复利口径；OOS 2024-07-18 后；修复后 broker（完整回测滑点1tick+费
0.005%+保证金12%+CONTRACTS18）；生产 cap 口径（load_config("configs/base.yaml")
+ 显式传参 group_cap=0.5/group_map）；嵌套零泄漏（块自助只比较 OOS 段）。
不改 hexbroker 包、不改 v8/rt30 缓存。

用法
----
  python scripts/p24_rt30_upgrade_eval.py
  python scripts/p24_rt30_upgrade_eval.py --bootstrap 2000 --block 21
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

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

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
RT30_PATH = ART / "signals_cache18_grouped_v2_rt30.parquet"
COMPARE_CSV = ART / "p24_rt30_compare.csv"
BOOT_JSON = ART / "p24_rt30_bootstrap.json"

OOS_START = "2024-07-18"
OOS_SUB_BOUNDS = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]


# ---------------------------------------------------------------------------
# 1. 覆盖率对比
# ---------------------------------------------------------------------------
def coverage_table() -> pd.DataFrame:
    """v8 vs rt30 覆盖率对比（全样本 + OOS + 2026-07~08 尾部信号差异）。"""
    rows: list[dict] = []
    for label, path in (("v8", V8_PATH), ("rt30", RT30_PATH)):
        sig = pd.read_parquet(path)
        sig["ts"] = pd.to_datetime(sig["ts"])
        cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()
        oos = sig[sig["ts"] >= pd.Timestamp(OOS_START)]
        oos_cov = oos.groupby(oos["ts"].dt.date)["symbol"].count()
        jul = sig[(sig["ts"] >= "2026-07-01") & (sig["ts"] < "2026-08-01")]
        aug = sig[sig["ts"] >= "2026-08-01"]
        # S2 资格日：OOS 内当日品种数 >=3 的天数（引擎 A S2 有 targets 的天数）
        s2_eligible_oos = int((oos_cov >= 3).sum())
        rows.append({
            "cache": label,
            "n_rows": int(len(sig)),
            "n_dates": int(cov.shape[0]),
            "ts_start": str(pd.Timestamp(sig["ts"].min()).date()),
            "ts_end": str(pd.Timestamp(sig["ts"].max()).date()),
            "avg_symbols_per_day": round(float(cov.mean()), 3),
            "n_rows_oos": int(len(oos)),
            "n_dates_oos": int(oos_cov.shape[0]),
            "avg_symbols_oos": round(float(oos_cov.mean()), 3),
            "s2_eligible_days_oos": s2_eligible_oos,
            "n_rows_2026_07": int(len(jul)),
            "n_dates_2026_07": int(jul["ts"].dt.date.nunique()) if len(jul) else 0,
            "syms_2026_07": sorted(jul["symbol"].unique()),
            "n_rows_2026_08": int(len(aug)),
            "n_dates_2026_08": int(aug["ts"].dt.date.nunique()) if len(aug) else 0,
            "syms_2026_08": sorted(aug["symbol"].unique()),
        })
    return pd.DataFrame(rows)


def tail_signal_diff() -> dict:
    """OOS 尾部（2026-07-01 后）信号差异：两缓存各自覆盖的品种/日期。"""
    out: dict = {}
    for label, path in (("v8", V8_PATH), ("rt30", RT30_PATH)):
        sig = pd.read_parquet(path)
        sig["ts"] = pd.to_datetime(sig["ts"])
        tail = sig[sig["ts"] >= "2026-07-01"]
        per_sym = tail.groupby("symbol")["ts"].agg(["min", "max", "count"])
        out[label] = {
            "n_rows_tail": int(len(tail)),
            "n_dates_tail": int(tail["ts"].dt.date.nunique()),
            "symbols": sorted(tail["symbol"].unique()),
            "per_symbol": {
                s: {
                    "first": str(r["min"].date()),
                    "last": str(r["max"].date()),
                    "n": int(r["count"]),
                }
                for s, r in per_sym.iterrows()
            },
        }
    return out


# ---------------------------------------------------------------------------
# 2. 完整回测对比（生产 cap 口径）
# ---------------------------------------------------------------------------
def run_full_comparison(cfg, cost, prices) -> tuple[pd.DataFrame, dict]:
    """引擎 A S2 单引擎 + 组合 A30/B70（v8 vs rt30）+ 纯 B 参考。

    返回 (rows_df, 各缓存日收益/权益曲线 dict)。
    """
    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    combo = cfg.backtest.combo

    tgt_b = engine_b_targets(prices, win=eb.win, thr=eb.thr)
    ret_b, eq_b, m_b, m_oos_b, _ = run_engine_row(cfg, cost, prices, tgt_b, "B")

    rows: list[dict] = []
    curves: dict[str, dict] = {}

    for label, path in (("v8", V8_PATH), ("rt30", RT30_PATH)):
        tgt_a = engine_a_targets_cs(
            prices, top_k=ea.top_k, min_symbols=ea.min_symbols,
            cache_path=path, group_cap=ea.group_cap,
            group_map=ea.group_map, score_col="exp_ret",
        )
        ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(
            cfg, cost, prices, tgt_a, f"A-S2-{label}"
        )
        seg1 = seg_sharpe(eq_a, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
        seg2 = seg_sharpe(eq_a, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
        n_long = int((tgt_a["target"] > 0).sum())
        rows.append({
            "cache": label, "kind": "engineA_single", "config": "S2",
            "min_symbols": 3,
            "sharpe_full": m_a.sharpe, "ann_ret_full": m_a.annual_return,
            "maxdd_full": m_a.max_drawdown,
            "oos_sharpe": m_oos_a.sharpe if m_oos_a else np.nan,
            "oos_maxdd": m_oos_a.max_drawdown if m_oos_a else np.nan,
            "oos_ret": m_oos_a.total_return if m_oos_a else np.nan,
            "oos_seg1_sharpe": seg1, "oos_seg2_sharpe": seg2,
            "long_day_ratio": long_ratio, "n_long_rows": n_long,
            "oos_n": m_oos_a.n_bars if m_oos_a else 0,
        })
        curves[label] = {"ret_a": ret_a, "eq_a": eq_a, "ret_b": ret_b, "eq_b": eq_b}

        common = ret_a.index.intersection(ret_b.index)
        ra, rb = ret_a.loc[common], ret_b.loc[common]
        r = combo_stats_row(ra, rb, float(combo.w_engine_a), bool(combo.vol_target))
        rows.append({
            "cache": label, "kind": "combo",
            "config": f"A{combo.w_engine_a:.0f}B{combo.w_engine_b:.0f}",
            "min_symbols": 3,
            "w_a": r["w_a"], "w_b": r["w_b"], "vol_target": r["vol_target"],
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"], "oos_sharpe": r["oos_sharpe"],
            "oos_maxdd": r["oos_maxdd"], "oos_ret": r["oos_ret"],
            "oos_seg1_sharpe": np.nan, "oos_seg2_sharpe": np.nan,
            "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r["oos_n"],
        })

    # 组合日收益曲线（A30/B70，同 combo_stats_row 口径）
    for label in ("v8", "rt30"):
        ra = curves[label]["ret_a"]
        rb = curves[label]["ret_b"]
        common = ra.index.intersection(rb.index)
        combo_ret = float(combo.w_engine_a) * ra.loc[common] + float(combo.w_engine_b) * rb.loc[common]
        curves[label]["combo_ret"] = combo_ret

    # 纯 B 参考
    r_pure = combo_stats_row(ret_b, ret_b, 1.0, False)
    rows.append({
        "cache": "ref", "kind": "engineB", "config": "pureB",
        "min_symbols": "", "w_a": 1.0, "w_b": 0.0, "vol_target": False,
        "sharpe_full": r_pure["sharpe_full"], "ann_ret_full": r_pure["ann_ret_full"],
        "maxdd_full": r_pure["maxdd_full"], "oos_sharpe": r_pure["oos_sharpe"],
        "oos_maxdd": r_pure["oos_maxdd"], "oos_ret": r_pure["oos_ret"],
        "oos_seg1_sharpe": seg_sharpe(eq_b, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1]),
        "oos_seg2_sharpe": seg_sharpe(eq_b, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1]),
        "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r_pure["oos_n"],
    })

    return pd.DataFrame(rows), curves


# ---------------------------------------------------------------------------
# 3. 块自助（P7 先例方法：OOS 日收益差，block≈21，B≥2000）
# ---------------------------------------------------------------------------
def moving_block_bootstrap(
    diff: np.ndarray,
    block: int = 21,
    n_boot: int = 5000,
    seed: int = 42,
) -> dict:
    """移动块自助：对日收益差序列按 block 长度切块重采样，估计均值差分布。

    返回 {obs_mean, se, t_stat, p_one_sided, p_two_sided, ci95, n, block, n_boot}。

    p_one_sided = P(boot_mean <= 0)（H0: 均值<=0，Ha: rt30 优于 v8 的单侧 p 值）；
    p_two_sided = P(|boot_mean - obs_mean| >= |obs_mean|)（双侧）。
    """
    n = int(len(diff))
    if n == 0:
        raise ValueError("空序列无法自助")
    block = int(max(1, min(block, n)))
    n_blocks = int(np.ceil(n / block))
    max_start = n - block
    rng = np.random.default_rng(seed)
    obs_mean = float(np.mean(diff))

    boot_means = np.empty(n_boot)
    starts = rng.integers(0, max_start + 1, size=(n_boot, n_blocks))
    for b in range(n_boot):
        idx = []
        for s in starts[b]:
            idx.extend(range(s, s + block))
        idx = np.asarray(idx[:n], dtype=int)
        boot_means[b] = float(np.mean(diff[idx]))

    se = float(np.std(boot_means, ddof=1))
    t_stat = float(obs_mean / se) if se > 1e-15 else float("nan")
    p_one_sided = float(np.mean(boot_means <= 0.0))
    p_two_sided = float(np.mean(np.abs(boot_means - obs_mean) >= abs(obs_mean)))
    ci_lo, ci_hi = np.percentile(boot_means, [2.5, 97.5])
    return {
        "obs_mean": obs_mean,
        "se": se,
        "t_stat": t_stat,
        "p_one_sided": p_one_sided,
        "p_two_sided": p_two_sided,
        "ci95": [float(ci_lo), float(ci_hi)],
        "n": n,
        "block": block,
        "n_boot": int(n_boot),
        "seed": seed,
    }


def _oos_rets(ret: pd.Series) -> pd.Series:
    """OOS 段日收益（2024-07-18 后）。"""
    idx = pd.to_datetime(ret.index)
    return ret[idx >= pd.Timestamp(OOS_START)]


def rolling_sharpe_win_rate(eq_rt: pd.Series, eq_v8: pd.Series, win: int = 60) -> dict:
    """OOS 段滚动 win 日 Sharpe 胜率（P7 补充指标）。"""
    def _roll(eq: pd.Series) -> pd.Series:
        idx = pd.to_datetime(eq.index)
        eq_oos = eq[idx >= pd.Timestamp(OOS_START)]
        ret = eq_oos.pct_change().dropna()
        m = ret.rolling(win, min_periods=win).mean() / ret.rolling(
            win, min_periods=win
        ).std() * np.sqrt(252)
        return m.dropna()

    r_rt = _roll(eq_rt)
    r_v8 = _roll(eq_v8)
    common = r_rt.index.intersection(r_v8.index)
    if len(common) == 0:
        return {"n_windows": 0, "win_rate": float("nan")}
    rr, rv = r_rt.loc[common], r_v8.loc[common]
    return {"n_windows": int(len(common)), "win_rate": float((rr > rv).mean())}


def run_bootstrap(curves: dict, block: int, n_boot: int, seed: int = 42) -> dict:
    """对引擎 A 单引擎与组合 A30/B70 的 OOS 日收益差做块自助。"""
    result: dict = {}
    for kind, rt_key, v8_key in (
        ("engineA_single", "ret_a", "ret_a"),
        ("combo", "combo_ret", "combo_ret"),
    ):
        ret_rt = _oos_rets(curves["rt30"][rt_key])
        ret_v8 = _oos_rets(curves["v8"][v8_key])
        common = ret_rt.index.intersection(ret_v8.index)
        if len(common) == 0:
            result[kind] = {"error": "无共同 OOS 日期"}
            continue
        rr, rv = ret_rt.loc[common].sort_index(), ret_v8.loc[common].sort_index()
        diff = (rr - rv).to_numpy(dtype=float)
        boot = moving_block_bootstrap(diff, block=block, n_boot=n_boot, seed=seed)
        boot["mean_diff_rt_minus_v8_per_day_pct"] = boot["obs_mean"] * 100.0
        boot["mean_diff_v8_per_day_pct"] = float(np.mean(rv.to_numpy())) * 100.0
        boot["mean_diff_rt_per_day_pct"] = float(np.mean(rr.to_numpy())) * 100.0
        boot["std_rt_per_day_pct"] = float(np.std(rr.to_numpy())) * 100.0
        boot["std_v8_per_day_pct"] = float(np.std(rv.to_numpy())) * 100.0
        result[kind] = boot
    return result


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P24-2 rt30 正式升级评估（rt30 vs v8）")
    ap.add_argument("--block", type=int, default=21, help="块自助块长（默认 21）")
    ap.add_argument("--bootstrap", type=int, default=5000, help="块自助抽样次数（默认 5000，>=2000）")
    ap.add_argument("--seed", type=int, default=42, help="块自助随机种子")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 96)
    print("P24-2 rt30 正式升级评估：当前生产口径下 rt30 vs v8")
    print("=" * 96)
    print(f"  口径: 显式 configs/base.yaml + 显式传参 group_cap=0.5/group_map | "
          f"修复后 broker | OOS {OOS_START} 后 | 复利口径")
    print(f"  块自助: block={args.block}, B={args.bootstrap}, seed={args.seed}")

    # ---- [1] 覆盖率 ----
    print("\n[1/4] 覆盖率对比 ...")
    cov = coverage_table()
    tail = tail_signal_diff()
    for _, r in cov.iterrows():
        print(f"  [{r['cache']:<4}] {r['n_rows']} 行 / {r['n_dates']} 日 | "
              f"{r['ts_start']} ~ {r['ts_end']} | 日均 {r['avg_symbols_per_day']:.2f} 品种 | "
              f"OOS {r['n_dates_oos']} 日 / 日均 {r['avg_symbols_oos']:.2f} | "
              f"S2 资格日(OOS) {r['s2_eligible_days_oos']}")
        print(f"      2026-07: {r['n_rows_2026_07']} 行 / {r['n_dates_2026_07']} 日 "
              f"品种 {r['syms_2026_07']}")
        print(f"      2026-08: {r['n_rows_2026_08']} 行 / {r['n_dates_2026_08']} 日 "
              f"品种 {r['syms_2026_08']}")
    print(f"  尾部信号(2026-07 后): v8 {tail['v8']['n_rows_tail']} 行/"
          f"{tail['v8']['n_dates_tail']} 日 品种 {tail['v8']['symbols']} | "
          f"rt30 {tail['rt30']['n_rows_tail']} 行/{tail['rt30']['n_dates_tail']} 日 "
          f"品种 {tail['rt30']['symbols']}")

    # ---- [2] 完整回测 ----
    print("\n[2/4] 完整回测对比（生产 cap 口径）...")
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    rows, curves = run_full_comparison(cfg, cost, prices)
    ART.mkdir(exist_ok=True)
    rows.to_csv(COMPARE_CSV, index=False)

    print(f"    {'kind':<16}{'cache':<6}{'全样本Sh':>10}{'OOS Sh':>9}{'OOS ret':>9}"
          f"{'OOS seg1':>9}{'OOS seg2':>9}{'OOS n':>7}")
    for _, r in rows.iterrows():
        seg1 = r["oos_seg1_sharpe"] if pd.notna(r["oos_seg1_sharpe"]) else np.nan
        seg2 = r["oos_seg2_sharpe"] if pd.notna(r["oos_seg2_sharpe"]) else np.nan
        fmt = lambda v: f"{v:.3f}" if pd.notna(v) else "  n/a"
        print(f"    {r['kind']:<16}{r['cache']:<6}{fmt(r['sharpe_full']):>10}"
              f"{fmt(r['oos_sharpe']):>9}{fmt(r['oos_ret']):>9}"
              f"{fmt(seg1):>9}{fmt(seg2):>9}{int(r['oos_n']):>7}")
    print(f"  [OK] 对比表 → {COMPARE_CSV}")

    # ---- [3] 块自助 ----
    print(f"\n[3/4] 块自助检验（block={args.block}, B={args.bootstrap}）...")
    boot = run_bootstrap(curves, args.block, args.bootstrap, args.seed)
    for kind in ("engineA_single", "combo"):
        b = boot[kind]
        if "error" in b:
            print(f"  [{kind:<15}] {b['error']}")
            continue
        print(f"  [{kind:<15}] 均值日差(rt30−v8)={b['obs_mean']*100:+.4f}%/d | "
              f"se={b['se']*100:.4f} | t={b['t_stat']:.2f} | "
              f"p_one_sided={b['p_one_sided']:.3f} | p_two_sided={b['p_two_sided']:.3f} | "
              f"95%CI [{b['ci95'][0]*100:+.4f}, {b['ci95'][1]*100:+.4f}]%/d | n={b['n']}")
        print(f"         rt30 日波动 {b['std_rt_per_day_pct']:.3f}%/d vs "
              f"v8 {b['std_v8_per_day_pct']:.3f}%/d")

    # 滚动 Sharpe 胜率（补充）
    wr = rolling_sharpe_win_rate(curves["rt30"]["eq_a"], curves["v8"]["eq_a"])
    print(f"  [引擎A 滚动60日Sharpe胜率] rt30 胜 {wr['win_rate']*100:.1f}% 窗口 "
          f"({wr['n_windows']} 窗)")

    # ---- [4] 裁决 ----
    print("\n[4/4] 升级裁决建议 ...")
    verdict = build_verdict(rows, boot)
    verdict["rolling_60d_sharpe_win_rate_rt30_vs_v8"] = wr
    BOOT_JSON.write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"  [OK] 完整裁决（含块自助）→ {BOOT_JSON}")
    print(json.dumps({k: v for k, v in verdict.items() if k != "bootstrap"},
                     ensure_ascii=False, indent=2, default=str))
    print(f"\n[P24-2 DONE] 总耗时 {time.time() - t0:.1f}s")


def build_verdict(rows: pd.DataFrame, boot: dict) -> dict:
    """根据对比表 + 块自助结果给出升级裁决建议。"""
    s2 = rows[(rows["kind"] == "engineA_single") & (rows["config"] == "S2")].set_index("cache")
    cb = rows[rows["kind"] == "combo"].set_index("cache")

    out: dict = {
        "engineA_s2_oos_sharpe_v8": float(s2.loc["v8", "oos_sharpe"]),
        "engineA_s2_oos_sharpe_rt30": float(s2.loc["rt30", "oos_sharpe"]),
        "engineA_s2_oos_ret_v8": float(s2.loc["v8", "oos_ret"]),
        "engineA_s2_oos_ret_rt30": float(s2.loc["rt30", "oos_ret"]),
        "combo_oos_sharpe_v8": float(cb.loc["v8", "oos_sharpe"]),
        "combo_oos_sharpe_rt30": float(cb.loc["rt30", "oos_sharpe"]),
        "combo_oos_ret_v8": float(cb.loc["v8", "oos_ret"]),
        "combo_oos_ret_rt30": float(cb.loc["rt30", "oos_ret"]),
        "bootstrap": boot,
    }

    # 统计显著性：引擎 A 单引擎 OOS 均值日差块自助 p 值（主判据，P7 先例）
    b_a = boot.get("engineA_single", {})
    b_c = boot.get("combo", {})
    p_a = b_a.get("p_one_sided", float("nan"))
    p_c = b_c.get("p_one_sided", float("nan"))
    delta_a = out["engineA_s2_oos_sharpe_rt30"] - out["engineA_s2_oos_sharpe_v8"]
    delta_c = out["combo_oos_sharpe_rt30"] - out["combo_oos_sharpe_v8"]

    sig = p_a < 0.05
    better = delta_a > 0 and delta_c >= 0
    if sig and better:
        rec = (
            "建议升级为生产候选（需主理人终裁）：rt30 显著优于 v8（块自助 p<0.05）"
            "且引擎 A/组合 OOS 均改善"
        )
    elif not sig and better:
        rec = (
            "维持 v8，登记'rt30 评估不通过（当前口径，改善不显著）'：方向为正但"
            "块自助 p 不显著（P7 先例同款结论）——仅影子候选，不投产"
        )
    else:
        rec = (
            "维持 v8，登记'rt30 评估不通过（当前口径，未改善/更差）'："
            "rt30 在引擎 A/组合 OOS 上未优于 v8"
        )
    out["recommendation"] = rec
    out["significance"] = {
        "engineA_bootstrap_p_one_sided": p_a,
        "combo_bootstrap_p_one_sided": p_c,
        "engineA_oos_sharpe_delta": delta_a,
        "combo_oos_sharpe_delta": delta_c,
        "statistically_significant": bool(sig),
        "direction_positive": bool(better),
    }
    return out


if __name__ == "__main__":
    main()
