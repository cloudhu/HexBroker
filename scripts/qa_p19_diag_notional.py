"""QA P19 补充诊断：生产有效名义下的单引擎退化 + 组合差距分解。

关键问题：P19-3 组合网格用"研究口径"（每引擎 200k 名义 + 权重），
而生产 combo_plan 用"有效名义"（capital × nf × w 直接建 targets）。
本脚本量化两种口径的差距来源（lot floor 量化 + 可交易品种收窄）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.qa_p19_independent_verify import (
    INITIAL_CAPITAL,
    OOS_START,
    _seg_sharpe,
    combo_production,
    engine_a_selection_ind,
    engine_a_targets_ind,
    engine_b_targets_ind,
    load_prices,
    oos_metrics,
    run_engine,
)


def lot_stats(sel_sel, notional) -> dict:
    with np.errstate(invalid="ignore", divide="ignore"):
        lots = (notional / (sel_sel["_px"] * sel_sel["_mult"])).fillna(0.0).astype(int)
    rows = int((lots >= 1).sum())
    days = int(sel_sel.loc[lots >= 1, "ts"].nunique())
    return {"rows": rows, "days": days, "row_ratio": rows / len(sel_sel), "n_sel": len(sel_sel)}


def main() -> None:
    print("=" * 100)
    print("QA P19 补充诊断：研究口径 vs 生产有效名义口径差距分解")
    print("=" * 100)
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    sel = engine_a_selection_ind(prices, group_cap=0.5)
    sel_oos = sel[pd.to_datetime(sel["ts"]) >= pd.Timestamp(OOS_START)]
    sel_sel = sel_oos[sel_oos["selected"]].copy()
    print(f"\n[引擎 A] OOS 选中 {len(sel_sel)} 行 / {sel_sel['ts'].nunique()} 天")

    print("\n--- 引擎 A 名义 vs 可交易性（生产有效名义 70k/100k/130k/200k）---")
    for notional in (70_000, 100_000, 130_000, 200_000, 300_000):
        st = lot_stats(sel_sel, notional)
        tgt = engine_a_targets_ind(sel, notional)
        _, eq = run_engine(cfg, cost, prices, tgt)
        m = oos_metrics(eq)
        print(f"  名义 {notional:>7,}: OOS Sharpe={m['oos_sharpe']:.3f} OOS={m['oos_ret']*100:+.2f}% "
              f"MaxDD={m['oos_maxdd']*100:.1f}% | 可交易行 {st['rows']}/{st['n_sel']} "
              f"({100*st['row_ratio']:.1f}%) 天 {st['days']}/{sel_sel['ts'].nunique()}")

    print("\n--- 引擎 B（win252/thr0.70）名义敏感性 ---")
    for notional in (100_000, 130_000, 180_000, 200_000):
        tgt = engine_b_targets_ind(prices, 252, 0.70, notional)
        _, eq = run_engine(cfg, cost, prices, tgt)
        m = oos_metrics(eq)
        print(f"  名义 {notional:>7,}: OOS Sharpe={m['oos_sharpe']:.3f} OOS={m['oos_ret']*100:+.2f}% "
              f"MaxDD={m['oos_maxdd']*100:.1f}%")

    print("\n--- 组合：研究口径 vs 生产有效名义口径 ---")
    tgt_a200 = engine_a_targets_ind(sel, 200_000)
    ret_a200, _ = run_engine(cfg, cost, prices, tgt_a200)
    tgt_b200 = engine_b_targets_ind(prices, 252, 0.70, 200_000)
    ret_b200, _ = run_engine(cfg, cost, prices, tgt_b200)

    for w_a, w_b, label in [(0.50, 0.50, "A50/B50"), (0.35, 0.65, "A35/B65"), (0.10, 0.90, "A10/B90 现生产")]:
        # 研究口径
        common = ret_a200.index.intersection(ret_b200.index)
        ra, rb = ret_a200.loc[common].sort_index(), ret_b200.loc[common].sort_index()
        eq_r = (1.0 + w_a * ra + w_b * rb).cumprod() * INITIAL_CAPITAL
        m_r = oos_metrics(eq_r)

        # 生产有效名义口径（直接建 targets）
        na = int(INITIAL_CAPITAL * 0.20 * w_a)
        nb = int(INITIAL_CAPITAL * 0.20 * w_b)
        tgt_a = engine_a_targets_ind(sel, na)
        ret_a, _ = run_engine(cfg, cost, prices, tgt_a)
        tgt_b = engine_b_targets_ind(prices, 252, 0.70, nb)
        ret_b, _ = run_engine(cfg, cost, prices, tgt_b)
        eq_p = combo_production(ret_a, ret_b)
        m_p = oos_metrics(eq_p)
        print(f"  {label}: 研究口径 OOS Sharpe={m_r['oos_sharpe']:.3f} "
              f"(ret {m_r['oos_ret']*100:+.2f}%) | "
              f"生产口径 OOS Sharpe={m_p['oos_sharpe']:.3f} (ret {m_p['oos_ret']*100:+.2f}%) "
              f"| Δ={m_p['oos_sharpe']-m_r['oos_sharpe']:+.3f}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
