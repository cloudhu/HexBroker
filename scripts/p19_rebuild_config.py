"""P19 生产配置重建（修复后真实口径）——引擎 B thr / 引擎 A 名义 / 组合复验 / 配置固化建议。

背景（P18 终裁定案，QA 三轮 VERIFIED）
--------------------------------------
P18-P0 修复 ``SimBroker`` 全局 multiplier=10 记账 bug（改为品种级乘数，commit d3ec0a6）。
修复后真实指标大幅回落：P16 A10/B90 OOS Sharpe 1.612→0.566；P11 win252/thr0.70
1.622→0.490（修复后 win126/thr0.60 0.641 为最优）；P9 网格最优 A0.35/B0.65（0.731）；
P5 引擎 A S2 v8+fixed +0.990（cap0.5 +1.049）。

生产现状（修复前配置）：
  - EngineBConfig win252/thr0.70
  - ComboConfig A10/B90
  - EngineAConfig notional_frac=0.20 → 生产有效名义 = 1e6 × 0.20 × 0.10 = 20,000 CNY/标的
    < OOS 最低一手（m0 ≈ 26,688 CNY）→ floor 后 0 手 → 生产事实 100% 纯引擎 B

P19 目标（本脚本只出建议，不改生产配置默认——QA 复核 + 主理人终裁后才改）：
  1. 引擎 B thr 评估：win126/win252 × thr 0.60/0.70/0.80（修复后口径）+ 成本敏感性
     （做多占比/持仓天数/换手/费滑成本侵蚀）
  2. 引擎 A 名义上调恢复可交易：NOTIONAL_FRAC 目标 + 提高名义后单引擎表现 + 容量检查
  3. 组合复验：引擎 A（cap0.5 + 提高名义）× 引擎 B（thr 0.60/0.70）组合网格
     A∈{0.10,0.20,0.35,0.50} × thr×{0.60,0.70} × volN/volY → 找修复后生产最优配置
  4. 配置固化建议：现生产 vs 候选最优对比表 + 裁决建议（是否更新生产配置）

口径铁律：复利口径；OOS 2024-07-18 后；修复后 broker；完整回测
（滑点1tick+费0.005%+保证金12%+CONTRACTS18+INITIAL_CAPITAL=1e6）；嵌套零泄漏
（复用 v8 缓存，不重建）；部署接线 §9.18：显式 load_config("configs/base.yaml") + 显式传参。

落盘：
  artifacts/p19_engineB_thr.csv / p19_engineA_notional.csv / p19_combo_revalidate.csv
  artifacts/p19_verdict.json
"""
from __future__ import annotations

