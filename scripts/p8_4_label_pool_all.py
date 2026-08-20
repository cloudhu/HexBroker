"""P8-4 全 18 品种统一截面重构：重训 v8（cross_z + label_pool=all）+ 重估对比。

背景
----
P8-3 定论（QA VERIFIED）：标签截面化（label_mode）是"组内"的（每组独立面板）
→ 跨组尺度错配：
  - v6 cross_rank：单品种组 m0 走绝对标签（exp_ret≈0.008）vs 多品种组（3.1-3.8）
    → 按日跨 18 品种统一 rank 时 m0 入选 0%、2 品种组占做多 79.9%
  - v7 cross_z：组内 z（各组均值归零）→ 引擎 A OOS 算术 +0.004 但复利仍亏 -1.63%
    （QA：度量假象）
P8-4 方向：把截面化从"组内"升级为"全 18 品种统一截面"（label_pool='all'）——
  m0 也参与全品种当日截面，根治组规模偏置。

本脚本（自包含编排）：
  Stage 0  重训 v8（--label-mode cross_z --label-pool all）→
           artifacts/signals_cache18_grouped_v8.parquet
           （可选 --v9 补跑 cross_rank+all → v9 作对照）
  Stage 0.5 全品种面板零泄漏实证（真实数据扰动检查）
  Stage 1  覆盖率 v4/v7/v8[/v9] → artifacts/p8_4_cache_coverage.csv
  Stage 2  引擎 A S2 单引擎 OOS（复利口径！）+ 组合 A15/B85
           → artifacts/p8_4_engineA_compare.csv
  Stage 3  exp_ret 截面 IC → artifacts/p8_4_cs_ic.csv
  Stage 4  m0 入选率 / 选组均衡性（根治组规模偏置的直接证据）
           → artifacts/p8_4_selection_balance.csv
  Stage 5  关键判断 + 裁决建议（IS_PASS）

口径铁律：截面化只用当日信息（无前视）；OOS 2024-07-18 后；嵌套零泄漏；
完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）；引擎 A 评估用复利口径。
不改 v4/v6/v7 缓存、不改数据文件、不改 hexbroker 其他模块。

用法
----
  python scripts/p8_4_label_pool_all.py                 # 全流程（重训 v8 + 重估）
  python scripts/p8_4_label_pool_all.py --v9            # 额外补跑 v9（cross_rank+all）
  python scripts/p8_4_label_pool_all.py --skip-train    # 仅重估（v8 已存在）
  python scripts/p8_4_label_pool_all.py --n-jobs 8
"""
from __future__ import annotations

import argparse
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
from scripts.group_modeling import build_group_signals
from scripts.group_modeling_v2 import GROUPS_V2
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import BASIS_THR, BASIS_WIN, OOS_START, TOP_K, engine_b_targets
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
)
from scripts.p8_3_label_cross_section import (
    coverage_stats,
    cs_ic_summary,
    realized_returns,
)

ART = ROOT / "artifacts"
V4_PATH = ART / "signals_cache18_grouped_v4.parquet"
V7_PATH = ART / "signals_cache18_grouped_v7.parquet"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V9_PATH = ART / "signals_cache18_grouped_v9.parquet"

OUT_COV = ART / "p8_4_cache_coverage.csv"
OUT_ENG = ART / "p8_4_engineA_compare.csv"
OUT_IC = ART / "p8_4_cs_ic.csv"
OUT_SEL = ART / "p8_4_selection_balance.csv"
OUT_VERDICT = ART / "p8_4_verdict.json"

MIN_SYMBOLS_S2 = 3  # P5 终裁：S2 截面 rank + min=3
PROD_W_A = 0.15
PROD_W_B = 0.85
HORIZON = 5

# 各版本标签口径（仅展示用）
VERSIONS = [
    ("v4", V4_PATH, "absolute", "group"),
    ("v7", V7_PATH, "cross_z", "group"),
    ("v8", V8_PATH, "cross_z", "all"),
    ("v9", V9_PATH, "cross_rank", "all"),
]


