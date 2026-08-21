#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QA P24 fresh-eyes 独立复核脚本（严过关 / Yan）。

独立验证 P24-2 关键声明（**不调用 scripts/p24_rt30_upgrade_eval.py 的任何函数**）：
  1. 覆盖率对比（v8 vs rt30）：行数/日期/日均/OOS/S2 资格日/2026-07~08 尾部/逐品种末信号日
  2. 引擎 A S2（cap0.5 + group_map(base.yaml)）+ 引擎 B（win252/thr0.7）+ 组合 A30/B70
     → OOS Sharpe（v8 1.064 / rt30 0.544；组合 0.717 / 0.545 可复现？）
  3. 块自助（block=21, B=5000, seed=42）——**自写实现**（不调 p24 函数）
  4. 滚动 60 日 Sharpe 胜率
  5. P24-1 防重入：_should_skip_apply 判定独立复核 + legacy 迁移场景

口径：复利；OOS 2024-07-18 后；修复后 broker（BacktestEngine+CostModel）；生产 cap 口径。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
RT30_PATH = ART / "signals_cache18_grouped_v2_rt30.parquet"
V2_PATH = ART / "signals_cache18_grouped_v2.parquet"

OOS_START = "2024-07-18"
RESULTS: dict = {}


def log(k: str, ok: bool, msg: str) -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {msg}")
    RESULTS[k] = {"ok": bool(ok), "msg": msg}


def section(t: str) -> None:
    print("\n" + "=" * 88)
    print(t)
    print("=" * 88)


# ---------------------------------------------------------------------------
# 1. 覆盖率对比（独立读 parquet 直接算）
# ---------------------------------------------------------------------------
def verify_coverage() -> None:
    section("[1] 覆盖率对比（独立读 parquet）")
    cov_rows = {}
    per_sym = {}
    for label, path in (("v8", V8_PATH), ("rt30", RT30_PATH)):
        sig = pd.read_parquet(path)
        sig["ts"] = pd.to_datetime(sig["ts"])
        cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()
        oos = sig[sig["ts"] >= pd.Timestamp(OOS_START)]
        oos_cov = oos.groupby(oos["ts"].dt.date)["symbol"].count()
        jul = sig[(sig["ts"] >= "2026-07-01") & (sig["ts"] < "2026-08-01")]
        aug = sig[sig["ts"] >= "2026-08-01"]
        s2_oos = int((oos_cov >= 3).sum())
        cov_rows[label] = dict(
            n_rows=int(len(sig)), n_dates=int(cov.shape[0]),
            ts_start=str(pd.Timestamp(sig["ts"].min()).date()),
            ts_end=str(pd.Timestamp(sig["ts"].max()).date()),
            avg=round(float(cov.mean()), 3),
            oos_dates=int(oos_cov.shape[0]),
            oos_avg=round(float(oos_cov.mean()), 3),
            s2_oos=s2_oos,
            jul_rows=int(len(jul)), jul_dates=int(jul["ts"].dt.date.nunique()) if len(jul) else 0,
            jul_syms=sorted(jul["symbol"].unique()),
            aug_rows=int(len(aug)), aug_dates=int(aug["ts"].dt.date.nunique()) if len(aug) else 0,
            aug_syms=sorted(aug["symbol"].unique()),
        )
        # 逐品种末信号日
        last = sig.groupby("symbol")["ts"].max()
        per_sym[label] = {s: str(pd.Timestamp(v).date()) for s, v in last.items()}
        print(f"  [{label}] {cov_rows[label]}")

    ok_cov = (
        cov_rows["v8"]["n_rows"] == 8624 and cov_rows["v8"]["n_dates"] == 662
        and cov_rows["rt30"]["n_rows"] == 6066 and cov_rows["rt30"]["n_dates"] == 837
        and cov_rows["v8"]["s2_oos"] == 160 and cov_rows["rt30"]["s2_oos"] == 143
        and cov_rows["rt30"]["jul_rows"] == 18 and cov_rows["rt30"]["aug_rows"] == 0
        and cov_rows["v8"]["jul_rows"] == 0 and cov_rows["v8"]["aug_rows"] == 0
    )
    log("coverage_counts", ok_cov,
        f"覆盖率计数 v8 {cov_rows['v8']['n_rows']}行/{cov_rows['v8']['n_dates']}日 "
        f"rt30 {cov_rows['rt30']['n_rows']}行/{cov_rows['rt30']['n_dates']}日 | "
        f"S2资格日 {cov_rows['v8']['s2_oos']}/{cov_rows['rt30']['s2_oos']} | "
        f"07月 rt30 {cov_rows['rt30']['jul_rows']}行 品种{cov_rows['rt30']['jul_syms']} | "
        f"08月 {cov_rows['rt30']['aug_rows']}行")

    # 逐品种末信号日差异：15 品种 rt30 早 140-174 天？仅 ag0/au0/m0 延至 07-24/27？
    d_last = {}
    for s in sorted(per_sym["v8"].keys()):
        v8d = pd.Timestamp(per_sym["v8"][s])
        rtd = pd.Timestamp(per_sym["rt30"].get(s, "NaT"))
        if pd.isna(rtd):
            d_last[s] = None
        else:
            d_last[s] = (rtd - v8d).days
    earlier = {s: d for s, d in d_last.items() if d is not None and d < 0}
    later = {s: d for s, d in d_last.items() if d is not None and d > 0}
    print(f"  rt30 比 v8 早的品种数={len(earlier)}：{earlier}")
    print(f"  rt30 比 v8 晚的品种数={len(later)}：{later}")
    n_early = len(earlier)
    only_three_late = set(later.keys()) <= {"ag0", "au0", "m0"}
    ranges_ok = all(-174 <= d <= -140 for d in earlier.values())
    ok_tail = n_early == 15 and only_three_late and ranges_ok and len(later) <= 3
    log("per_symbol_tail", ok_tail,
        f"逐品种末信号日：{n_early}/18 品种 rt30 早于 v8（140~174 天区间满足={ranges_ok}）；"
        f"晚于 v8 的品种 {sorted(later.keys())}（⊆{{ag0,au0,m0}}={only_three_late}）")

    # 07 月仅 ag0/au0/m0 3 品种 18 行
    jul_syms = cov_rows["rt30"]["jul_syms"]
    ok_jul = jul_syms == ["ag0", "au0", "m0"] and cov_rows["rt30"]["jul_rows"] == 18
    log("july_tail", ok_jul, f"2026-07 rt30 信号=18行/7日 品种{jul_syms}（=3品种）")
    ok_aug = cov_rows["rt30"]["aug_rows"] == 0 and cov_rows["v8"]["aug_rows"] == 0
    log("august_tail", ok_aug, "2026-08 两缓存均 0 行（rt30 覆盖更长不成立）")


