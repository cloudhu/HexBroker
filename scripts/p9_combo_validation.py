"""P9 组合级验证 + 黑色系敞口风控（复利口径）。

背景（P8-4 定案，QA VERIFIED）
----------------------------
- v8 已采纳为生产信号缓存：``artifacts/signals_cache18_grouped_v8.parquet``
  （label_pool=all + cross_z，8,624 行）；EngineAConfig.signal_cache 已切 v8。
- 生产基线：引擎 A（v8 + top_k 0.30 + S2 min=3）+ 引擎 B（win252/thr0.7/名义20%）
  + 组合 A15/B85 / vol_target=False → 组合 OOS 复利 +27.5% / Sharpe 1.602（P8-4 实测）。
- QA 风控条件：OOS 截面 IC 仍负（-0.064），盈利靠黑色系敞口
  （ferrous_raw+ferrous_steel 的 jm0/zn0/rb0/j0/hc0/i0 贡献 +0.7~1.8%）
  → 黑色系转弱需预警；复利口径为唯一评估口径。

本脚本（自包含编排）：
  Stage 1  组合权重网格（P9-1）：
           A∈{0.10,0.15,0.20,0.25,0.30,0.35}（B=1-A）× 波目标 on/off → 12 配置
           引擎 A v8（S2 min=3）、引擎 B win252/thr0.7 固定
           每配置完整 BacktestEngine 回测 → 全样本 + OOS 复利收益/Sharpe/MaxDD
           以 OOS 复利收益/Sharpe 选优 → artifacts/p9_combo_grid.csv
  Stage 2  黑色系敞口统计（P9-2）：
           每日选中品种中黑色系（ferrous_raw i0/j0/jm0 + ferrous_steel rb0/hc0）
           占比分布（全样本/IS/OOS）+ 极端敞口（>50% 天数占比）
           → artifacts/p9_ferrous_exposure.csv
  Stage 3  group_cap 对照（P9-2 风控实现）：
           engine_a_targets_cs 新增 group_cap 参数（默认 None 向后兼容）；
           group_cap=0.5（黑色系合并组敞口上限）→ 引擎 A 单引擎 + 组合 OOS 表现
           vs 无 cap 基线 → artifacts/p9_group_cap_compare.csv
  Stage 4  配置固化建议（P9-3）：ComboConfig 默认值裁决 + EngineAConfig.group_cap
           （config.py 已加 group_cap=None；本脚本输出是否需改 A/B 权重的裁决建议）

口径铁律：复利口径评估（OOS 权益曲线 total_return）；OOS 2024-07-18 后；
完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）；嵌套零泄漏；
不改 v8 缓存、不改数据文件。

用法：
  python scripts/p9_combo_validation.py
"""
from __future__ import annotations

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
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    NOTIONAL_FRAC,
    OOS_START,
    TOP_K,
    engine_b_targets,
)
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_selection,
    engine_a_targets_cs,
    run_engine_row,
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"

# P9-1 组合网格（A 权重 × 波目标开关）
W_A_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
VOL_GRID = [False, True]
VOL_TARGET = 0.175
VOL_HALFLIFE = 10

# 生产基线（P8-4 定案）
PROD_W_A = 0.15
PROD_W_B = 0.85
PROD_VOL = False

MIN_SYMBOLS_S2 = 3  # P5 终裁：S2 截面 rank + min=3

# P9-2 黑色系合并组映射（ferrous_raw + ferrous_steel → 单一 'ferrous' 组），
# 其余组沿用 GROUPS_V2。group_cap=0.5 即"黑色系敞口上限 50%"。
FERROUS_SYMS = ["i0", "j0", "jm0", "rb0", "hc0"]
P9_GROUPS: dict[str, list[str]] = {
    "ferrous": FERROUS_SYMS,
    "industrial": ["cu0", "al0", "zn0", "ni0"],
    "precious": ["au0", "ag0"],
    "agri_oil": ["y0", "p0"],
    "agri_protein": ["m0"],
    "agri_soft": ["sr0", "cf0"],
    "chem_energy": ["ta0", "sc0"],
}
P9_GROUP_MAP: dict[str, str] = {
    s: g for g, syms in P9_GROUPS.items() for s in syms
}