# ---------------------------------------------------------------------------
# Stage 0：重训（全品种统一截面标签）
# ---------------------------------------------------------------------------
def train_cache(label_mode: str, label_pool: str, out_path: Path, n_jobs: int) -> pd.DataFrame:
    """按 GROUPS_V2 逐组训练（截面化范围=label_pool），合并信号落盘。"""
    print(f"[TRAIN] label_mode={label_mode} label_pool={label_pool} n_jobs={n_jobs}")
    frames = []
    for gname, gcfg in GROUPS_V2.items():
        t0 = time.time()
        sig = build_group_signals(gcfg["syms"], gcfg["global"], n_jobs,
                                  cal_return_all=False,
                                  collect_models=False,
                                  label_mode=label_mode,
                                  label_pool=label_pool)
        frames.append(sig)
        print(f"  [{gname}] {len(sig)} 条信号 ({time.time()-t0:.0f}s)")
    all_sig = pd.concat(frames, ignore_index=True)
    all_sig["ts"] = pd.to_datetime(all_sig["ts"])
    ART.mkdir(exist_ok=True)
    all_sig.to_parquet(out_path, index=False)
    print(f"[TRAIN OK] {len(all_sig)} 条信号 → {out_path}")
    print(f"  品种覆盖: {sorted(all_sig['symbol'].unique())}")
    return all_sig


# ---------------------------------------------------------------------------
# Stage 0.5：全品种面板零泄漏实证（真实数据扰动）
# ---------------------------------------------------------------------------
def zero_leakage_check() -> None:
    from scripts import refine_lightgbm_champion as R

    panel = R._build_fwd_cs_panel(None, None, HORIZON, "cross_z", "all")
    assert panel is not None and len(panel.columns) == 18, "全品种面板应含 18 列"
    # 只扰动单品种未来 close（改变截面结构）。注意：若全部品种同乘常数，
    # z 尺度不变（QA 已证退化），故必须单品种扰动才能检验无前视。
    close_map = R._load_all18_close_map()
    sym0 = list(close_map)[0]
    s0 = close_map[sym0]
    cut = int(len(s0) * 0.8)
    close2_map = {k: v.copy() for k, v in close_map.items()}
    close2_map[sym0].iloc[cut:] = close2_map[sym0].iloc[cut:] * 1.5
    orig = R._load_all18_close_map
    R._load_all18_close_map = lambda: close2_map
    try:
        panel2 = R._build_fwd_cs_panel(None, None, HORIZON, "cross_z", "all")
    finally:
        R._load_all18_close_map = orig
    # fwd 行 t 依赖 close[t+h]：扰动自 cut 起 → 受影响行仅 [cut-h, cut)。
    # 无前视 ⇒ t < cut - h 的截面逐位不变（若截面用了未来行统计，会污染更早行）。
    diff = (panel.iloc[: cut - HORIZON] - panel2.iloc[: cut - HORIZON]).abs().max().max()
    print(f"[ZERO-LEAK] 全品种面板 18 列 | 单品种未来扰动→早于 cut-h 行 max|Δ|={diff:.3e} "
          f"→ {'PASS' if diff == 0.0 else 'FAIL'}")
    assert diff == 0.0, "全品种截面存在前视泄漏！"
    # 附：受影响窗 [cut-h, cut) 确实变化 → 扰动有效（避免退化测试）
    aff = (panel.iloc[cut - HORIZON: cut] - panel2.iloc[cut - HORIZON: cut]).abs().max().max()
    print(f"  [ZERO-LEAK] 受影响窗 [cut-h, cut) max|Δ|={aff:.4f}（>0 即扰动有效）")
    assert aff > 0, "扰动未生效，测试无效"


