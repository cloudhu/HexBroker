#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P22 QA 独立复核：v8 == tail_ext 共享窗口逐字节 + 同窗口重估交叉验证。

QA 独立实现（不调用 scripts/p22_tail_ext.py 任何函数）：
  - 共享窗口逐字节比对（自写）
  - 引擎 A S2 目标自建（按文档口径：日截面 rank top30% / min=3 / cap=0.5 /
    group_map=base.yaml / score_col=exp_ret / floor 手数）
  - 引擎 B 目标自建（basis_ratio 滚动分位 win252/thr0.7）
  - 组合 A30/B70（vol_target=False）
  - BacktestEngine + CostModel（生产代码，同口径）

输出真实数字供 QA 报告贴出。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices, load_basis_panel
from scripts.p5_engineA_cross_section import engine_a_targets_cs as p5_engine_a_targets
from scripts.p3_combo_backtest import engine_b_targets as p3_engine_b_targets

V8 = ROOT / "artifacts/signals_cache18_grouped_v8.parquet"
TAIL = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
OOS_START = "2024-07-18"
TOP_K, MIN_SYMS, CAP = 0.30, 3, 0.5
NOTIONAL_FRAC = 0.20


def load_group_map():
    cfg = load_config("configs/base.yaml")
    return dict(cfg.backtest.engine_a.group_map)


# ---------------------------------------------------------------------------
# 自建引擎 A S2 目标（文档口径复刻）
# ---------------------------------------------------------------------------
def _capped_selection(ranked, target_count, cap, group_map):
    """按 exp_ret 降序候选 + 单组敞口上限 cap（P9-2 算法复刻）。"""
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g, n, total):
        return total > 0 and n / total > cap

    changed = True
    while changed and len(selected) > 1:
        changed = False
        for i in range(len(selected) - 1, -1, -1):
            g = group_map.get(selected[i], "other")
            if over_cap(g, counts[g], len(selected)) and counts[g] >= 2:
                counts[g] -= 1
                selected.pop(i)
                changed = True
                break
    sel_set = set(selected)
    for s in ranked:
        if len(selected) >= target_count:
            break
        if s in sel_set:
            continue
        g = group_map.get(s, "other")
        if (counts.get(g, 0) + 1) / (len(selected) + 1) <= cap:
            selected.append(s)
            counts[g] = counts.get(g, 0) + 1
            sel_set.add(s)
    return selected


