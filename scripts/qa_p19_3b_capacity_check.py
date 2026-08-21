"""QA P19-3b Round 2：保守候选 A0.30/B0.70 nf_b0.30 容量独立复算。

背景：工程师报告 §4 声称保守候选容量"峰值总名义/权益 1.88、峰值保证金/权益 0.23"，
但 artifacts/p19_combo_prod_capacity.csv 只含 (0.10,0.20) (0.50,0.20) (0.50,0.40)
(0.30,0.40) (0.15,0.40) —— **没有 (0.30, 0.30) 行**。该数字无落盘支撑，需独立复算。

方法（生产计划口径，p16 combo_plan 语义）：
  - 引擎 A：engine_a_selection（p5 生产函数）选中明细 OOS 窗口 → 名义 capital×nf_a×w_a floor 手数
  - 引擎 B：engine_b_targets_notional（p19 3b 生产函数）OOS 窗口 → 名义 capital×nf_b×w_b floor 手数
  - 逐日合并总名义 → 峰值/均值/超1.0天数/峰值保证金（12%）
  同时用**独立实现**（qa_p19_independent_verify 的 engine_a_selection_ind +
  engine_b_targets_ind）交叉验证。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START
from scripts.p5_engineA_cross_section import engine_a_selection
from scripts.p19_rebuild_config import BASE_GROUP_MAP, V8_PATH, _prod_capacity_row
from scripts.p19_3b_prod_accounting import engine_b_targets_notional
from scripts.qa_p19_independent_verify import (
    GROUP_CAP,
    engine_a_selection_ind,
    engine_b_targets_ind,
)

OOS = pd.Timestamp(OOS_START)
MARGIN = 0.12


def capacity_independent(prices, w_a, nf_a, nf_b, win=252, thr=0.70) -> dict:
    """独立实现：用 qa 独立 A 选中 + 独立 B targets 逐日合并名义。"""
    w_b = round(1.0 - w_a, 4)
    na = INITIAL_CAPITAL * nf_a * w_a
    nb = INITIAL_CAPITAL * nf_b * w_b
    sel = engine_a_selection_ind(prices, group_cap=GROUP_CAP)
    sel = sel[sel["selected"]].copy()
    sel["ts"] = pd.to_datetime(sel["ts"])
    sel = sel[sel["ts"] >= OOS]

    day_notional: dict[pd.Timestamp, float] = {}
    for _, row in sel.iterrows():
        if pd.isna(row["_px"]):
            continue
        lots = int(na // (row["_px"] * row["_mult"]))
        if lots > 0:
            ts = pd.Timestamp(row["ts"])
            day_notional[ts] = day_notional.get(ts, 0.0) + lots * row["_px"] * row["_mult"]
    t_b = engine_b_targets_ind(prices, win, thr, nb)
    for (sym, ts), row in t_b.iterrows():
        lots = int(row["target"])
        if lots <= 0:
            continue
        ts = pd.Timestamp(ts)
        if ts < OOS:
            continue
        try:
            px = float(prices.xs(sym, level=0)["close"].get(ts))
        except KeyError:
            continue
        if pd.isna(px):
            continue
        mult = CONTRACTS18[sym]["multiplier"]
        day_notional[ts] = day_notional.get(ts, 0.0) + lots * px * mult

    arr = np.array(list(day_notional.values())) if day_notional else np.array([0.0])
    return {
        "w_a": w_a, "nf_b": nf_b,
        "peak_ratio": float(arr.max() / INITIAL_CAPITAL),
        "mean_ratio": float(arr.mean() / INITIAL_CAPITAL),
        "days_over_cap": int((arr > INITIAL_CAPITAL).sum()),
        "peak_margin_ratio": float(arr.max() * MARGIN / INITIAL_CAPITAL),
    }


def main() -> None:
    print("=" * 100)
    print("[QA P19-3b Round 2] 保守候选 A0.30/B0.70 nf_b0.30 容量独立复算")
    print("=" * 100)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 | OOS {OOS_START}")

    # 工程师函数口径（与 p19_combo_prod_capacity.csv 同构）
    sel_detail = engine_a_selection(prices, top_k=0.30, min_symbols=3, cache_path=V8_PATH,
                                    group_cap=0.5, group_map=BASE_GROUP_MAP)
    sel_detail = sel_detail[pd.to_datetime(sel_detail["ts"]) >= OOS]
    for w_a, nf_b in ((0.30, 0.30), (0.50, 0.40), (0.10, 0.20)):
        w_b = round(1.0 - w_a, 4)
        bs = engine_b_targets_notional(prices, 252, 0.70, INITIAL_CAPITAL * nf_b * w_b)
        bs_oos = bs[pd.to_datetime(bs.index.get_level_values(1)) >= OOS]
        r = _prod_capacity_row(sel_detail, bs_oos, prices, w_a, 0.20, w_b, nf_b)
        print(f"  [工程师函数] A{w_a:.2f}/B{w_b:.2f} nfB={nf_b:.2f}: "
              f"峰值名义/权益={r['peak_ratio']:.2f} 均值={r['mean_ratio']:.2f} "
              f"超1.0天数={r['days_over_cap']} 峰值保证金/权益={r['peak_margin_ratio']:.2f}")

    # 独立实现
    for w_a, nf_b in ((0.30, 0.30), (0.50, 0.40), (0.10, 0.20)):
        r = capacity_independent(prices, w_a, 0.20, nf_b)
        print(f"  [独立实现]   A{w_a:.2f}/B{round(1-w_a,4):.2f} nfB={nf_b:.2f}: "
              f"峰值名义/权益={r['peak_ratio']:.2f} 均值={r['mean_ratio']:.2f} "
              f"超1.0天数={r['days_over_cap']} 峰值保证金/权益={r['peak_margin_ratio']:.2f}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
