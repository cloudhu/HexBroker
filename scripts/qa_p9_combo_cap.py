"""QA 补充：A10/B90 采纳组合下的 cap 效果（工程师口径）+ 全样本/OOS 分叉核对。"""
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
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets
from scripts.p5_engineA_cross_section import engine_a_targets_cs, run_engine_row

ART = ROOT / "artifacts"
V8 = ART / "signals_cache18_grouped_v8.parquet"

P9_GROUP_MAP = {
    "i0": "ferrous", "j0": "ferrous", "jm0": "ferrous",
    "rb0": "ferrous", "hc0": "ferrous",
    "cu0": "industrial", "al0": "industrial", "zn0": "industrial", "ni0": "industrial",
    "au0": "precious", "ag0": "precious",
    "y0": "agri_oil", "p0": "agri_oil",
    "m0": "agri_protein",
    "sr0": "agri_soft", "cf0": "agri_soft",
    "ta0": "chem_energy", "sc0": "chem_energy",
}


def combo(ret_a, ret_b, w_a):
    idx = ret_a.index.intersection(ret_b.index)
    comb = w_a * ret_a.loc[idx] + (1 - w_a) * ret_b.loc[idx]
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    oos_eq = eq[pd.to_datetime(eq.index) >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily")
    return m, m_oos


def main() -> None:
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    tgt_b = engine_b_targets(prices, 252, 0.70)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    tgt_a = engine_a_targets_cs(prices, 0.30, 3, cache_path=V8)
    ret_a = run_engine_row(cfg, cost, prices, tgt_a, "A")[0]
    tgt_ac = engine_a_targets_cs(prices, 0.30, 3, cache_path=V8,
                                 group_cap=0.5, group_map=P9_GROUP_MAP)
    ret_ac = run_engine_row(cfg, cost, prices, tgt_ac, "Acap")[0]

    print("\n[组合 cap 效果（工程师口径）]")
    for w_a, tag in [(0.15, "A15/B85"), (0.10, "A10/B90")]:
        _, mo = combo(ret_a, ret_b, w_a)
        _, moc = combo(ret_ac, ret_b, w_a)
        print(f"  {tag}: OOS Sharpe {mo.sharpe:.3f} → {moc.sharpe:.3f} "
              f"(Δ{moc.sharpe-mo.sharpe:+.3f}) | OOS 复利 {mo.total_return*100:+.2f}% → "
              f"{moc.total_return*100:+.2f}% (Δ{(moc.total_return-mo.total_return)*100:+.2f}pp)")

    # 全样本 vs OOS 分叉（vol=N 网格）
    grid = pd.read_csv(ART / "p9_combo_grid.csv")
    vn = grid[~grid["vol_target"]].sort_values("w_a")
    print("\n[vol=N 网格：全样本 Sharpe vs OOS Sharpe 分叉]")
    print(vn[["w_a", "w_b", "sharpe_full", "oos_sharpe", "oos_ret"]].round(4).to_string(index=False))
    d_full = vn.loc[vn["w_a"] == 0.10, "sharpe_full"].iloc[0] - vn.loc[vn["w_a"] == 0.15, "sharpe_full"].iloc[0]
    d_oos = vn.loc[vn["w_a"] == 0.10, "oos_sharpe"].iloc[0] - vn.loc[vn["w_a"] == 0.15, "oos_sharpe"].iloc[0]
    print(f"\nA10/B90 vs A15/B85: 全样本 Sharpe Δ={d_full:+.4f} (A10 更差) | "
          f"OOS Sharpe Δ={d_oos:+.4f} (A10 更好)")


if __name__ == "__main__":
    main()