GROUP_CAP_TEST = 0.5  # 黑色系敞口上限对照实验


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%" if pd.notna(x) else "   n/a"


def fmt_sh(x: float) -> str:
    return f"{x:.3f}" if pd.notna(x) else "  n/a"


def fmt_plain(x: float) -> str:
    return f"{x:.4f}" if pd.notna(x) else "  n/a"


# ---------------------------------------------------------------------------
# Stage 1：组合权重网格（P9-1）
# ---------------------------------------------------------------------------
def stage1_grid(ret_a: pd.Series, ret_b: pd.Series) -> pd.DataFrame:
    """12 配置网格 → 对比表（复利口径，OOS Sharpe 排序）。"""
    rows = []
    for w_a in W_A_GRID:
        for vt in VOL_GRID:
            ra, rb = align_rets(ret_a, ret_b)
            r = combo_stats_row(ra, rb, w_a, vt)
            r["w_b"] = round(1.0 - w_a, 4)
            rows.append(r)
    tbl = pd.DataFrame(rows)
    tbl["oos_rank"] = tbl["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    return tbl


def align_rets(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


# ---------------------------------------------------------------------------
# Stage 2：黑色系敞口统计（P9-2）
# ---------------------------------------------------------------------------
def ferrous_daily_share(sel: pd.DataFrame) -> pd.DataFrame:
    """每日选中品种中黑色系占比 → 长表（ts, n_selected, n_ferrous, ferrous_share）。"""
    s = sel[sel["selected"]]
    g = s.groupby("ts")
    daily = pd.DataFrame({
        "n_selected": g.size(),
        "n_ferrous": g["group"].apply(lambda x: int((x == "ferrous").sum())),
    })
    daily["ferrous_share"] = daily["n_ferrous"] / daily["n_selected"]
    daily = daily.reset_index()
    # 仅保留当日确有选中品种的日子（n_selected>0 天然满足）
    return daily


def segment_share_summary(daily: pd.DataFrame) -> list[dict]:
    """全样本 / IS / OOS 三段敞口分布摘要（含极端敞口 >50% 天数占比）。"""
    segs = [
        ("full", pd.Timestamp("2000-01-01"), None),
        ("is", pd.Timestamp("2000-01-01"), pd.Timestamp(OOS_START)),
        ("oos", pd.Timestamp(OOS_START), None),
    ]
    out = []
    for name, lo, hi in segs:
        d = daily[pd.to_datetime(daily["ts"]) >= lo]
        if hi is not None:
            d = d[pd.to_datetime(d["ts"]) < hi]
        if len(d) == 0:
            continue
        sh = d["ferrous_share"]
        n_over = int((sh > 0.5).sum())
        out.append({
            "segment": name,
            "n_days": len(d),
            "n_selected_rows": int(d["n_selected"].sum()),
            "mean_share": float(sh.mean()),
            "std_share": float(sh.std()),
            "p50_share": float(sh.median()),
            "p90_share": float(sh.quantile(0.90)),
            "max_share": float(sh.max()),
            "days_over_50pct": n_over,
            "pct_days_over_50pct": float(n_over / len(d) * 100),
        })
    return out


def group_share_table(sel: pd.DataFrame) -> pd.DataFrame:
    """各组选中占比（全样本 / OOS），与 P8-4 selection_balance 口径一致。"""
    rows = []
    for seg, lo, hi in [("full", pd.Timestamp("2000-01-01"), None),
                        ("oos", pd.Timestamp(OOS_START), None)]:
        s = sel[sel["selected"] & (pd.to_datetime(sel["ts"]) >= lo)]
        if hi is not None:
            s = s[pd.to_datetime(s["ts"]) < hi]
        n_sel = len(s)
        for gname, g in s.groupby("group"):
            rows.append({
                "table": "group_share",
                "segment": seg,
                "group": gname,
                "n_syms": int(g["symbol"].nunique()),
                "sel_share_pct": float(g.shape[0] / n_sel * 100) if n_sel else np.nan,
            })
    return pd.DataFrame(rows)


def symbol_share_table(sel: pd.DataFrame) -> pd.DataFrame:
    """黑色系各品种选中占比（全样本 / OOS）。"""
    rows = []
    for seg, lo, hi in [("full", pd.Timestamp("2000-01-01"), None),
                        ("oos", pd.Timestamp(OOS_START), None)]:
        s = sel[sel["selected"] & (pd.to_datetime(sel["ts"]) >= lo)]
        if hi is not None:
            s = s[pd.to_datetime(s["ts"]) < hi]
        n_sel = len(s)
        for sym in FERROUS_SYMS:
            rows.append({
                "table": "symbol_share",
                "segment": seg,
                "group": P9_GROUP_MAP.get(sym, "ferrous"),
                "symbol": sym,
                "n_syms": 1,
                "sel_share_pct": float((s["symbol"] == sym).sum() / n_sel * 100) if n_sel else np.nan,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Stage 3：group_cap 对照（P9-2）
# ---------------------------------------------------------------------------
def combo_from_rets(ret_a: pd.Series, ret_b: pd.Series, w_a: float, vt: bool) -> dict:
    ra, rb = align_rets(ret_a, ret_b)
    return combo_stats_row(ra, rb, w_a, vt)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    t_start = time.time()
    print("=" * 100)
    print("P9 组合级验证 + 黑色系敞口风控（复利口径）")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"INITIAL_CAPITAL={INITIAL_CAPITAL:,.0f} | top_k={TOP_K} | 名义={NOTIONAL_FRAC*100:.0f}%权益/标的")
    print("=" * 100)

    # ---- 0. 环境 ----
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"\n[0] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种 | "
          f"v8 缓存: {V8_PATH.name}（存在={V8_PATH.exists()}）")

    # ---- 引擎 B（固定 win252/thr0.7）----
    print("\n[0b] 引擎 B（基差 win252/thr0.7）")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    # ---- Stage 1: 组合权重网格 ----
    print("-" * 100)
    print("[Stage 1] 组合权重网格（P9-1）：A∈{0.10..0.35} × 波目标 on/off → 12 配置")
    tgt_a = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=V8_PATH)
    ret_a, eq_a, m_a, m_oos_a, long_ratio_a = run_engine_row(
        cfg, cost, prices, tgt_a, "A-v8")
    print(f"  引擎 A v8 S2: Sharpe={m_a.sharpe:.3f} 年化={m_a.annual_return*100:+.1f}% "
          f"MaxDD={m_a.max_drawdown*100:.1f}% | OOS Sharpe={m_oos_a.sharpe:.3f} "
          f"OOS 复利={m_oos_a.total_return*100:+.2f}% OOS MaxDD={m_oos_a.max_drawdown*100:.1f}%")

    grid = stage1_grid(ret_a, ret_b)
    ART.mkdir(exist_ok=True)
    grid.to_csv(ART / "p9_combo_grid.csv", index=False)

    show = grid.copy()
    show["vol_target"] = show["vol_target"].map({True: "Y", False: "N"})
    show["sharpe_full"] = show["sharpe_full"].map(fmt_sh)
    show["ann_ret_full"] = show["ann_ret_full"].map(fmt_pct)
    show["maxdd_full"] = show["maxdd_full"].map(fmt_pct)
    show["oos_sharpe"] = show["oos_sharpe"].map(fmt_sh)
    show["oos_maxdd"] = show["oos_maxdd"].map(fmt_pct)
    show["oos_ret"] = show["oos_ret"].map(fmt_pct)
    show = show.sort_values("oos_rank")
    print(show[["oos_rank", "w_a", "w_b", "vol_target", "sharpe_full", "ann_ret_full",
                "maxdd_full", "oos_sharpe", "oos_maxdd", "oos_ret", "oos_n"]]
          .to_string(index=False))
    print(f"  [OK] → {ART / 'p9_combo_grid.csv'}")

    # 选优：OOS Sharpe 主序 + OOS 复利收益确认（复利口径铁律）
    best_sh = grid.loc[grid["oos_sharpe"].idxmax()]
    best_ret = grid.loc[grid["oos_ret"].idxmax()]
    base = grid[(grid["w_a"] == PROD_W_A) & (~grid["vol_target"])].iloc[0]
    print(f"\n  生产基线 A{PROD_W_A:.2f}/B{PROD_W_B:.2f} vol=N: "
          f"OOS Sharpe={base['oos_sharpe']:.3f} OOS 复利={base['oos_ret']*100:+.2f}% "
          f"OOS MaxDD={base['oos_maxdd']*100:.1f}%")
    print(f"  OOS Sharpe 最优: A{best_sh['w_a']:.2f}/B{best_sh['w_b']:.2f} "
          f"vol={'Y' if best_sh['vol_target'] else 'N'} "
          f"OOS Sharpe={best_sh['oos_sharpe']:.3f} OOS 复利={best_sh['oos_ret']*100:+.2f}%")
    print(f"  OOS 复利最优  : A{best_ret['w_a']:.2f}/B{best_ret['w_b']:.2f} "
          f"vol={'Y' if best_ret['vol_target'] else 'N'} "
          f"OOS Sharpe={best_ret['oos_sharpe']:.3f} OOS 复利={best_ret['oos_ret']*100:+.2f}%")

    # ---- Stage 2: 黑色系敞口统计 ----
    print("-" * 100)
    print("[Stage 2] 黑色系敞口统计（P9-2）：v8 引擎 A 每日选中品种中黑色系占比")
    sel = engine_a_selection(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=V8_PATH,
                             group_cap=None, group_map=P9_GROUP_MAP)
    daily = ferrous_daily_share(sel)
    seg_rows = segment_share_summary(daily)
    grp_rows = group_share_table(sel)
    sym_rows = symbol_share_table(sel)
    expo = pd.concat([
        pd.DataFrame(seg_rows),
        grp_rows,
        sym_rows,
    ], ignore_index=True)
    expo.to_csv(ART / "p9_ferrous_exposure.csv", index=False)

    for r in seg_rows:
        print(f"  [{r['segment']:<4}] 天数={r['n_days']:>4} 选中行={r['n_selected_rows']:>5} "
              f"黑色系占比 mean={r['mean_share']*100:5.1f}% p50={r['p50_share']*100:5.1f}% "
              f"p90={r['p90_share']*100:5.1f}% max={r['max_share']*100:5.1f}% "
              f"| >50% 天数={r['days_over_50pct']:>3} ({r['pct_days_over_50pct']:5.1f}%)")
    print("  黑色系各品种选中占比（OOS）：")
    sym_oos = sym_rows[sym_rows["segment"] == "oos"].sort_values("sel_share_pct", ascending=False)
    for _, r in sym_oos.iterrows():
        print(f"    {r['symbol']:<4} {r['sel_share_pct']:6.2f}%")
    print("  各组选中占比（OOS）：")
    grp_oos = grp_rows[grp_rows["segment"] == "oos"].sort_values("sel_share_pct", ascending=False)
    for _, r in grp_oos.iterrows():
        print(f"    {r['group']:<16} {r['sel_share_pct']:6.2f}%")
    print(f"  [OK] → {ART / 'p9_ferrous_exposure.csv'}")

    # ---- Stage 3: group_cap 对照 ----
    print("-" * 100)
    print(f"[Stage 3] group_cap 对照（P9-2）：group_cap={GROUP_CAP_TEST}（黑色系合并组敞口上限）")
    tgt_a_cap = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=V8_PATH,
                                    group_cap=GROUP_CAP_TEST, group_map=P9_GROUP_MAP)
    ret_a_cap, eq_a_cap, m_a_cap, m_oos_a_cap, lr_a_cap = run_engine_row(
        cfg, cost, prices, tgt_a_cap, "A-v8-cap")
    print(f"  引擎 A v8 cap{GROUP_CAP_TEST}: Sharpe={m_a_cap.sharpe:.3f} "
          f"年化={m_a_cap.annual_return*100:+.1f}% MaxDD={m_a_cap.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={m_oos_a_cap.sharpe:.3f} OOS 复利={m_oos_a_cap.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos_a_cap.max_drawdown*100:.1f}% | 做多天数占比={lr_a_cap*100:.1f}%")
    print(f"  （基线）引擎 A v8      : Sharpe={m_a.sharpe:.3f} "
          f"年化={m_a.annual_return*100:+.1f}% MaxDD={m_a.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={m_oos_a.sharpe:.3f} OOS 复利={m_oos_a.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos_a.max_drawdown*100:.1f}% | 做多天数占比={long_ratio_a*100:.1f}%")

    cap_rows: list[dict] = []
    # 单引擎
    cap_rows.append({
        "config": "A-S2", "variant": "baseline", "group_cap": None,
        "sharpe_full": m_a.sharpe, "ann_ret_full": m_a.annual_return,
        "maxdd_full": m_a.max_drawdown,
        "oos_sharpe": m_oos_a.sharpe, "oos_maxdd": m_oos_a.max_drawdown,
        "oos_ret": m_oos_a.total_return, "long_day_ratio": long_ratio_a,
    })
    cap_rows.append({
        "config": "A-S2", "variant": f"cap{GROUP_CAP_TEST}", "group_cap": GROUP_CAP_TEST,
        "sharpe_full": m_a_cap.sharpe, "ann_ret_full": m_a_cap.annual_return,
        "maxdd_full": m_a_cap.max_drawdown,
        "oos_sharpe": m_oos_a_cap.sharpe, "oos_maxdd": m_oos_a_cap.max_drawdown,
        "oos_ret": m_oos_a_cap.total_return, "long_day_ratio": lr_a_cap,
    })
    # 组合（生产权重 A15/B85，vol N + Y 两个口径）
    for vt in (False, True):
        c_base = combo_from_rets(ret_a, ret_b, PROD_W_A, vt)
        c_cap = combo_from_rets(ret_a_cap, ret_b, PROD_W_A, vt)
        for tag, c, capv in (("baseline", c_base, None),
                             (f"cap{GROUP_CAP_TEST}", c_cap, GROUP_CAP_TEST)):
            cap_rows.append({
                "config": f"A{PROD_W_A:.2f}/B{PROD_W_B:.2f}",
                "variant": tag, "group_cap": capv, "vol_target": vt,
                "sharpe_full": c["sharpe_full"], "ann_ret_full": c["ann_ret_full"],
                "maxdd_full": c["maxdd_full"],
                "oos_sharpe": c["oos_sharpe"], "oos_maxdd": c["oos_maxdd"],
                "oos_ret": c["oos_ret"], "long_day_ratio": np.nan,
            })
    cap_tbl = pd.DataFrame(cap_rows)
    cap_tbl.to_csv(ART / "p9_group_cap_compare.csv", index=False)

    print(f"\n  组合 A{PROD_W_A:.2f}/B{PROD_W_B:.2f}（生产权重）对比：")
    combo_tag = f"A{PROD_W_A:.2f}/B{PROD_W_B:.2f}"
    for _, r in cap_tbl[cap_tbl["config"] == combo_tag].iterrows():
        print(f"    [{r['variant']:<10} vol={'Y' if r['vol_target'] else 'N'}] "
              f"OOS Sharpe={r['oos_sharpe']:.3f} OOS 复利={r['oos_ret']*100:+.2f}% "
              f"OOS MaxDD={r['oos_maxdd']*100:.1f}%")
    print(f"  [OK] → {ART / 'p9_group_cap_compare.csv'}")

    # ---- Stage 4: 配置固化裁决 ----
    print("-" * 100)
    print("[Stage 4] 配置固化裁决（P9-3）")
    vol_n = grid[~grid["vol_target"]].copy()
    best_vol_n = vol_n.loc[vol_n["oos_sharpe"].idxmax()]
    best_any = grid.loc[grid["oos_sharpe"].idxmax()]
    cfg_now = load_config()
    cfg_ea = cfg_now.backtest.engine_a
    cfg_cb = cfg_now.backtest.combo
    print(f"  EngineAConfig.group_cap 默认 = {cfg_ea.group_cap!r}（None=不启用，向后兼容）")
    print(f"  ComboConfig 当前默认 = A{cfg_cb.w_engine_a:.2f}/B{cfg_cb.w_engine_b:.2f} "
          f"vol={'Y' if cfg_cb.vol_target else 'N'}")
    print(f"  vol=N 网格 OOS Sharpe 最优: A{best_vol_n['w_a']:.2f}/B{best_vol_n['w_b']:.2f} "
          f"OOS Sharpe={best_vol_n['oos_sharpe']:.3f} OOS 复利={best_vol_n['oos_ret']*100:+.2f}%")
    print(f"  全部网格 OOS Sharpe 最优  : A{best_any['w_a']:.2f}/B{best_any['w_b']:.2f} "
          f"vol={'Y' if best_any['vol_target'] else 'N'} "
          f"OOS Sharpe={best_any['oos_sharpe']:.3f} OOS 复利={best_any['oos_ret']*100:+.2f}%")

    same_weight = abs(best_vol_n["w_a"] - cfg_cb.w_engine_a) < 1e-9
    same_vol = (not best_vol_n["vol_target"]) == (not cfg_cb.vol_target)
    if same_weight and same_vol:
        verdict = "KEEP"
        reason = (f"vol=N 网格最优 A{best_vol_n['w_a']:.2f}/B{best_vol_n['w_b']:.2f} "
                  f"与 ComboConfig 当前默认一致，无需改动")
    else:
        verdict = "CHANGE"
        reason = (f"vol=N 网格最优 A{best_vol_n['w_a']:.2f}/B{best_vol_n['w_b']:.2f} "
                  f"≠ ComboConfig 当前默认 A{cfg_cb.w_engine_a:.2f}/B{cfg_cb.w_engine_b:.2f} "
                  f"→ 需更新 w_engine_a/w_engine_b 默认值")
    print(f"  组合权重裁决: {verdict} —— {reason}")

    # group_cap 是否启用
    delta_sh = m_oos_a_cap.sharpe - m_oos_a.sharpe
    delta_ret = m_oos_a_cap.total_return - m_oos_a.total_return
    combo_base = combo_from_rets(ret_a, ret_b, PROD_W_A, PROD_VOL)
    combo_cap = combo_from_rets(ret_a_cap, ret_b, PROD_W_A, PROD_VOL)
    print(f"  单引擎 A cap{GROUP_CAP_TEST} vs 基线: OOS Sharpe Δ={delta_sh:+.3f} "
          f"OOS 复利 Δ={delta_ret*100:+.2f}pp")
    print(f"  组合   cap{GROUP_CAP_TEST} vs 基线: OOS Sharpe Δ="
          f"{combo_cap['oos_sharpe']-combo_base['oos_sharpe']:+.3f} "
          f"OOS 复利 Δ={(combo_cap['oos_ret']-combo_base['oos_ret'])*100:+.2f}pp "
          f"OOS MaxDD Δ={(combo_cap['oos_maxdd']-combo_base['oos_maxdd'])*100:+.2f}pp")

    print("=" * 100)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s")
    print("=" * 100)


if __name__ == "__main__":
    main()
