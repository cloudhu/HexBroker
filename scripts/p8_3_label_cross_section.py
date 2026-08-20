"""P8-3 训练标签截面化重估：v6（cross_rank 标签）vs v4（absolute 基线）。

背景
----
P8 根因定论：引擎 A 瓶颈 = 训练任务（时序预测绝对 exp_ret）与推理用途
（每日截面选 top30% 品种）错配。P8-2 加基差特征（v5）反而恶化（OOS -0.335），
说明不是特征缺失而是标签/任务错配。

本脚本（只读重估，不重训）：
  1. 覆盖率对比 v4 vs v6（同 p6_3b 口径）。
  2. 引擎 A S2 单引擎 OOS（基线 v4 -0.024）+ 组合 A15/B85（基线 1.384）。
  3. exp_ret 截面 IC 对比（关键附加验证）：每日截面 Spearman(exp_ret, realized)，
     期望标签截面化后模型输出更贴合截面排序。
  4. 关键判断：v6 OOS 是否转正/改善。

口径铁律：OOS 2024-07-18 后；嵌套零泄漏；完整回测（滑点1tick+费0.005%+
保证金12%+CONTRACTS18）；引擎 A 排序只用 exp_ret.rank 按日截面；不改
hexbroker/数据/v4 缓存。

用法
----
  python scripts/p8_3_label_cross_section.py [--cache-v6 artifacts/signals_cache18_grouped_v6.parquet]
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
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
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
V6_PATH = ART / "signals_cache18_grouped_v6.parquet"
OUT_CSV = ART / "p8_3_engineA_compare.csv"
OUT_IC = ART / "p8_3_cs_ic.csv"
OUT_COV = ART / "p8_3_cache_coverage.csv"

MIN_SYMBOLS_S2 = 3  # P5 终裁：S2 截面 rank + min=3
PROD_W_A = 0.15
PROD_W_B = 0.85
HORIZON = 5
IS_END = "2022-04-21"
V2_TAIL_CUT = "2026-06-11"


# ---------------------------------------------------------------------------
# 1. 已实现收益（用于截面 IC 验证）
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


# ---------------------------------------------------------------------------
# 2. 覆盖率（同 p6_3b 口径）
# ---------------------------------------------------------------------------
def coverage_stats(path: Path) -> pd.DataFrame:
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    cov = sig.groupby(sig["ts"].dt.date)["symbol"].count()
    tail = sig[sig["ts"] > pd.Timestamp(V2_TAIL_CUT)]
    tail_cov = tail.groupby(tail["ts"].dt.date)["symbol"].count()
    row = {
        "cache": path.stem.replace("signals_cache18_grouped_", ""),
        "n_signals": int(len(sig)),
        "n_days": int(len(cov)),
        "avg_symbols_per_day": round(float(cov.mean()), 2),
        "min_symbols_day": int(cov.min()),
        "max_symbols_day": int(cov.max()),
        "n_symbols": int(sig["symbol"].nunique()),
        "ts_start": str(pd.Timestamp(sig["ts"].min()).date()),
        "ts_end": str(pd.Timestamp(sig["ts"].max()).date()),
        "n_sig_2026_tail": int(len(tail)),
        "n_days_2026_tail": int(len(tail_cov)),
        "avg_sym_2026_tail": round(float(tail_cov.mean()), 2) if len(tail_cov) else np.nan,
    }
    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# 3. exp_ret 截面 IC（每日截面 Spearman，跨品种）
# ---------------------------------------------------------------------------
def cs_ic_summary(sig: pd.DataFrame, realized: pd.Series, oos_start: str) -> dict:
    """每日截面 IC 统计。

    每日截面：同一 ts 内 exp_ret vs realized 的 Spearman 相关（当日跨品种）。
    返回 全样本/IS/OOS 的 IC 均值、IC 标准差、ICIR（mean/std，未年化）、
    正 IC 占比、参与天数。
    """
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized.reindex(sig.index)
    sig = sig.dropna(subset=["realized"])

    def _daily_ic(g: pd.DataFrame) -> float:
        if len(g) < 3:
            return np.nan
        return float(g["exp_ret"].corr(g["realized"], method="spearman"))

    ics = sig.groupby(level="ts").apply(_daily_ic).dropna()
    ics.name = "ic"

    def _summ(sub: pd.Series) -> dict:
        if len(sub) == 0:
            return {"n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
                    "icir": np.nan, "pos_ratio": np.nan}
        return {
            "n_days": int(len(sub)),
            "ic_mean": float(sub.mean()),
            "ic_std": float(sub.std(ddof=1)) if len(sub) > 1 else 0.0,
            "icir": float(sub.mean() / sub.std(ddof=1)) if len(sub) > 1 and sub.std(ddof=1) > 0 else np.nan,
            "pos_ratio": float((sub > 0).mean()),
        }

    out = {"full": _summ(ics)}
    out["is"] = _summ(ics[ics.index < pd.Timestamp(IS_END)])
    out["oos"] = _summ(ics[ics.index >= pd.Timestamp(oos_start)])
    # 另附：全池 pooled rank_ic（参考，同 gate1 口径）
    out["pooled_rank_ic"] = float(sig["exp_ret"].corr(sig["realized"], method="spearman"))
    return out


def group_oos_rank_ic(sig: pd.DataFrame, realized: pd.Series, oos_start: str) -> pd.DataFrame:
    """分组建模 OOS 时序 rank_ic（品种内合并，同 p8_engineA_basis）。"""
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
            rows.append({"group": gname, "n": len(g), "rank_ic": np.nan})
            continue
        rows.append({"group": gname, "n": len(g),
                     "rank_ic": float(g["exp_ret"].corr(g["realized"], method="spearman"))})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="P8-3 标签截面化重估 v6 vs v4")
    ap.add_argument("--cache-v4", type=str, default=str(V4_PATH))
    ap.add_argument("--cache-v6", type=str, default=str(V6_PATH))
    ap.add_argument("--label-mode", type=str, default="cross_rank",
                    help="v6 采用的标签口径（仅用于命名展示）")
    args = ap.parse_args()

    print("=" * 96)
    print(f"P8-3 训练标签截面化重估：v6(label_mode={args.label_mode}) vs v4(absolute 基线)")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    print(f"[env] prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18")

    # ---- Stage 1: 覆盖率 ----
    print("-" * 96)
    print("[Stage 1] 覆盖率 v4 vs v6")
    cov = pd.concat([coverage_stats(Path(args.cache_v4)), coverage_stats(Path(args.cache_v6))],
                    ignore_index=True)
    ART.mkdir(exist_ok=True)
    cov.to_csv(OUT_COV, index=False)
    print(cov[["cache", "n_signals", "n_days", "avg_symbols_per_day", "min_symbols_day",
               "max_symbols_day", "n_symbols", "ts_start", "ts_end"]].to_string(index=False))
    print(cov[["cache", "n_sig_2026_tail", "n_days_2026_tail", "avg_sym_2026_tail"]].to_string(index=False))
    print(f"  [OK] → {OUT_COV}")

    # ---- Stage 2: 引擎 A S2 + 组合 ----
    print("-" * 96)
    print("[Stage 2] 引擎 A S2 单引擎 + 组合 A15/B85（v4 vs v6）")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    rows: list[dict] = []
    rets: dict[str, pd.Series] = {}
    for cp in (args.cache_v4, args.cache_v6):
        tag = Path(cp).stem.replace("signals_cache18_grouped_", "")
        tgt = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cp)
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt, f"A-{tag}")
        rets[tag] = ret
        rows.append({
            "cache": tag, "engine": "A-S2",
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "long_day_ratio": long_ratio, "vol_target": "N",
        })
        print(f"  [A-S2 {tag}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={m_oos.sharpe:.3f} "
              f"OOS MaxDD={m_oos.max_drawdown*100:.1f}% | 做多天数占比={long_ratio*100:.1f}%")

        for vt in (False, True):
            ra = ret.loc[ret.index.intersection(ret_b.index)]
            rb = ret_b.loc[ret.index.intersection(ret_b.index)]
            r = combo_stats_row(ra, rb, PROD_W_A, vt)
            rows.append({
                "cache": tag, "engine": f"A{PROD_W_A}/B{PROD_W_B}",
                "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
                "maxdd_full": r["maxdd_full"],
                "oos_sharpe": r["oos_sharpe"], "oos_maxdd": r["oos_maxdd"],
                "oos_ret": r["oos_ret"], "long_day_ratio": np.nan,
                "vol_target": "Y" if vt else "N",
            })
            print(f"  [combo {tag} A{PROD_W_A}/B{PROD_W_B} vol={'Y' if vt else 'N'}] "
                  f"Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f}")

    tbl = pd.DataFrame(rows)
    tbl.to_csv(OUT_CSV, index=False)
    print(f"  [OK] → {OUT_CSV}")

    # ---- Stage 3: exp_ret 截面 IC ----
    print("-" * 96)
    print("[Stage 3] exp_ret 截面 IC 对比（每日截面 Spearman(exp_ret, realized)）")
    ic_rows = []
    for cp in (args.cache_v4, args.cache_v6):
        tag = Path(cp).stem.replace("signals_cache18_grouped_", "")
        sig = pd.read_parquet(cp)
        summ = cs_ic_summary(sig, realized, OOS_START)
        for seg, st in summ.items():
            if seg == "pooled_rank_ic":
                continue
            ic_rows.append({"cache": tag, "segment": seg, **st})
        print(f"  [{tag}] pooled rank_ic={summ['pooled_rank_ic']:.4f}")
        for seg in ("full", "is", "oos"):
            st = summ[seg]
            print(f"      {seg:<5} n_days={st['n_days']:>4} IC均值={st['ic_mean']:+.4f} "
                  f"ICIR={st['icir']:+.3f} 正IC占比={st['pos_ratio']*100:5.1f}%")
    ic_tbl = pd.DataFrame(ic_rows)
    ic_tbl.to_csv(OUT_IC, index=False)
    print(f"  [OK] → {OUT_IC}")

    print("-" * 96)
    print("[Stage 3b] 分组 OOS rank_ic（v4 vs v6，品种内合并）")
    gic_rows = []
    for cp in (args.cache_v4, args.cache_v6):
        tag = Path(cp).stem.replace("signals_cache18_grouped_", "")
        sig = pd.read_parquet(cp)
        g = group_oos_rank_ic(sig, realized, OOS_START)
        g["cache"] = tag
        gic_rows.append(g)
    gic = pd.concat(gic_rows, ignore_index=True)
    pivot = gic.pivot_table(index="group", columns="cache", values="rank_ic")
    other = [c for c in pivot.columns if c != "v4"]
    if other:
        pivot["delta"] = pivot.get(other[0], np.nan) - pivot.get("v4", np.nan)
    print(pivot.round(4).to_string())

    # ---- 关键判断 ----
    print("=" * 96)
    tag4 = Path(args.cache_v4).stem.replace("signals_cache18_grouped_", "")
    tag6 = Path(args.cache_v6).stem.replace("signals_cache18_grouped_", "")
    a4 = tbl[(tbl["cache"] == tag4) & (tbl["engine"] == "A-S2")]["oos_sharpe"].iloc[0]
    a6 = tbl[(tbl["cache"] == tag6) & (tbl["engine"] == "A-S2")]["oos_sharpe"].iloc[0]
    c4 = tbl[(tbl["cache"] == tag4) & (tbl["engine"] == "A0.15/B0.85") & (tbl["vol_target"] == "N")]["oos_sharpe"].iloc[0]
    c6 = tbl[(tbl["cache"] == tag6) & (tbl["engine"] == "A0.15/B0.85") & (tbl["vol_target"] == "N")]["oos_sharpe"].iloc[0]
    ic4 = ic_tbl[(ic_tbl["cache"] == tag4) & (ic_tbl["segment"] == "oos")]["ic_mean"].iloc[0]
    ic6 = ic_tbl[(ic_tbl["cache"] == tag6) & (ic_tbl["segment"] == "oos")]["ic_mean"].iloc[0]
    print(f"关键判断（{tag6} 标签截面化 vs {tag4} 基线）")
    print(f"  引擎 A S2 OOS Sharpe: {a4:+.3f} -> {a6:+.3f} (Δ {a6-a4:+.3f}) "
          f"→ {'转正' if a6 > 0 else '仍为负'} {'改善' if a6 > a4 else '未改善'}")
    print(f"  组合 A15/B85 OOS Sharpe: {c4:.3f} -> {c6:.3f} (Δ {c6-c4:+.3f}) "
          f"→ {'提升' if c6 > c4 else '未提升'}")
    print(f"  exp_ret OOS 截面 IC: {ic4:+.4f} -> {ic6:+.4f} (Δ {ic6-ic4:+.4f}) "
          f"→ {'改善（更贴合截面排序）' if ic6 > ic4 else '未改善'}")
    print("=" * 96)
    print(f"[DONE] 对比表 → {OUT_CSV} | 截面 IC → {OUT_IC} | 覆盖率 → {OUT_COV}")


if __name__ == "__main__":
    main()
