"""P19 补充：组合权重峰值探查（w_a > 0.50）+ 生产 B 可交易性 + A0.50 容量。

主脚本 p19_combo_revalidate 的网格到 w_a=0.50 为止且 0.50 为网格最优（边界值），
本脚本补充探查 w_a∈{0.50..1.00} 以确认峰值，并给出高 A 权重下生产 B 的可交易性
（B 有效名义 = capital×nf_b×w_b 是否仍能出 1 手），以及候选 A0.50/B0.50 的组合层容量。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets
from scripts.p5_engineA_cross_section import combo_stats_row, engine_a_selection

from scripts.p19_rebuild_config import (
    ART,
    BASE_GROUP_MAP,
    V8_PATH,
    _align,
    build_engine_a_targets,
    run_engine_metrics,
    _prod_capacity_row,
)

W_A_EXT = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
VOL_GRID = [False, True]


def main() -> None:
    print("=" * 100)
    print("[P19-EXT] 组合权重峰值探查（w_a>0.50）+ 生产 B 可交易性 + A0.50 容量")
    print("=" * 100)
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    # ret 序列（复用 p19 的实现）
    tgt_a = build_engine_a_targets(prices, INITIAL_CAPITAL * 0.20, group_cap=0.5,
                                   group_map=BASE_GROUP_MAP)
    ra = run_engine_metrics(cfg, cost, prices, tgt_a, "A-nf0.20", ret_key="A-nf0.20")
    from scripts.p19_rebuild_config import RET_CACHE
    ret_a = RET_CACHE["A-nf0.20"]

    rows = []
    for thr in (0.70, 0.60):
        tgt_b = engine_b_targets(prices, 252, thr)
        run_engine_metrics(cfg, cost, prices, tgt_b, f"B-252-{thr:.2f}", ret_key=f"B-252-{thr:.2f}")
        ret_b = RET_CACHE[f"B-252-{thr:.2f}"]
        ra_, rb_ = _align(ret_a, ret_b)
        for w_a in W_A_EXT:
            for vt in VOL_GRID:
                r = combo_stats_row(ra_, rb_, w_a, vt)
                r["thr"] = thr
                r["vol_target"] = vt
                rows.append(r)
    df = pd.DataFrame(rows)
    df["oos_rank"] = df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("oos_rank")
    print("\n组合扩展网格（A-nf0.20 cap0.5 × B win252）按 OOS Sharpe 排名：")
    print(df[["oos_rank", "thr", "w_a", "w_b", "vol_target", "oos_sharpe", "oos_ret",
              "oos_maxdd"]].head(24).to_string(index=False))
    df.to_csv(ART / "p19_combo_ext.csv", index=False, encoding="utf-8-sig")
    print(f"\n[OK] → {ART / 'p19_combo_ext.csv'}")

    # 生产 B 可交易性（高 A 权重下）
    print("\n生产 B 有效名义 = capital × nf_b × w_b（nf_b=0.20）：")
    px_oos = prices[pd.to_datetime(prices.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    lot_min = min(
        float(px_oos.xs(s, level=0)["close"].min() * CONTRACTS18[s]["multiplier"])
        for s in SYMBOLS18
    )
    for w_b in (0.50, 0.40, 0.30, 0.20, 0.10):
        eff_b = INITIAL_CAPITAL * 0.20 * w_b
        print(f"  w_b={w_b:.2f}: eff={eff_b:,.0f} CNY = {int(eff_b//lot_min)} 手 m0")

    # A0.50/B0.50 容量
    print("\n候选 A0.50/B0.50 组合层容量（生产计划口径，thr0.70）：")
    sel_detail = engine_a_selection(prices, top_k=0.30, min_symbols=3, cache_path=V8_PATH,
                                    group_cap=0.5, group_map=BASE_GROUP_MAP)
    sel_detail = sel_detail[pd.to_datetime(sel_detail["ts"]) >= pd.Timestamp(OOS_START)]
    bs_anchor = engine_b_targets(prices, 252, 0.70)
    bs_oos = bs_anchor[pd.to_datetime(bs_anchor.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    for w_a, nf_a in ((0.50, 0.20), (0.50, 0.30), (0.35, 0.30)):
        r = _prod_capacity_row(sel_detail, bs_oos, prices, w_a, nf_a, round(1 - w_a, 2), 0.20)
        print(f"  A{w_a:.2f}/B{1-w_a:.2f} nfA={nf_a:.2f}: 峰值总名义/权益={r['peak_ratio']:.2f} "
              f"均值={r['mean_ratio']:.2f} 超1.0天数={r['days_over_cap']} "
              f"峰值保证金/权益={r['peak_margin_ratio']:.2f}")
    print("\nDONE")


if __name__ == "__main__":
    main()