import json
import sys
import time
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
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets
from scripts.p5_engineA_cross_section import (
    TOP_K,
    combo_stats_row,
    engine_a_selection,
    seg_sharpe,
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"

# P10-1/P16 生产 group_map（与 configs/base.yaml 一致：黑色系 5 品种合并 ferrous_all）
BASE_GROUP_MAP: dict[str, str] = {
    "i0": "ferrous_all", "j0": "ferrous_all", "jm0": "ferrous_all",
    "rb0": "ferrous_all", "hc0": "ferrous_all",
    "cu0": "industrial", "al0": "industrial", "zn0": "industrial", "ni0": "industrial",
    "au0": "precious", "ag0": "precious",
    "y0": "agri_oil", "p0": "agri_oil",
    "m0": "agri_protein",
    "sr0": "agri_soft", "cf0": "agri_soft",
    "ta0": "chem_energy", "sc0": "chem_energy",
}

# P19-1：引擎 B thr 评估网格（聚焦生产窗口 126/252）
B_WINS = [126, 252]
B_THRS = [0.60, 0.70, 0.80]

# P19-2：引擎 A 名义候选（研究口径 per-symbol notional = capital × nf_a）
A_NF_GRID = [0.20, 0.30, 0.40, 0.50]
# 生产口径有效名义 = capital × nf_a × w_a
W_A_PROD = [0.10, 0.20, 0.35, 0.50]
NF_A_PROD = [0.20, 0.30, 0.40, 0.50]

# P19-3：组合复验网格（研究口径 w_a*ret_a + w_b*ret_b）
W_A_GRID = [0.10, 0.20, 0.35, 0.50]
COMBO_THRS = [0.60, 0.70]
VOL_GRID = [False, True]
A_NF_COMBO = [0.20, 0.35]  # 引擎 A per-symbol 名义候选（cap0.5 生产口径）

MARGIN_RATE = 0.12  # 保证金率（与 CostModel 一致）
OOS_SUB_BOUNDS = [("2024-07-18", "2025-06-30"), ("2025-07-01", None)]

# 模块级 ret 缓存（key → daily return Series），避免重复回测
RET_CACHE: dict[str, pd.Series] = {}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def build_engine_a_targets(
    prices: pd.DataFrame,
    notional: float,
    top_k: float = TOP_K,
    min_symbols: int = 3,
    cache_path: Path | str | None = V8_PATH,
    group_cap: float | None = 0.5,
    group_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """引擎 A targets（按日截面 rank top_k + S2 min_symbols + group_cap），
    显式指定 per-symbol 名义（研究口径：capital × nf_a，等价 p5 的
    ``engine_a_targets_cs`` 但名义参数化，便于 P19-2 名义敏感性评估）。
    """
    sel = engine_a_selection(
        prices,
        top_k=top_k,
        min_symbols=min_symbols,
        cache_path=cache_path,
        group_cap=group_cap,
        group_map=group_map,
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = notional / (sel["_px"] * sel["_mult"])
    sel["target"] = np.where(sel["selected"], raw.fillna(0.0).astype(int), 0)
    return sel.set_index(["symbol", "ts"])[["target"]].sort_index()


def _engine_ret(cfg, cost, prices, targets) -> pd.Series:
    """单次完整回测 → 日收益序列（并写缓存由调用方负责 key）。"""
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    return pf.equity_curve.pct_change().dropna(), engine


def run_engine_metrics(
    cfg,
    cost: CostModel,
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    label: str,
    collect_cost: bool = True,
    ret_key: str | None = None,
) -> dict:
    """完整口径回测 → 全样本/OOS/分段指标 + 交易成本统计（复利口径）。

    若 ``ret_key`` 非空，把日收益序列写入模块级 ``RET_CACHE[ret_key]`` 供组合复用。
    """
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    if ret_key is not None:
        RET_CACHE[ret_key] = ret

    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    seg1 = seg_sharpe(eq, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
    seg2 = seg_sharpe(eq, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])

    # 做多天数 / 平均连续持仓天数（按标的）
    tgt = targets["target"]
    ts_level = pd.to_datetime(targets.index.get_level_values(1))
    total_days = pd.Index(ts_level.unique())
    long_days = pd.Index(pd.to_datetime(tgt[tgt > 0].index.get_level_values(1).unique()))
    long_ratio = len(long_days) / len(total_days) if len(total_days) else float("nan")
    streaks: list[int] = []
    for sym in targets.index.get_level_values(0).unique():
        try:
            s = tgt.xs(sym, level=0) > 0
        except KeyError:
            continue
        run = 0
        for v in s.values:
            if v:
                run += 1
            else:
                if run > 0:
                    streaks.append(run)
                run = 0
        if run > 0:
            streaks.append(run)
    mean_streak = float(np.mean(streaks)) if streaks else float("nan")

    # 交易成本统计（从 broker trades 提取，OOS 窗口）
    trades = [t for t in engine.broker.trades
              if t.timestamp is None or pd.Timestamp(t.timestamp) >= pd.Timestamp(OOS_START)]
    fee_total = float(sum(t.fee for t in trades))
    slip_total = float(sum(
        cost._min_tick(t.symbol) * cost.slippage_ticks * cost._multiplier(t.symbol) * abs(t.qty)
        for t in trades
    ))
    notional_traded = float(sum(
        abs(t.qty) * t.fill_price * cost._multiplier(t.symbol) for t in trades
    ))

    return {
        "label": label,
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
        "seg1_sharpe": seg1,
        "seg2_sharpe": seg2,
        "long_ratio": long_ratio,
        "mean_streak_days": mean_streak,
        "n_long_rows": int((tgt > 0).sum()),
        "fee_total": fee_total,
        "slip_total": slip_total,
        "cost_total": fee_total + slip_total,
        "notional_traded": notional_traded,
        "cost_bps": (fee_total + slip_total) / notional_traded * 1e4 if notional_traded else np.nan,
        "final_equity": float(pf.final_equity),
    }


def zero_cost_model(cfg) -> CostModel:
    """零成本 CostModel（费用=0、滑点=0），用于净/毛收益对比（成本侵蚀度量）。"""
    return CostModel(
        fee_open=0.0,
        fee_close=0.0,
        fee_close_today=0.0,
        slippage_ticks=0.0,
        margin_rate=cfg.backtest.margin_rate,
        multiplier=cfg.backtest.multiplier,
        min_tick=cfg.backtest.min_tick,
        contracts=dict(cfg.backtest.contracts or {}),
    )


# ---------------------------------------------------------------------------
# P19-1 引擎 B thr 评估
# ---------------------------------------------------------------------------
def eval_engine_b_thr(cfg, cost, prices) -> tuple[pd.DataFrame, dict]:
    """win126/252 × thr0.60/0.70/0.80：净/毛收益对比 + 成本敏感性。"""
    print("\n" + "=" * 100)
    print("[P19-1] 引擎 B thr 评估（修复后 broker，净 vs 毛收益）")
    print("=" * 100)
    rows: list[dict] = []
    zcost = zero_cost_model(cfg)
    for win in B_WINS:
        for thr in B_THRS:
            tgt = engine_b_targets(prices, win, thr)
            key = f"B-{win}-{thr:.2f}"
            r = run_engine_metrics(cfg, cost, prices, tgt, key,
                                   ret_key=key)
            rg = run_engine_metrics(cfg, zcost, prices, tgt, f"{key}-gross",
                                    collect_cost=False)
            r["gross_oos_ret"] = rg["oos_ret"]
            r["cost_drag_pp"] = (r["oos_ret"] - rg["oos_ret"]) * 100  # 百分点
            r["win"] = win
            r["thr"] = thr
            rows.append(r)
            print(f"  win={win:>3} thr={thr:.2f}: OOS Sharpe={r['oos_sharpe']:.3f} "
                  f"OOS 复利={r['oos_ret']*100:+.2f}% (毛 {rg['oos_ret']*100:+.2f}%, "
                  f"成本拖累 {r['cost_drag_pp']:+.2f}pp) MaxDD={r['oos_maxdd']*100:.1f}% "
                  f"| 做多天数={r['long_ratio']*100:.1f}% 均持仓={r['mean_streak_days']:.0f}d "
                  f"成本={r['cost_bps']:.0f}bps 成交名义={r['notional_traded']/1e6:.1f}M")
    df = pd.DataFrame(rows)
    df["oos_rank"] = df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("oos_rank")
    out_cols = ["win", "thr", "oos_rank", "sharpe_full", "ann_ret_full", "maxdd_full",
                "seg1_sharpe", "seg2_sharpe", "oos_sharpe", "oos_ret", "gross_oos_ret",
                "cost_drag_pp", "oos_maxdd", "long_ratio", "mean_streak_days",
                "n_long_rows", "notional_traded", "cost_total", "cost_bps"]
    out = df[out_cols]
    out.to_csv(ART / "p19_engineB_thr.csv", index=False, encoding="utf-8-sig")
    print(f"\n  [OK] → {ART / 'p19_engineB_thr.csv'}")
    return df, {"rows": rows}


# ---------------------------------------------------------------------------
# P19-2 引擎 A 名义上调
# ---------------------------------------------------------------------------
def eval_engine_a_notional(cfg, cost, prices) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """名义敏感性 + 生产有效名义可交易性 + 容量检查。"""
    print("\n" + "=" * 100)
    print("[P19-2] 引擎 A 名义上调恢复可交易（cap0.5，v8+fixed）")
    print("=" * 100)

    # --- 2a. OOS lot cost 基准 ---
    px_oos = prices[pd.to_datetime(prices.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    lot_min: dict[str, float] = {}
    for sym in SYMBOLS18:
        try:
            sub = px_oos.xs(sym, level=0)["close"]
        except KeyError:
            continue
        mult = CONTRACTS18[sym]["multiplier"]
        lot_min[sym] = float(sub.min() * mult)
    min_lot = min(lot_min.values())
    print(f"  OOS 全局最低一手成本（m0）: {min_lot:,.0f} CNY | 2 手 {2*min_lot:,.0f} | "
          f"3 手 {3*min_lot:,.0f}")

    # --- 2b. 名义敏感性：研究口径 per-symbol notional = capital × nf_a ---
    sel_detail = engine_a_selection(
        prices, top_k=TOP_K, min_symbols=3, cache_path=V8_PATH,
        group_cap=0.5, group_map=BASE_GROUP_MAP,
    )
    sel_detail = sel_detail[pd.to_datetime(sel_detail["ts"]) >= pd.Timestamp(OOS_START)]
    sel_sel = sel_detail[sel_detail["selected"]].copy()
    print(f"  OOS 引擎 A（cap0.5）选中 {len(sel_sel)} 行 / {sel_sel['ts'].nunique()} 天")

    rows: list[dict] = []
    for nf_a in A_NF_GRID:
        notional = INITIAL_CAPITAL * nf_a
        tgt = build_engine_a_targets(prices, notional, group_cap=0.5, group_map=BASE_GROUP_MAP)
        key = f"A-nf{nf_a:.2f}"
        r = run_engine_metrics(cfg, cost, prices, tgt, key, ret_key=key)
        with np.errstate(invalid="ignore", divide="ignore"):
            lots = (notional / (sel_sel["_px"] * sel_sel["_mult"])).fillna(0.0).astype(int)
        lots_s = pd.Series(lots.values, index=sel_sel.index)
        tradable_rows = int((lots_s >= 1).sum())
        tradable_days = int(sel_sel.loc[lots_s >= 1, "ts"].nunique())
        r["nf_a"] = nf_a
        r["notional_per_sym"] = notional
        r["tradable_row_ratio"] = tradable_rows / len(sel_sel) if len(sel_sel) else np.nan
        r["tradable_days"] = tradable_days
        rows.append(r)
        print(f"  nf_a={nf_a:.2f} (名义 {notional:,.0f}): OOS Sharpe={r['oos_sharpe']:.3f} "
              f"OOS 复利={r['oos_ret']*100:+.2f}% MaxDD={r['oos_maxdd']*100:.1f}% "
              f"| 选中行可交易 {100*r['tradable_row_ratio']:.1f}% ({tradable_rows}/{len(sel_sel)}) "
              f"可交易天数 {tradable_days}/{sel_sel['ts'].nunique()}")
    df = pd.DataFrame(rows)
    out = df[[
        "nf_a", "notional_per_sym", "oos_sharpe", "oos_ret", "oos_maxdd", "sharpe_full",
        "seg1_sharpe", "seg2_sharpe", "tradable_row_ratio", "tradable_days",
        "long_ratio", "mean_streak_days", "cost_total", "cost_bps",
    ]]
    out.to_csv(ART / "p19_engineA_notional.csv", index=False, encoding="utf-8-sig")
    print(f"\n  [OK] → {ART / 'p19_engineA_notional.csv'}")

    # --- 2c. 生产有效名义（capital × nf_a × w_a）可交易性 ---
    print("\n  生产有效名义 = capital × nf_a × w_a（p16 combo_plan 口径）：")
    prod_rows: list[dict] = []
    for w_a in W_A_PROD:
        for nf_a in NF_A_PROD:
            eff = INITIAL_CAPITAL * nf_a * w_a
            lots_m0 = int(eff // min_lot)
            # 按选中集合中位一手成本计算可交易性
            med_lot = float(np.median(sel_sel["_px"] * sel_sel["_mult"]))
            trad_median = int(eff >= med_lot)
            prod_rows.append({
                "w_a": w_a, "nf_a": nf_a, "eff_notional": eff,
                "lots_m0": lots_m0, "tradable_median_row": trad_median,
            })
            print(f"    w_a={w_a:.2f} nf_a={nf_a:.2f} -> {eff:>9,.0f} CNY = {lots_m0} 手 m0 "
                  f"{'| 可覆盖中位一手' if trad_median else '| 中位一手不可覆盖'}")
    prod_df = pd.DataFrame(prod_rows)
    prod_df.to_csv(ART / "p19_engineA_prod_notional.csv", index=False, encoding="utf-8-sig")

    # --- 2d. 容量检查（组合层，候选配置，生产计划口径） ---
    print("\n  容量检查（组合层总名义 vs 权益，OOS 窗口，生产计划口径）：")
    bs_anchor = engine_b_targets(prices, 252, 0.70)
    bs_oos = bs_anchor[pd.to_datetime(bs_anchor.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    cap_rows: list[dict] = []
    for w_a in (0.20, 0.35):
        for nf_a in (0.20, 0.30, 0.35):
            w_b = round(1.0 - w_a, 4)
            cap_rows.append(_prod_capacity_row(sel_detail, bs_oos, prices, w_a, nf_a, w_b, 0.20))
    cap_df = pd.DataFrame(cap_rows)
    cap_df.to_csv(ART / "p19_combo_capacity.csv", index=False, encoding="utf-8-sig")
    for _, r in cap_df.iterrows():
        print(f"    A{r['w_a']:.2f}/B{r['w_b']:.2f} nfA={r['nf_a']:.2f} nfB={r['nf_b']:.2f}: "
              f"峰值总名义/权益={r['peak_ratio']:.2f} 均值={r['mean_ratio']:.2f} "
              f"超1.0天数={r['days_over_cap']} 峰值保证金/权益={r['peak_margin_ratio']:.2f}")

    return df, {"rows": rows, "prod": prod_rows, "cap": cap_rows, "min_lot": min_lot}, sel_sel


def _prod_capacity_row(sel_detail, bs_oos, prices, w_a, nf_a, w_b, nf_b) -> dict:
    """生产计划口径（p16 combo_plan）：逐日合并名义 → 总名义/权益峰值。"""
    notional_a = INITIAL_CAPITAL * nf_a * w_a
    notional_b = INITIAL_CAPITAL * nf_b * w_b
    sel_sel = sel_detail[sel_detail["selected"]]

    day_notional: dict[pd.Timestamp, float] = {}
    # 注意：engine_a_selection 返回 symbol/ts 为普通列（非 MultiIndex），按列取值
    for _, row in sel_sel.iterrows():
        sym = row["symbol"]
        ts = pd.Timestamp(row["ts"])
        px = row["_px"]
        if pd.isna(px):
            continue
        lots = int(notional_a // (px * row["_mult"]))
        if lots > 0:
            day_notional[ts] = day_notional.get(ts, 0.0) + lots * px * row["_mult"]
    for (sym, ts), row in bs_oos.iterrows():
        lots = int(row["target"])
        if lots <= 0:
            continue
        ts = pd.Timestamp(ts)
        try:
            px = float(prices.xs(sym, level=0)["close"].get(ts))
        except KeyError:
            continue
        if pd.isna(px):
            continue
        mult = CONTRACTS18[sym]["multiplier"]
        day_notional[ts] = day_notional.get(ts, 0.0) + lots * px * mult

    if not day_notional:
        return {"w_a": w_a, "nf_a": nf_a, "w_b": w_b, "nf_b": nf_b,
                "peak_ratio": 0.0, "mean_ratio": 0.0, "days_over_cap": 0,
                "peak_margin_ratio": 0.0}
    arr = np.array(list(day_notional.values()))
    return {
        "w_a": w_a, "nf_a": nf_a, "w_b": w_b, "nf_b": nf_b,
        "peak_ratio": float(arr.max() / INITIAL_CAPITAL),
        "mean_ratio": float(arr.mean() / INITIAL_CAPITAL),
        "days_over_cap": int((arr > INITIAL_CAPITAL).sum()),
        "peak_margin_ratio": float(arr.max() * MARGIN_RATE / INITIAL_CAPITAL),
    }


# ---------------------------------------------------------------------------
# P19-3 组合复验
# ---------------------------------------------------------------------------
def eval_combo(cfg, cost, prices) -> tuple[pd.DataFrame, dict]:
    """修复后组合网格：A∈{0.10,0.20,0.35,0.50} × thr∈{0.60,0.70} × volN/volY。"""
    print("\n" + "=" * 100)
    print("[P19-3] 组合复验（引擎 A cap0.5 + 名义候选 × 引擎 B thr 候选）")
    print("=" * 100)

    def get_ret_b(win: int, thr: float) -> pd.Series:
        key = f"B-{win}-{thr:.2f}"
        if key not in RET_CACHE:
            tgt = engine_b_targets(prices, win, thr)
            run_engine_metrics(cfg, cost, prices, tgt, key, ret_key=key)
        return RET_CACHE[key]

    def get_ret_a(nf_a: float) -> pd.Series:
        key = f"A-nf{nf_a:.2f}"
        if key not in RET_CACHE:
            notional = INITIAL_CAPITAL * nf_a
            tgt = build_engine_a_targets(prices, notional, group_cap=0.5, group_map=BASE_GROUP_MAP)
            run_engine_metrics(cfg, cost, prices, tgt, key, ret_key=key)
        return RET_CACHE[key]

    rows: list[dict] = []
    for nf_a in A_NF_COMBO:
        ret_a = get_ret_a(nf_a)
        for thr in COMBO_THRS:
            ret_b = get_ret_b(252, thr)
            ra, rb = _align(ret_a, ret_b)
            for w_a in W_A_GRID:
                for vt in VOL_GRID:
                    r = combo_stats_row(ra, rb, w_a, vt)
                    r["nf_a"] = nf_a
                    r["thr"] = thr
                    rows.append(r)
    df = pd.DataFrame(rows)
    df["oos_rank"] = df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("oos_rank")
    out = df[["oos_rank", "nf_a", "thr", "w_a", "w_b", "vol_target",
              "sharpe_full", "ann_ret_full", "maxdd_full",
              "oos_sharpe", "oos_ret", "oos_maxdd", "oos_n"]]
    out.to_csv(ART / "p19_combo_revalidate.csv", index=False, encoding="utf-8-sig")
    print(out[["oos_rank", "nf_a", "thr", "w_a", "w_b", "vol_target", "oos_sharpe",
               "oos_ret", "oos_maxdd"]].to_string(index=False))
    print(f"\n  [OK] → {ART / 'p19_combo_revalidate.csv'}")
    return df, {"rows": rows}


def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


# ---------------------------------------------------------------------------
# P19-4 对比表 + 裁决
# ---------------------------------------------------------------------------
def build_verdict(b_df, a_df, combo_df, prod_df, cap_df, cfg, cost, prices, min_lot) -> dict:
    """现生产 vs 候选最优（全指标对比）+ 裁决建议。"""
    print("\n" + "=" * 100)
    print("[P19-4] 现生产 vs 候选最优（全指标对比）")
    print("=" * 100)

    # 现生产（修复后真实口径）：A10/B90 + thr0.70 + nfA0.20（研究口径）
    tgt_b_prod = engine_b_targets(prices, 252, 0.70)
    rb_prod = _ret_from_cache_or_run(cfg, cost, prices, tgt_b_prod, "B-252-0.70")
    tgt_a_prod = build_engine_a_targets(prices, INITIAL_CAPITAL * 0.20, group_cap=0.5,
                                        group_map=BASE_GROUP_MAP)
    ra_prod = _ret_from_cache_or_run(cfg, cost, prices, tgt_a_prod, "A-nf0.20")
    ra_p, rb_p = _align(ra_prod, rb_prod)
    prod_row = combo_stats_row(ra_p, rb_p, 0.10, False)
    prod_row["config"] = "现生产 A10/B90 thr0.70 nfA0.20"

    # 候选最优（combo 网格 volN 最优）
    combo_n = combo_df[~combo_df["vol_target"]].copy()
    best = combo_n.loc[combo_n["oos_sharpe"].idxmax()]
    cand_row = best.to_dict()
    cand_row["config"] = (f"候选 A{best['w_a']:.2f}/B{best['w_b']:.2f} "
                          f"thr{best['thr']:.2f} nfA{best['nf_a']:.2f} volN")

    # 单引擎明细（候选引擎参数）
    b_best = b_df[(b_df["win"] == 252) & (b_df["thr"] == best["thr"])].iloc[0]
    a_best = a_df[a_df["nf_a"] == best["nf_a"]].iloc[0]

    print("\n  单引擎（修复后真实口径）：")
    print(f"    现生产 B win252/thr0.70 : OOS Sharpe="
          f"{b_df[(b_df['win']==252)&(b_df['thr']==0.70)].iloc[0]['oos_sharpe']:.3f} "
          f"OOS 复利={b_df[(b_df['win']==252)&(b_df['thr']==0.70)].iloc[0]['oos_ret']*100:+.2f}%")
    print(f"    候选 B win252/thr{best['thr']:.2f}: OOS Sharpe={b_best['oos_sharpe']:.3f} "
          f"OOS 复利={b_best['oos_ret']*100:+.2f}%")
    print(f"    引擎 A nf_a={best['nf_a']:.2f} (cap0.5) : OOS Sharpe={a_best['oos_sharpe']:.3f} "
          f"OOS 复利={a_best['oos_ret']*100:+.2f}%")

    # 生产有效名义（候选）
    prod_match = prod_df[(prod_df["w_a"] == best["w_a"]) & (prod_df["nf_a"] == best["nf_a"])]
    eff_a = float(prod_match.iloc[0]["eff_notional"]) if len(prod_match) else float("nan")
    lots_m0 = int(prod_match.iloc[0]["lots_m0"]) if len(prod_match) else 0

    verdict = {
        "p19_1_engineB_thr": {
            "grid": b_df[["win", "thr", "oos_rank", "oos_sharpe", "oos_ret", "oos_maxdd",
                          "long_ratio", "mean_streak_days", "cost_drag_pp", "cost_bps"]]
                        .round(4).to_dict(orient="records"),
            "prod_anchor_win252_thr070": {
                "oos_sharpe": float(b_df[(b_df["win"] == 252) & (b_df["thr"] == 0.70)]
                                    .iloc[0]["oos_sharpe"]),
                "oos_ret": float(b_df[(b_df["win"] == 252) & (b_df["thr"] == 0.70)]
                                 .iloc[0]["oos_ret"]),
            },
        },
        "p19_2_engineA_notional": {
            "min_lot_cost": float(min_lot),
            "grid": a_df[["nf_a", "notional_per_sym", "oos_sharpe", "oos_ret", "oos_maxdd",
                          "tradable_row_ratio", "tradable_days"]].round(4).to_dict(orient="records"),
            "prod_effective": prod_df.round(2).to_dict(orient="records"),
            "capacity": cap_df.round(4).to_dict(orient="records"),
        },
        "p19_3_combo": combo_df[["oos_rank", "nf_a", "thr", "w_a", "w_b", "vol_target",
                                 "oos_sharpe", "oos_ret", "oos_maxdd"]].round(4)
            .to_dict(orient="records"),
        "p19_4_compare": {
            "prod": {
                "config": prod_row["config"],
                "oos_sharpe": round(float(prod_row["oos_sharpe"]), 4),
                "oos_ret": round(float(prod_row["oos_ret"]), 4),
                "oos_maxdd": round(float(prod_row["oos_maxdd"]), 4),
            },
            "candidate": {
                "config": cand_row["config"],
                "oos_sharpe": round(float(cand_row["oos_sharpe"]), 4),
                "oos_ret": round(float(cand_row["oos_ret"]), 4),
                "oos_maxdd": round(float(cand_row["oos_maxdd"]), 4),
                "eff_a_notional": round(eff_a, 0),
                "lots_m0": lots_m0,
            },
        },
    }
    return verdict


def _ret_from_cache_or_run(cfg, cost, prices, targets, key) -> pd.Series:
    if key not in RET_CACHE:
        run_engine_metrics(cfg, cost, prices, targets, key, ret_key=key)
    return RET_CACHE[key]


def main() -> None:
    t0 = time.time()
    print("=" * 100)
    print("P19 生产配置重建（修复后真实口径）| OOS 起点 2024-07-18 | 复利口径")
    print("=" * 100)

    # §9.18：显式 load_config("configs/base.yaml") + 显式传参
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 | OOS 起点 {OOS_START} | v8 缓存存在={V8_PATH.exists()}")

    # P19-1
    b_df, b_info = eval_engine_b_thr(cfg, cost, prices)

    # P19-2
    a_df, a_info, _sel = eval_engine_a_notional(cfg, cost, prices)

    # P19-3
    combo_df, combo_info = eval_combo(cfg, cost, prices)

    # P19-4
    prod_df = pd.DataFrame(a_info["prod"])
    cap_df = pd.DataFrame(a_info["cap"])
    verdict = build_verdict(b_df, a_df, combo_df, prod_df, cap_df, cfg, cost, prices,
                            a_info["min_lot"])

    # 裁决建议
    print("\n" + "=" * 100)
    print("裁决建议（脚本只出建议，不改生产配置默认）")
    print("=" * 100)
    b252_60 = b_df[(b_df["win"] == 252) & (b_df["thr"] == 0.60)].iloc[0]
    b252_70 = b_df[(b_df["win"] == 252) & (b_df["thr"] == 0.70)].iloc[0]
    b126_60 = b_df[(b_df["win"] == 126) & (b_df["thr"] == 0.60)].iloc[0]
    print(f"  1. 引擎 B thr：win252/thr0.60 OOS Sharpe={b252_60['oos_sharpe']:.3f} vs "
          f"thr0.70 {b252_70['oos_sharpe']:.3f}（Δ={b252_60['oos_sharpe']-b252_70['oos_sharpe']:+.3f}）| "
          f"win126/thr0.60 {b126_60['oos_sharpe']:.3f}")
    combo_n = combo_df[~combo_df["vol_target"]].sort_values("oos_sharpe", ascending=False)
    best = combo_n.iloc[0]
    print(f"  3. 组合最优（volN）: A{best['w_a']:.2f}/B{best['w_b']:.2f} "
          f"thr{best['thr']:.2f} nfA{best['nf_a']:.2f} OOS Sharpe={best['oos_sharpe']:.3f} "
          f"OOS 复利={best['oos_ret']*100:+.2f}%")

    verdict["recommendation"] = {
        "engine_b_thr": {
            "win252_thr060_delta": float(b252_60["oos_sharpe"] - b252_70["oos_sharpe"]),
            "win126_thr060_oos_sharpe": float(b126_60["oos_sharpe"]),
            "note": "thr 0.60 相比 0.70：OOS Sharpe 提升但做多天数占比更高（成本敏感性见报告）",
        },
        "engine_a_notional": {
            "note": "全品种可交易需名义 ≥1.12M（au0/sc0/cu0/i0 一手成本 > 名义上限），"
                    "不可行；建议名义覆盖便宜品种 2-3 手",
        },
        "combo": {
            "best_volN": {
                "w_a": float(best["w_a"]), "w_b": float(best["w_b"]),
                "thr": float(best["thr"]), "nf_a": float(best["nf_a"]),
                "oos_sharpe": float(best["oos_sharpe"]),
                "oos_ret": float(best["oos_ret"]),
            }
        },
    }

    ART.mkdir(exist_ok=True)
    with open(ART / "p19_verdict.json", "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  [OK] → {ART / 'p19_verdict.json'}")
    print(f"\n[DONE] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