# ---------------------------------------------------------------------------
# 2. 引擎 A S2 + 引擎 B + 组合（生产 cap 口径）→ OOS Sharpe
# ---------------------------------------------------------------------------
def verify_backtest() -> dict:
    section("[2] 完整回测对比（生产 cap 口径，独立构建 targets）")
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    combo = cfg.backtest.combo
    print(f"  引擎A: top_k={ea.top_k} min={ea.min_symbols} cap={ea.group_cap} "
          f"group_map_ferrous={ea.group_map.get('i0')} | 引擎B: win={eb.win} thr={eb.thr} | "
          f"组合 A{combo.w_engine_a}/B{combo.w_engine_b} vol={combo.vol_target}")

    tgt_b = engine_b_targets(prices, win=eb.win, thr=eb.thr)
    ret_b, eq_b, _, _, _ = run_engine_row(cfg, cost, prices, tgt_b, "B")

    out: dict = {}
    for label, path in (("v8", V8_PATH), ("rt30", RT30_PATH)):
        tgt_a = engine_a_targets_cs(
            prices, top_k=ea.top_k, min_symbols=ea.min_symbols,
            cache_path=path, group_cap=ea.group_cap,
            group_map=ea.group_map, score_col="exp_ret",
        )
        ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(
            cfg, cost, prices, tgt_a, f"A-S2-{label}"
        )
        common = ret_a.index.intersection(ret_b.index)
        ra, rb = ret_a.loc[common], ret_b.loc[common]
        r = combo_stats_row(ra, rb, float(combo.w_engine_a), bool(combo.vol_target))
        out[label] = {
            "engineA_oos_sharpe": float(m_oos_a.sharpe) if m_oos_a else None,
            "engineA_oos_ret": float(m_oos_a.total_return) if m_oos_a else None,
            "engineA_full_sharpe": float(m_a.sharpe),
            "combo_oos_sharpe": float(r["oos_sharpe"]),
            "combo_oos_ret": float(r["oos_ret"]),
            "combo_full_sharpe": float(r["sharpe_full"]),
            "oos_n_a": m_oos_a.n_bars if m_oos_a else 0,
            "ret_a": ret_a, "eq_a": eq_a, "ret_b": ret_b, "eq_b": eq_b,
        }
        print(f"  [{label}] 引擎A S2: full={m_a.sharpe:.4f} OOS={m_oos_a.sharpe:.4f} "
              f"OOS ret={m_oos_a.total_return:.4f} | 组合: full={r['sharpe_full']:.4f} "
              f"OOS={r['oos_sharpe']:.4f} OOS ret={r['oos_ret']:.4f}")

    # 组合日收益（A30/B70 加权，同 combo_stats_row 口径）
    for label in ("v8", "rt30"):
        ra = out[label]["ret_a"]
        rb = out[label]["ret_b"]
        common = ra.index.intersection(rb.index)
        out[label]["combo_ret"] = (
            float(combo.w_engine_a) * ra.loc[common] + float(combo.w_engine_b) * rb.loc[common]
        )

    # 与 P24 声明对比（容忍 1e-6）
    ok_a = abs(out["v8"]["engineA_oos_sharpe"] - 1.064206885818831) < 1e-6
    ok_a_rt = abs(out["rt30"]["engineA_oos_sharpe"] - 0.5442069240426627) < 1e-6
    ok_c = abs(out["v8"]["combo_oos_sharpe"] - 0.7165489591250693) < 1e-6
    ok_c_rt = abs(out["rt30"]["combo_oos_sharpe"] - 0.544852287176174) < 1e-6
    log("backtest_v8", ok_a and ok_c,
        f"v8 基线复现：引擎A OOS={out['v8']['engineA_oos_sharpe']:.6f}（期望1.064207）| "
        f"组合={out['v8']['combo_oos_sharpe']:.6f}（期望0.716549）")
    log("backtest_rt30", ok_a_rt and ok_c_rt,
        f"rt30 复现：引擎A OOS={out['rt30']['engineA_oos_sharpe']:.6f}（期望0.544207）| "
        f"组合={out['rt30']['combo_oos_sharpe']:.6f}（期望0.544852）")
    return out