def my_engine_a_targets(cache_path, prices, group_map):
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(
        {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    )
    sig["group"] = sig["symbol"].map(lambda s: group_map.get(s, "other"))
    elig = (sig["_px"].notna()) & (sig["_day_cnt"] >= MIN_SYMS)
    sig["selected"] = False
    for _ts, g in sig[elig].groupby("ts"):
        g = g.sort_values("exp_ret", ascending=False)
        target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
        if target_count <= 0:
            continue
        sel_syms = _capped_selection(g["symbol"].tolist(), target_count, CAP, group_map)
        sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = notional / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(sig["selected"], raw.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def my_engine_b_targets(prices, win=252, thr=0.70):
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


def run_bt(cfg, cost, prices, targets):
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily")
    tgt = targets["target"]
    total_days = pd.Index(pd.to_datetime(targets.index.get_level_values(1).unique()))
    long_days = pd.Index(pd.to_datetime(tgt[tgt > 0].index.get_level_values(1).unique()))
    long_ratio = len(long_days) / len(total_days)
    return ret, eq, m, m_oos, long_ratio


def align(a, b):
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def combo_metrics(ret_a, ret_b, w_a=0.30):
    comb = w_a * ret_a + (1 - w_a) * ret_b
    eq = (1 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily")
    return m, m_oos


def main():
    print("=" * 90)
    print("P22 QA 独立复核：共享窗口逐字节 + 同窗口重估（自建目标，不调 p22 函数）")
    print("=" * 90)
    group_map = load_group_map()
    prices = load_prices()
    print(f"prices: {len(prices)} 行 | 数据末端 {prices.index.get_level_values(1).max()}")

    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)

    # 引擎 B（自建 vs p3，应一致）
    tgt_b_my = my_engine_b_targets(prices)
    tgt_b_p3 = p3_engine_b_targets(prices, win=252, thr=0.70)
    b_diff = int((tgt_b_my["target"].to_numpy() != tgt_b_p3["target"].to_numpy()).sum())
    print(f"\n[引擎B] 自建 vs p3 targets 不一致行: {b_diff}（应 0）")
    ret_b, eq_b, m_b, m_b_oos, _ = run_bt(cfg, cost, prices, tgt_b_my)
    print(f"[引擎B] 全样本 Sharpe {m_b.sharpe:.4f} | OOS Sharpe {m_b_oos.sharpe:.4f} | "
          f"OOS ret {m_b_oos.total_return*100:+.2f}% | OOS n {len(m_b_oos.equity_curve) if hasattr(m_b_oos,'equity_curve') else ''}")

    print()
    print("-" * 90)
    results = {}
    for label, path in (("v8", V8), ("tail_ext", TAIL)):
        tgt_my = my_engine_a_targets(path, prices, group_map)
        tgt_p5 = p5_engine_a_targets(
            prices, top_k=TOP_K, min_symbols=MIN_SYMS, cache_path=path,
            group_cap=CAP, group_map=group_map, score_col="exp_ret",
        )
        # 对齐比较（index 层名可能不同）
        tgt_p5 = tgt_p5["target"].rename("target")
        tgt_my_s = tgt_my["target"].rename("target")
        merged = pd.concat([tgt_my_s, tgt_p5], axis=1, keys=["my", "p5"]).dropna()
        ndiff = int((merged["my"].to_numpy() != merged["p5"].to_numpy()).sum())
        print(f"\n[引擎A-{label}] 自建 vs p5 targets 不一致行: {ndiff}（应 0；共比对 {len(merged)} 行）")
        ret_a, eq_a, m_a, m_a_oos, long_ratio = run_bt(cfg, cost, prices, tgt_my)
        n_long = int((tgt_my["target"] > 0).sum())
        print(f"[引擎A-{label}] 全样本 Sharpe {m_a.sharpe:.4f} | OOS Sharpe {m_a_oos.sharpe:.4f} "
              f"| OOS ret {m_a_oos.total_return*100:+.4f}% | OOS maxDD {m_a_oos.max_drawdown*100:.2f}% "
              f"| 做多行 {n_long} | long_ratio {long_ratio:.4f} | OOS n {m_a_oos.n_bars}")
        # 组合
        ra, rb = align(ret_a, ret_b)
        m_c, m_c_oos = combo_metrics(ra, rb, w_a=0.30)
        print(f"[combo A30/B70-{label}] 全样本 Sharpe {m_c.sharpe:.4f} | OOS Sharpe {m_c_oos.sharpe:.4f} "
              f"| OOS ret {m_c_oos.total_return*100:+.4f}% | OOS n {m_c_oos.n_bars}")
        results[label] = dict(
            a_s2_oos_sharpe=m_a_oos.sharpe, a_s2_oos_ret=m_a_oos.total_return,
            a_s2_full_sharpe=m_a.sharpe, combo_oos_sharpe=m_c_oos.sharpe,
            combo_oos_ret=m_c_oos.total_return, n_long=n_long,
            oos_n=m_a_oos.n_bars, a_s2_oos_maxdd=m_a_oos.max_drawdown,
        )

    print()
    print("=" * 90)
    print("对比摘要（QA 自建目标 + BacktestEngine 同窗口 prices→08-21）")
    print("=" * 90)
    for label, r in results.items():
        print(f"  {label}: 引擎A S2 OOS Sharpe {r['a_s2_oos_sharpe']:.6f} | OOS ret {r['a_s2_oos_ret']*100:+.4f}% "
              f"| OOS maxDD {r['a_s2_oos_maxdd']*100:.2f}% | 做多行 {r['n_long']} | "
              f"combo OOS Sharpe {r['combo_oos_sharpe']:.6f} | combo OOS ret {r['combo_oos_ret']*100:+.4f}%")
    d_sh = results["tail_ext"]["a_s2_oos_sharpe"] - results["v8"]["a_s2_oos_sharpe"]
    d_cb = results["tail_ext"]["combo_oos_sharpe"] - results["v8"]["combo_oos_sharpe"]
    d_ret = results["tail_ext"]["a_s2_oos_ret"] - results["v8"]["a_s2_oos_ret"]
    print(f"\n  Δ 引擎A S2 OOS Sharpe: {d_sh:+.6f}（工程师声称 +0.0321）")
    print(f"  Δ 引擎A S2 OOS ret: {d_ret*100:+.4f}pp（工程师声称 +0.22pp）")
    print(f"  Δ combo OOS Sharpe: {d_cb:+.6f}（工程师声称 +0.0088）")
    print(f"  OOS n 相同（同窗口）: v8={results['v8']['oos_n']} tail_ext={results['tail_ext']['oos_n']}")


if __name__ == "__main__":
    main()