# ---------------------------------------------------------------------------
# Stage 4：m0 入选率 / 选组均衡性（与 QA P8-3 口径一致：含 px 对齐）
# ---------------------------------------------------------------------------
def selection_balance(cache_path: Path, prices: pd.DataFrame) -> pd.DataFrame:
    """OOS 期间引擎 A（top30% + min=3）选中品种按组分布 + m0 专项。

    返回长表：group / n_syms / sel_pct（选中占比）/ sig_pct（信号占比，公平参照）。
    """
    from scripts.group_modeling_v2 import GROUPS_V2 as G2

    sym2group: dict[str, str] = {}
    for gname, gcfg in G2.items():
        for s in gcfg["syms"]:
            sym2group[s] = gname

    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig[sig["ts"] >= pd.Timestamp(OOS_START)]
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["selected"] = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMBOLS_S2)
    sig["group"] = sig["symbol"].map(sym2group)

    sel = sig[sig["selected"]]
    n_sel = len(sel)
    n_sig = len(sig)
    rows = []
    for gname, g in sig.groupby("group"):
        n_syms = int(g["symbol"].nunique())
        sel_pct = float(sel[sel["group"] == gname].shape[0] / n_sel * 100) if n_sel else np.nan
        sig_pct = float(g.shape[0] / n_sig * 100) if n_sig else np.nan
        rows.append({"group": gname, "n_syms": n_syms,
                     "sel_pct": round(sel_pct, 2), "sig_pct": round(sig_pct, 2)})
    out = pd.DataFrame(rows).sort_values("sel_pct", ascending=False)
    # m0 专项：入选占比 + 入选天数占比（m0 有信号且当日品种数>=3 的日子中，m0 被选中的比例）
    m0 = sig[sig["symbol"] == "m0"]
    m0_cand = m0[m0["_day_cnt"] >= MIN_SYMBOLS_S2]
    m0_sel = m0_cand[m0_cand["selected"]]
    m0_share = float(sel[sel["symbol"] == "m0"].shape[0] / n_sel * 100) if n_sel else np.nan
    m0_day = float(m0_sel.shape[0] / m0_cand.shape[0] * 100) if len(m0_cand) else np.nan
    out.loc["_m0_share"] = {"group": "_m0_share", "n_syms": 1,
                            "sel_pct": round(m0_share, 2) if pd.notna(m0_share) else np.nan,
                            "sig_pct": round(float(m0.shape[0] / n_sig * 100), 2) if n_sig else np.nan}
    out.loc["_m0_day_sel_ratio"] = {"group": "_m0_day_sel_ratio", "n_syms": 1,
                                    "sel_pct": round(m0_day, 2) if pd.notna(m0_day) else np.nan,
                                    "sig_pct": np.nan}
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P8-4 全 18 品种统一截面重构（重训 v8 + 重估）")
    ap.add_argument("--n-jobs", type=int, default=8, help="walk-forward 折级并行进程数")
    ap.add_argument("--skip-train", action="store_true", help="跳过重训（v8 已存在）")
    ap.add_argument("--v9", action="store_true", help="补跑 v9（cross_rank+all）作对照")
    ap.add_argument("--caches", type=str, default="v4,v7,v8",
                    help="重估缓存版本列表（逗号分隔；默认 v4,v7,v8；含 v9 需同时 --v9）")
    args = ap.parse_args()

    t_start = time.time()
    print("=" * 96)
    print("P8-4 全 18 品种统一截面重构（label_pool=all）")
    print(f"n_jobs={args.n_jobs} skip_train={args.skip_train} v9={args.v9} caches={args.caches}")
    print("=" * 96)

    # ---- Stage 0: 重训 v8（cross_z + all） ----
    if not args.skip_train:
        print("-" * 96)
        print("[Stage 0] 重训 v8（cross_z + label_pool=all）")
        train_cache("cross_z", "all", V8_PATH, args.n_jobs)
        if args.v9:
            print("-" * 96)
            print("[Stage 0b] 重训 v9（cross_rank + label_pool=all）对照")
            train_cache("cross_rank", "all", V9_PATH, args.n_jobs)
    else:
        print("[Stage 0] --skip-train：使用现有缓存")
        for tag, p, _, _ in VERSIONS:
            if p.exists():
                sig = pd.read_parquet(p)
                print(f"  [{tag}] 存在 {len(sig)} 条 ({p.name})")

    # ---- Stage 0.5: 全品种面板零泄漏实证 ----
    print("-" * 96)
    print("[Stage 0.5] 全品种面板零泄漏实证（真实数据扰动）")
    zero_leakage_check()

    # ---- 环境 ----
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    print(f"[env] prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | 复利口径评估")

    caches = [c.strip() for c in args.caches.split(",") if c.strip()]
    path_of = {tag: p for tag, p, _, _ in VERSIONS}

    # ---- Stage 1: 覆盖率 ----
    print("-" * 96)
    print("[Stage 1] 覆盖率 v4/v7/v8[/v9]")
    cov = pd.concat([coverage_stats(path_of[c]) for c in caches if path_of[c].exists()],
                    ignore_index=True)
    ART.mkdir(exist_ok=True)
    cov.to_csv(OUT_COV, index=False)
    print(cov[["cache", "n_signals", "n_days", "avg_symbols_per_day", "min_symbols_day",
               "max_symbols_day", "n_symbols", "ts_start", "ts_end"]].to_string(index=False))
    print(f"  [OK] → {OUT_COV}")

    # ---- Stage 2: 引擎 A S2（复利口径）+ 组合 A15/B85 ----
    print("-" * 96)
    print("[Stage 2] 引擎 A S2 单引擎（复利口径）+ 组合 A15/B85")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    rows: list[dict] = []
    rets: dict[str, pd.Series] = {}
    meta = {tag: (lm, lp) for tag, _, lm, lp in VERSIONS}
    for c in caches:
        cp = path_of[c]
        if not cp.exists():
            print(f"  [WARN] {cp} 不存在，跳过")
            continue
        label_mode, label_pool = meta[c]
        tgt = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cp)
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt, f"A-{c}")
        rets[c] = ret
        rows.append({
            "cache": c, "engine": "A-S2", "label_mode": label_mode, "label_pool": label_pool,
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,   # 复利口径（OOS 权益总收益）
            "oos_ann_ret": m_oos.annual_return if m_oos else np.nan,
            "long_day_ratio": long_ratio, "vol_target": "N",
        })
        print(f"  [A-S2 {c}] Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={m_oos.sharpe:.3f} "
              f"OOS 复利={m_oos.total_return*100:+.2f}% OOS MaxDD={m_oos.max_drawdown*100:.1f}% "
              f"| 做多天数占比={long_ratio*100:.1f}%")

        for vt in (False, True):
            ra = ret.loc[ret.index.intersection(ret_b.index)]
            rb = ret_b.loc[ret.index.intersection(ret_b.index)]
            r = combo_stats_row(ra, rb, PROD_W_A, vt)
            rows.append({
                "cache": c, "engine": f"A{PROD_W_A}/B{PROD_W_B}",
                "label_mode": label_mode, "label_pool": label_pool,
                "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
                "maxdd_full": r["maxdd_full"],
                "oos_sharpe": r["oos_sharpe"], "oos_maxdd": r["oos_maxdd"],
                "oos_ret": r["oos_ret"], "oos_ann_ret": np.nan,
                "long_day_ratio": np.nan, "vol_target": "Y" if vt else "N",
            })
            print(f"  [combo {c} A{PROD_W_A}/B{PROD_W_B} vol={'Y' if vt else 'N'}] "
                  f"Sharpe={r['sharpe_full']:.3f} | OOS Sharpe={r['oos_sharpe']:.3f}")

    tbl = pd.DataFrame(rows)
    tbl.to_csv(OUT_ENG, index=False)
    print(f"  [OK] → {OUT_ENG}")

    # ---- Stage 3: exp_ret 截面 IC ----
    print("-" * 96)
    print("[Stage 3] exp_ret 截面 IC（每日截面 Spearman(exp_ret, realized)）")
    ic_rows = []
    for c in caches:
        cp = path_of[c]
        if not cp.exists():
            continue
        sig = pd.read_parquet(cp)
        summ = cs_ic_summary(sig, realized, str(OOS_START))
        for seg, st in summ.items():
            if seg == "pooled_rank_ic":
                continue
            ic_rows.append({"cache": c, "segment": seg, **st})
        print(f"  [{c}] pooled rank_ic={summ['pooled_rank_ic']:.4f}")
        for seg in ("full", "is", "oos"):
            st = summ[seg]
            print(f"      {seg:<5} n_days={st['n_days']:>4} IC均值={st['ic_mean']:+.4f} "
                  f"ICIR={st['icir']:+.3f} 正IC占比={st['pos_ratio']*100:5.1f}%")
    ic_tbl = pd.DataFrame(ic_rows)
    ic_tbl.to_csv(OUT_IC, index=False)
    print(f"  [OK] → {OUT_IC}")

    # ---- Stage 4: m0 入选率 / 选组均衡性 ----
    print("-" * 96)
    print("[Stage 4] m0 入选率 / 选组均衡性（根治组规模偏置的直接证据，OOS）")
    sel_rows = []
    for c in caches:
        cp = path_of[c]
        if not cp.exists():
            continue
        sb = selection_balance(cp, prices)
        sb.insert(0, "cache", c)
        sel_rows.append(sb)
        print(f"  [{c}] OOS 选中按组分布：")
        print(sb.round(2).to_string(index=False))
    sel_tbl = pd.concat(sel_rows, ignore_index=True)
    sel_tbl.to_csv(OUT_SEL, index=False)
    print(f"  [OK] → {OUT_SEL}")

    # ---- Stage 5: 关键判断 + 裁决 ----
    print("=" * 96)
    print("[Stage 5] 关键判断（复利口径 + m0 恢复 + 选组均衡 + 组合）")
    verdict = verdict_report(tbl, sel_tbl, caches)
    with open(OUT_VERDICT, "w", encoding="utf-8") as f:
        json_dump(verdict, f)
    print(f"  [OK] → {OUT_VERDICT}")
    print("=" * 96)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s")
    print("=" * 96)