# ---------------------------------------------------------------------------
# 3. 块自助（自写实现：移动块 bootstrap，block=21, B=5000, seed=42）
# ---------------------------------------------------------------------------
def my_moving_block_bootstrap(diff: np.ndarray, block: int = 21, n_boot: int = 5000,
                              seed: int = 42) -> dict:
    n = int(len(diff))
    block = int(max(1, min(block, n)))
    n_blocks = int(np.ceil(n / block))
    max_start = n - block
    rng = np.random.default_rng(seed)
    obs_mean = float(np.mean(diff))
    # 预计算每个可能起点 block 的和
    block_sum = np.array([diff[s:s + block].sum() for s in range(max_start + 1)])
    trunc = n_blocks * block - n  # 需截断的元素数（尾块尾部）
    starts = rng.integers(0, max_start + 1, size=(n_boot, n_blocks))
    boot_means = np.empty(n_boot)
    for b in range(n_boot):
        total = float(block_sum[starts[b]].sum())
        if trunc > 0:
            s_last = int(starts[b, -1])
            total -= float(diff[s_last + block - trunc:s_last + block].sum())
        boot_means[b] = total / n
    se = float(np.std(boot_means, ddof=1))
    t_stat = float(obs_mean / se) if se > 1e-15 else float("nan")
    return {
        "obs_mean": obs_mean,
        "se": se,
        "t_stat": t_stat,
        "p_one_sided": float(np.mean(boot_means <= 0.0)),
        "p_two_sided": float(np.mean(np.abs(boot_means - obs_mean) >= abs(obs_mean))),
        "ci95": [float(np.percentile(boot_means, 2.5)), float(np.percentile(boot_means, 97.5))],
        "n": n, "block": block, "n_boot": int(n_boot), "seed": seed,
    }


def verify_bootstrap(out: dict) -> None:
    section("[3] 块自助（自写实现，block=21, B=5000, seed=42，仅 OOS 段）")
    for kind, rt_key, v8_key in (
        ("engineA_single", "ret_a", "ret_a"),
        ("combo", "combo_ret", "combo_ret"),
    ):
        idx = pd.to_datetime(out["rt30"][rt_key].index)
        rr = out["rt30"][rt_key][idx >= pd.Timestamp(OOS_START)]
        rv = out["v8"][v8_key][pd.to_datetime(out["v8"][v8_key].index) >= pd.Timestamp(OOS_START)]
        common = rr.index.intersection(rv.index)
        rr, rv = rr.loc[common].sort_index(), rv.loc[common].sort_index()
        diff = (rr - rv).to_numpy(dtype=float)
        b = my_moving_block_bootstrap(diff, block=21, n_boot=5000, seed=42)
        b["mean_diff_pct"] = b["obs_mean"] * 100.0
        print(f"  [{kind}] 均值日差={b['obs_mean']*100:+.4f}%/d se={b['se']*100:.4f} "
              f"t={b['t_stat']:.2f} p_one={b['p_one_sided']:.4f} p_two={b['p_two_sided']:.4f} "
              f"95%CI [{b['ci95'][0]*100:+.4f},{b['ci95'][1]*100:+.4f}] n={b['n']}")
        if kind == "engineA_single":
            ok = (abs(b["obs_mean"] - (-0.00010450518560237165)) < 1e-12
                  and abs(b["p_one_sided"] - 0.7574) < 0.002
                  and abs(b["p_two_sided"] - 0.4674) < 0.002
                  and b["n"] == 506)
            log("bootstrap_engineA", ok,
                f"引擎A 块自助复现：obs_mean={b['obs_mean']:.6e}（期望-1.045e-4）p_one={b['p_one_sided']:.4f}（期望0.7574）")
        else:
            ok = (abs(b["obs_mean"] - (-3.135155568071152e-05)) < 1e-12
                  and abs(b["p_one_sided"] - 0.7574) < 0.002)
            log("bootstrap_combo", ok,
                f"组合 块自助复现：obs_mean={b['obs_mean']:.6e} p_one={b['p_one_sided']:.4f}")


