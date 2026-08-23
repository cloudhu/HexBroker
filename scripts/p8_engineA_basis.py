"""P8-2 引擎 A 加入基差特征后的重估：v5（含基差）vs v4（基线）。

背景
----
P8-1 诊断：引擎 A（LightGBM 分组建模）信号质量差的直接嫌疑是特征体系纯量价/技术面，
训练从未见过基本面特征（基差/期限结构）。引擎 B 用 ``basis_ratio`` 品种内滚动分位
已验证 alpha（OOS Sharpe 1.26~1.62，品种内时序 IC +0.047）。

本脚本：
  1. 重训 v5 信号缓存（scripts/group_modeling_v2.py --output ...v5.parquet）后，
     用 ``engine_a_targets_cs(cache_path=v5)`` 重估引擎 A S2 单引擎 OOS。
  2. 组合 A15/B85（vol=N/Y）OOS 重估（引擎 B 固定 win252/thr0.7）。
  3. 对比 v4 基线（引擎 A S2 OOS -0.024 / 组合 A15B85 OOS 1.384）。
  4. 附加验证：分组建模 OOS 分段 IC（precious/agri 等此前翻转组）。

口径铁律：与 p3/p5/p6_3 完全一致——完整回测（滑点1tick+费0.005%+保证金12%+
CONTRACTS18）、OOS 2024-07-18 后、嵌套零泄漏、品种内时序因子验证。

用法
----
  python scripts/p8_engineA_basis.py [--cache-v4 ...] [--cache-v5 ...]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18
from scripts.group_modeling_v2 import GROUPS_V2
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    OOS_START,
    TOP_K,
    engine_b_targets,
)
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
)

ART = ROOT / "artifacts"
V4_PATH = ART / "signals_cache18_grouped_v4.parquet"
V5_PATH = ART / "signals_cache18_grouped_v5.parquet"
OUT_CSV = ART / "p8_engineA_basis_compare.csv"
OUT_GROUP_IC = ART / "p8_group_ic.csv"

MIN_SYMBOLS_S2 = 3  # P5 终裁：S2 截面 rank + min=3
PROD_W_A = 0.15
PROD_W_B = 0.85
HORIZON = 5


# ---------------------------------------------------------------------------
# 1. 已实现收益（用于组内时序 IC 验证）
# ---------------------------------------------------------------------------
def realized_returns(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    """每品种 5 日已实现收益 → MultiIndex(symbol, datetime)。"""
    parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"]
    )


def group_ic_table(sig: pd.DataFrame, realized: pd.Series, oos_start: str) -> pd.DataFrame:
    """按 v2 分组统计 OOS 时序 IC（Spearman exp_ret vs realized，品种内合并）。

    与「品种内时序因子验证」口径一致：只统计已实现收益非 NaN 的信号。
    """
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized.reindex(sig.index)
    sig = sig.dropna(subset=["realized"])
    sig = sig[sig.index.get_level_values(1) >= pd.Timestamp(oos_start)]

    sym2group = {}
    for gname, gcfg in GROUPS_V2.items():
        for s in gcfg["syms"]:
            sym2group[s] = gname
    sig["group"] = sig.index.get_level_values(0).map(sym2group)

    rows = []
    for gname, g in sig.groupby("group"):
        if len(g) < 5:
            rows.append({"group": gname, "n": len(g), "ic": np.nan, "rank_ic": np.nan})
            continue
        ic = g["exp_ret"].corr(g["realized"], method="pearson")
        ric = g["exp_ret"].corr(g["realized"], method="spearman")
        rows.append({"group": gname, "n": len(g), "ic": float(ic), "rank_ic": float(ric)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-v4", type=str, default=str(V4_PATH))
    ap.add_argument("--cache-v5", type=str, default=str(V5_PATH))
    args = ap.parse_args()

    print("=" * 96)
    print("P8-2 引擎 A 基差特征重估：v5（含基差）vs v4（基线）")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    print(f"[env] prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18")

    # ---- 覆盖率 ----
    for tag, p in (("v4", args.cache_v4), ("v5", args.cache_v5)):
        s = pd.read_parquet(p)
        s["ts"] = pd.to_datetime(s["ts"])
        cov = s.groupby(s["ts"].dt.date)["symbol"].count()
        print(f"[cov {tag}] {len(s)} 行 | {len(cov)} 天 | 日均 {cov.mean():.1f} 品种 | "
              f"OOS 日均 {s[s['ts']>=OOS_START].groupby(s[s['ts']>=OOS_START]['ts'].dt.date)['symbol'].count().mean():.1f}")

    # ---- 引擎 B（固定） ----
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    # ---- 引擎 A S2：v4 vs v5 ----
    rows = []
    rets: dict[str, pd.Series] = {}
    for tag, cp in (("v4", args.cache_v4), ("v5", args.cache_v5)):
        tgt = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cp)
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt, f"A-{tag}")
        rets[tag] = ret
        rows.append({
            "cache": tag,
            "engine": "A-S2",
            "sharpe_full": m.sharpe,
            "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "long_day_ratio": long_ratio,
        })
        print(f"[A-S2 {tag}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe="
              f"{m_oos.sharpe:.3f} OOS MaxDD={m_oos.max_drawdown*100:.1f}% "
              f"| 做多天数占比={long_ratio*100:.1f}%")

    # ---- 组合 A15/B85 ----
    for tag in ("v4", "v5"):
        ra, rb = rets[tag].loc[rets[tag].index.intersection(ret_b.index)], ret_b.loc[rets[tag].index.intersection(ret_b.index)]
        for vt in (False, True):
            r = combo_stats_row(ra, rb, PROD_W_A, vt)
            rows.append({
                "cache": tag,
                "engine": f"A{PROD_W_A}/B{PROD_W_B}",
                "sharpe_full": r["sharpe_full"],
                "ann_ret_full": r["ann_ret_full"],
                "maxdd_full": r["maxdd_full"],
                "oos_sharpe": r["oos_sharpe"],
                "oos_maxdd": r["oos_maxdd"],
                "oos_ret": r["oos_ret"],
                "long_day_ratio": np.nan,
                "vol_target": "Y" if vt else "N",
            })
            print(f"[combo {tag} A{PROD_W_A}/B{PROD_W_B} vol={'Y' if vt else 'N'}] "
                  f"Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f}")

    tbl = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    tbl.to_csv(OUT_CSV, index=False)
    print(f"\n[OK] 对比表 → {OUT_CSV}")

    # ---- 附加验证：分组建模 OOS IC ----
    print("\n[附加] 分组建模 OOS 时序 IC（v4 vs v5，OOS 2024-07-18 后）")
    ics = []
    for tag, cp in (("v4", args.cache_v4), ("v5", args.cache_v5)):
        sig = pd.read_parquet(cp)
        g = group_ic_table(sig, realized, OOS_START)
        g["cache"] = tag
        ics.append(g)
    gic = pd.concat(ics, ignore_index=True)
    gic.to_csv(OUT_GROUP_IC, index=False)
    pivot = gic.pivot_table(index="group", columns="cache", values="rank_ic")
    pivot["delta"] = pivot.get("v5", np.nan) - pivot.get("v4", np.nan)
    print(pivot.round(4).to_string())
    print(f"\n[OK] 分组 IC → {OUT_GROUP_IC}")

    # ---- 关键判断 ----
    a4 = tbl[(tbl["cache"] == "v4") & (tbl["engine"] == "A-S2")]["oos_sharpe"].iloc[0]
    a5 = tbl[(tbl["cache"] == "v5") & (tbl["engine"] == "A-S2")]["oos_sharpe"].iloc[0]
    c4 = tbl[(tbl["cache"] == "v4") & (tbl["engine"] == "A0.15/B0.85") & (tbl["vol_target"] == "N")]["oos_sharpe"].iloc[0]
    c5 = tbl[(tbl["cache"] == "v5") & (tbl["engine"] == "A0.15/B0.85") & (tbl["vol_target"] == "N")]["oos_sharpe"].iloc[0]
    print("\n" + "=" * 96)
    print("关键判断（v5 加基差 vs v4 基线）")
    print(f"  引擎 A S2 OOS Sharpe: {a4:+.3f} -> {a5:+.3f} (Δ {a5-a4:+.3f}) "
          f"→ {'转正' if a5 > 0 else '仍为负'} {'改善' if a5 > a4 else '未改善'}")
    print(f"  组合 A15/B85 OOS Sharpe: {c4:.3f} -> {c5:.3f} (Δ {c5-c4:+.3f}) "
          f"→ {'提升' if c5 > c4 else '未提升'}")
    print("=" * 96)


if __name__ == "__main__":
    main()