def json_dump(obj, f):
    import json

    def _san(v):
        if isinstance(v, (np.floating, float)):
            v = float(v)
            return None if (np.isnan(v) or np.isinf(v)) else v
        if isinstance(v, (np.integer,)):
            return int(v)
        return v

    json.dump(obj, f, ensure_ascii=False, indent=2, default=_san)


def verdict_report(tbl: pd.DataFrame, sel_tbl: pd.DataFrame, caches: list[str]) -> dict:
    """输出 v4/v7/v8[/v9] 对比摘要 + 采纳建议（如实报告）。"""

    def _row(cache: str, engine: str, vol: str = "N") -> pd.Series:
        m = tbl[(tbl["cache"] == cache) & (tbl["engine"] == engine) & (tbl["vol_target"] == vol)]
        return m.iloc[0] if len(m) else None

    def _m0(cache: str, col: str):
        r = sel_tbl[(sel_tbl["cache"] == cache) & (sel_tbl["group"] == "_m0_share")]
        if len(r):
            return float(r.iloc[0][col])
        return np.nan

    def _balance_dev(cache: str) -> float:
        """选组均衡度：8 组 |sel_pct - sig_pct| 的均值（越小越接近按信号占比均衡）。"""
        r = sel_tbl[(sel_tbl["cache"] == cache) &
                    ~sel_tbl["group"].str.startswith("_")]
        if len(r) == 0:
            return np.nan
        return float(np.abs(r["sel_pct"] - r["sig_pct"]).mean())

    summary = {}
    for c in caches:
        a = _row(c, "A-S2")
        combo = _row(c, f"A{PROD_W_A}/B{PROD_W_B}")
        summary[c] = {
            "A_S2_oos_sharpe": float(a["oos_sharpe"]) if a is not None else np.nan,
            "A_S2_oos_compound_ret": float(a["oos_ret"]) if a is not None else np.nan,  # 复利口径
            "A_S2_oos_ann_ret": float(a["oos_ann_ret"]) if a is not None else np.nan,
            "A_S2_full_sharpe": float(a["sharpe_full"]) if a is not None else np.nan,
            "A_S2_full_maxdd": float(a["maxdd_full"]) if a is not None else np.nan,
            "combo_oos_sharpe": float(combo["oos_sharpe"]) if combo is not None else np.nan,
            "combo_oos_compound_ret": float(combo["oos_ret"]) if combo is not None else np.nan,
            "m0_sel_share_oos": _m0(c, "sel_pct"),
            "m0_day_sel_ratio_oos": _m0_day(sel_tbl, c),
            "balance_dev": _balance_dev(c),
        }
        print(f"  [{c}] A-S2 OOS Sharpe={summary[c]['A_S2_oos_sharpe']:+.3f} "
              f"OOS 复利={summary[c]['A_S2_oos_compound_ret']*100:+.2f}% "
              f"| combo OOS Sharpe={summary[c]['combo_oos_sharpe']:.3f} "
              f"| m0 选中占比={summary[c]['m0_sel_share_oos']:.1f}% "
              f"m0 入选天数占比={summary[c]['m0_day_sel_ratio_oos']:.1f}% "
              f"| 选组均衡偏差={summary[c]['balance_dev']:.1f}pp")

    # 裁决：v8 vs v4/v7
    v8 = summary.get("v8", {})
    v4 = summary.get("v4", {})
    v7 = summary.get("v7", {})
    m0_base = max(
        [v for v in (v4.get("m0_sel_share_oos"), v7.get("m0_sel_share_oos")) if pd.notna(v)],
        default=np.nan,
    )
    checks = {
        # m0 恢复：v8 m0 选中占比 ≥ v4/v7 基线（全品种截面应让 m0 回到公平份额附近）
        "m0_recovered": bool(
            pd.notna(v8.get("m0_sel_share_oos")) and pd.notna(m0_base)
            and v8["m0_sel_share_oos"] >= m0_base
        ),
        # 选组均衡改善：v8 组间偏差 < v4 基线
        "balance_improved": bool(
            pd.notna(v8.get("balance_dev")) and pd.notna(v4.get("balance_dev"))
            and v8["balance_dev"] < v4["balance_dev"]
        ),
        "engine_compound_positive": bool(pd.notna(v8.get("A_S2_oos_compound_ret")) and v8["A_S2_oos_compound_ret"] > 0),
        "engine_sharpe_improved_vs_v4": bool(
            pd.notna(v8.get("A_S2_oos_sharpe")) and pd.notna(v4.get("A_S2_oos_sharpe"))
            and v8["A_S2_oos_sharpe"] > v4["A_S2_oos_sharpe"]
        ),
        "combo_improved_vs_v4": bool(
            pd.notna(v8.get("combo_oos_sharpe")) and pd.notna(v4.get("combo_oos_sharpe"))
            and v8["combo_oos_sharpe"] > v4["combo_oos_sharpe"]
        ),
        "combo_improved_vs_v7": bool(
            pd.notna(v8.get("combo_oos_sharpe")) and pd.notna(v7.get("combo_oos_sharpe"))
            and v8["combo_oos_sharpe"] > v7["combo_oos_sharpe"]
        ),
    }
    print("  校验：")
    print(f"    m0 入选恢复（v8 ≥ max(v4,v7) 基线）: {'PASS' if checks['m0_recovered'] else 'FAIL'}")
    print(f"    选组均衡改善（v8 组间偏差 < v4）: {'PASS' if checks['balance_improved'] else 'FAIL'}")
    print(f"    引擎 A OOS 复利转正: {'PASS' if checks['engine_compound_positive'] else 'FAIL'}")
    print(f"    引擎 A OOS Sharpe 优于 v4: {'PASS' if checks['engine_sharpe_improved_vs_v4'] else 'FAIL'}")
    print(f"    组合 A15/B85 OOS 优于 v4(1.384): {'PASS' if checks['combo_improved_vs_v4'] else 'FAIL'}")
    print(f"    组合 A15/B85 OOS 优于 v7(1.399): {'PASS' if checks['combo_improved_vs_v7'] else 'FAIL'}")

    # 采纳建议（如实报告——全品种截面后仍不转正也如实记录）
    if checks["m0_recovered"] and checks["engine_compound_positive"] and checks["combo_improved_vs_v4"]:
        adopt = "ADOPT"
        reason = "m0 恢复 + 引擎 A OOS 复利转正 + 组合提升，全品种统一截面达成目标"
    elif checks["m0_recovered"] and checks["combo_improved_vs_v4"] and checks["engine_sharpe_improved_vs_v4"]:
        adopt = "CANDIDATE"
        reason = "m0 恢复且组合/引擎改善，但引擎 A OOS 复利未转正（可能仍亏损），只宜作候选"
    elif checks["m0_recovered"]:
        adopt = "CANDIDATE"
        reason = "m0 恢复（组规模偏置已根治）但盈利未达阈值，需进一步验证"
    else:
        adopt = "NOT-ADOPT"
        reason = "m0 未恢复或盈利未改善，全品种统一截面未达成目标"
    print(f"  采纳建议: {adopt} —— {reason}")
    return {"summary": summary, "checks": checks, "adopt": adopt, "reason": reason}


def _m0_day(sel_tbl: pd.DataFrame, cache: str) -> float:
    r = sel_tbl[(sel_tbl["cache"] == cache) & (sel_tbl["group"] == "_m0_day_sel_ratio")]
    return float(r.iloc[0]["sel_pct"]) if len(r) else np.nan


if __name__ == "__main__":
    main()