def verify_rolling_sharpe(out: dict) -> None:
    section("[4] 滚动 60 日 Sharpe 胜率")
    def _roll(eq: pd.Series) -> pd.Series:
        idx = pd.to_datetime(eq.index)
        eq_oos = eq[idx >= pd.Timestamp(OOS_START)]
        ret = eq_oos.pct_change().dropna()
        return (ret.rolling(60, min_periods=60).mean() / ret.rolling(60, min_periods=60).std() * np.sqrt(252)).dropna()
    r_rt = _roll(out["rt30"]["eq_a"])
    r_v8 = _roll(out["v8"]["eq_a"])
    common = r_rt.index.intersection(r_v8.index)
    wr = float((r_rt.loc[common] > r_v8.loc[common]).mean())
    print(f"  rt30 胜率={wr*100:.2f}% ({len(common)} 窗) 期望 43.5% (446 窗)")
    ok = abs(wr - 0.4349775784753363) < 0.005 and len(common) == 446
    log("rolling_winrate", ok, f"滚动60日Sharpe胜率 rt30={wr*100:.2f}%（期望43.50%/446窗）")


# ---------------------------------------------------------------------------
# 5. P24-1 防重入独立复核
# ---------------------------------------------------------------------------
def verify_apply_logic() -> None:
    section("[5] P24-1 防重入逻辑独立复核")
    import scripts.p23_daily_run as p23
    fp = "abc123"
    cases = [
        ("首次", None, fp, False, False),
        ("applied+同fp→跳过", {"state": "applied", "plan_fp": fp, "applied_at": "t0"}, fp, False, True),
        ("pending+同fp→重试", {"state": "pending", "plan_fp": fp, "applied_at": "t0"}, fp, False, False),
        ("legacy无state→保守跳过", {"plan_fp": fp, "applied_at": "t0"}, fp, False, True),
        ("applied+异fp→重入", {"state": "applied", "plan_fp": "old"}, fp, False, False),
        ("force→强制", {"state": "applied", "plan_fp": fp}, fp, True, False),
        ("未知state→允许重试", {"state": "weird", "plan_fp": fp}, fp, False, False),
    ]
    fails = 0
    for name, prev, f, force, exp in cases:
        skip, reason = p23._should_skip_apply(prev, f, force)
        ok = skip == exp
        fails += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<24} skip={skip} expect={exp} | {reason}")
    # legacy 迁移独立断言
    legacy = p23._legacy_state({"plan_fp": "x"})
    ok_legacy = legacy == "applied"
    log("apply_legacy", ok_legacy, f"_legacy_state(无state) → {legacy!r}（保守=applied）")
    log("apply_should_skip", fails == 0, f"_should_skip_apply 独立 7 例（FAIL={fails}）")


def main() -> None:
    print("=" * 88)
    print("QA P24 fresh-eyes 独立复核（严过关/Yan）")
    print("=" * 88)
    verify_coverage()
    out = verify_backtest()
    verify_bootstrap(out)
    verify_rolling_sharpe(out)
    verify_apply_logic()

    section("[汇总]")
    fails = [k for k, v in RESULTS.items() if not v["ok"]]
    print(f"  PASS={len(RESULTS) - len(fails)} FAIL={len(fails)}")
    for k, v in RESULTS.items():
        print(f"  [{'PASS' if v['ok'] else 'FAIL'}] {k}: {v['msg']}")
    print(f"\nQA P24 独立复核: {'PASS' if not fails else 'FAIL（' + str(fails) + '）'}")


if __name__ == "__main__":
    main()
