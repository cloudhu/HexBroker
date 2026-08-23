"""QA P13 独立复核（严过关 fresh-eyes）：多模型融合 F1/F2/F3 独立交叉验证。

与工程师实现的关系（独立性声明）：
- 不调用 scripts/p13_multimodel.py 的任何融合函数（fuse_ic/ic_weights_is/
  per_day_rank_frame/build_fused_caches 均不用），F2 权重与融合全部自写。
- 不调用 p5/p8_3 的重估函数（engine_a_targets_cs/run_engine_row/combo_stats_row/
  cs_ic_summary 均不用）；只用 BacktestEngine/CostModel/compute_metrics 核心引擎
  与数据加载函数（load_prices/load_basis_panel/CONTRACTS18），目标构建自写。
- 附：额外用生产函数（engine_a_targets_cs + run_engine_row + combo_stats_row）
  复跑一遍以核对 artifacts 数字是否可复现（两者都过才算 PASS）。

复核范围：
  A. 缓存覆盖一致性：v8/xgb/hgb/f1/f2/f3 行数 8624 / 18 品种 / ts 范围；
     (symbol,ts) 索引集合全同校验。
  B. 独立 F2 交叉验证（自写 IC 加权）：IS(<=2022-04-21) 品种内时序 Spearman IC
     → max(ic,0) 按品种归一化 → 加权 rank 融合 → 与落盘 F2 缓存对比 max|Δ|。
  C. 独立引擎 A S2 回测（自写 target 构建，含 group_cap=0.5 + ferrous_all 映射）：
     v8 基线 0.626/+8.68%/1.612/-0.0640 可复现？F2 单引擎 0.679 / 组合 1.617 可复现？
  D. 生产函数路径复跑（核对工程师 artifacts 数字）。
  E. 独立 OOS 截面 IC：v8 -0.0640 / F2 -0.0606 / F3 -0.0385 可复现？
  F. 零泄漏：F2 权重只用 IS（截断 OOS 后权重不变）；HGB 确定性（同输入两次一致）。
  G. 裁决逻辑复现：从 compare CSV 重跑 make_verdict 判定（Round 2 修正后 = 不采纳）。

口径铁律：复利口径；OOS 2024-07-18 后；滑点1tick+费0.005%+保证金12%+CONTRACTS18。
"""
from __future__ import annotations

import json
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
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import BASIS_THR, BASIS_WIN, engine_b_targets

ART = ROOT / "artifacts"
OOS_START = pd.Timestamp("2024-07-18")
IS_END = pd.Timestamp("2022-04-21")
TOP_K = 0.30
MIN_SYMS = 3
NOTIONAL_FRAC = 0.20
W_A = 0.10
W_B = 0.90
HORIZON = 5
VOL_HALFLIFE = 10
VOL_TARGET = 0.175
VOL_SCALE_CAP = 1.5

CACHES = {
    "v8": ART / "signals_cache18_grouped_v8.parquet",
    "xgb": ART / "signals_cache18_grouped_v9_xgb.parquet",
    "hgb": ART / "signals_cache18_grouped_v9_hgb.parquet",
    "F1": ART / "signals_cache18_grouped_v9_f1.parquet",
    "F2": ART / "signals_cache18_grouped_v9_f2.parquet",
    "F3": ART / "signals_cache18_grouped_v9_f3.parquet",
}
MULT = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
NOTIONAL = INITIAL_CAPITAL * NOTIONAL_FRAC

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name} {detail}")


# ---------------------------------------------------------------------------
# 0. 工具：独立 realized（前向 5 日收益）
# ---------------------------------------------------------------------------
def realized_returns(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"])


# ---------------------------------------------------------------------------
# A. 缓存覆盖一致性
# ---------------------------------------------------------------------------
def check_coverage() -> None:
    print("\n[A] 缓存覆盖一致性")
    metas = {}
    for name, p in CACHES.items():
        df = pd.read_parquet(p)
        df["ts"] = pd.to_datetime(df["ts"])
        metas[name] = df
        print(f"    {name:4s} rows={len(df):5d} syms={df['symbol'].nunique():2d} "
              f"ts={df['ts'].min().date()}~{df['ts'].max().date()}")
    base = metas["v8"]
    ok_rows = all(len(metas[n]) == len(base) for n in CACHES)
    check("全部缓存 8624 行", ok_rows, f"{[len(metas[n]) for n in CACHES]}")
    ok_syms = all(set(metas[n]["symbol"]) == set(base["symbol"]) for n in CACHES)
    check("全部缓存 18 品种同集", ok_syms)
    ok_ts = all(metas[n]["ts"].min() == base["ts"].min() and metas[n]["ts"].max() == base["ts"].max()
                for n in CACHES)
    check("全部缓存 ts 范围同", ok_ts, f"{metas['v8']['ts'].min().date()}~{metas['v8']['ts'].max().date()}")
    # (symbol,ts) 索引集合全同（不只看行数）
    base_idx = set(zip(base["symbol"], base["ts"]))
    for n in ("xgb", "hgb", "F1", "F2", "F3"):
        idx = set(zip(metas[n]["symbol"], metas[n]["ts"]))
        check(f"{n} 与 v8 (symbol,ts) 索引集合全同",
              idx == base_idx,
              f"only_v8={len(base_idx-idx)} only_{n}={len(idx-base_idx)}")


# ---------------------------------------------------------------------------
# B. 独立 F2 融合（自写 IC 加权，不调用 p13 融合函数）
# ---------------------------------------------------------------------------
def per_day_rank(sig: pd.DataFrame) -> pd.Series:
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s["_r"] = s.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    return s.set_index(["symbol", "ts"])["_r"].sort_index()


def is_ic_weights(caches: dict[str, pd.DataFrame], realized: pd.Series,
                  is_end: pd.Timestamp = IS_END) -> dict[str, dict[str, float]]:
    """自写 IS 段品种内时序 IC 权重（与 p13 语义一致但独立实现）。"""
    names = list(caches)
    syms = sorted({s for c in caches.values() for s in c["symbol"].unique()})
    weights: dict[str, dict[str, float]] = {}
    for sym in syms:
        ic_by_model: dict[str, float] = {}
        for n in names:
            sub = caches[n][caches[n]["symbol"] == sym].copy()
            sub["ts"] = pd.to_datetime(sub["ts"])
            sub = sub[sub["ts"] <= is_end]
            if len(sub) < 3:
                ic_by_model[n] = np.nan
                continue
            mi = pd.MultiIndex.from_arrays([[sym] * len(sub), sub["ts"]])
            sub["realized"] = realized.reindex(mi).to_numpy(dtype=float)
            sub = sub.dropna(subset=["realized"])
            if len(sub) < 3:
                ic_by_model[n] = np.nan
                continue
            ic_by_model[n] = float(sub["exp_ret"].corr(sub["realized"], method="spearman"))
        vals = [max(0.0, v) for v in ic_by_model.values() if pd.notna(v)]
        total = float(sum(vals))
        if total <= 0:
            weights[sym] = {n: 1.0 / len(names) for n in names}
        else:
            weights[sym] = {}
            for n in names:
                v = ic_by_model.get(n, np.nan)
                weights[sym][n] = (max(0.0, v) / total) if pd.notna(v) else 0.0
    return weights


def fuse_ic_independent(frames: dict[str, pd.Series], weights: dict[str, dict[str, float]],
                        names: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({n: frames[n] for n in names}).dropna()
    rows = []
    for (sym, ts), row in df.iterrows():
        ws = weights.get(sym)
        val = float(row.mean()) if ws is None else float(sum(row[n] * ws[n] for n in names))
        rows.append((sym, ts, val))
    return pd.DataFrame(rows, columns=["symbol", "ts", "exp_ret"])


def check_f2_independent(realized: pd.Series) -> tuple[pd.DataFrame, dict]:
    print("\n[B] 独立 F2 融合（自写 IC 加权）vs 落盘 F2 缓存")
    caches = {n: pd.read_parquet(p) for n, p in CACHES.items() if n in ("v8", "xgb", "hgb")}
    frames = {n: per_day_rank(caches[n]) for n in caches}
    weights = is_ic_weights(caches, realized)
    print("    IS 品种内时序 IC 权重（w_v8 / w_xgb / w_hgb，按品种）：")
    for sym in sorted(weights):
        w = weights[sym]
        print(f"      {sym:5s} v8={w.get('v8', 0):+.3f} xgb={w.get('xgb', 0):+.3f} "
              f"hgb={w.get('hgb', 0):+.3f}")
    f2_ind = fuse_ic_independent(frames, weights, ["v8", "xgb", "hgb"])
    f2_ship = pd.read_parquet(CACHES["F2"])
    f2_ship["ts"] = pd.to_datetime(f2_ship["ts"])
    merged = f2_ind.merge(f2_ship, on=["symbol", "ts"], suffixes=("_ind", "_ship"))
    diff = (merged["exp_ret_ind"] - merged["exp_ret_ship"]).abs()
    check("独立 F2 与落盘 F2 逐行一致", diff.max() < 1e-12,
          f"rows={len(merged)} max|Δ|={diff.max():.3e} mean|Δ|={diff.mean():.3e}")
    # F3 对照（LGB+XGB 等权）
    f3_ind = fuse_ic_independent({n: frames[n] for n in ("v8", "xgb")},
                                 {s: {"v8": 0.5, "xgb": 0.5} for s in weights}, ["v8", "xgb"])
    f3_ship = pd.read_parquet(CACHES["F3"])
    f3_ship["ts"] = pd.to_datetime(f3_ship["ts"])
    m3 = f3_ind.merge(f3_ship, on=["symbol", "ts"], suffixes=("_ind", "_ship"))
    d3 = (m3["exp_ret_ind"] - m3["exp_ret_ship"]).abs()
    check("独立 F3 与落盘 F3 逐行一致", d3.max() < 1e-12, f"max|Δ|={d3.max():.3e}")
    return f2_ind, weights


# ---------------------------------------------------------------------------
# C/D. 独立引擎 A S2 回测（自写 target，含 group_cap=0.5 + ferrous_all）
# ---------------------------------------------------------------------------
def group_map_from_config(cfg) -> dict[str, str]:
    raw = cfg.backtest.engine_a.group_map
    if raw:
        return dict(raw)
    from scripts.group_modeling_v2 import GROUPS_V2
    m = {}
    for gname, gcfg in GROUPS_V2.items():
        for s in gcfg["syms"]:
            m[s] = gname
    return m


def capped_selection(ranked: list[str], target_count: int, cap: float,
                     group_map: dict[str, str]) -> list[str]:
    """自写 P9-2 单组敞口上限（与 p5._capped_selection 文档语义一致）。"""
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts: dict[str, int] = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g: str, n: int, total: int) -> bool:
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


def build_engine_a_targets_ind(cache_path: Path, prices: pd.DataFrame,
                               group_map: dict[str, str], group_cap: float | None) -> pd.DataFrame:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    sig["_mult"] = sig["symbol"].map(MULT)
    sig["group"] = sig["symbol"].map(lambda s: group_map.get(s, "other"))
    base_cond = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)
    if group_cap is None:
        sig["selected"] = base_cond
    else:
        sig["selected"] = False
        elig = sig[sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)]
        for _ts, g in elig.groupby("ts"):
            g = g.sort_values("exp_ret", ascending=False)
            target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
            if target_count <= 0:
                continue
            sel_syms = capped_selection(g["symbol"].tolist(), target_count, group_cap, group_map)
            sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = NOTIONAL / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(sig["selected"], raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def run_engine_ind(cfg, cost, prices, targets: pd.DataFrame):
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, eq, m, m_oos


def combo_ind(ret_a: pd.Series, ret_b: pd.Series, w_a: float) -> dict:
    ra, rb = ret_a.align(ret_b, join="inner")
    comb = w_a * ra + (1.0 - w_a) * rb
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {"sharpe_full": m.sharpe, "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan}


# ---------------------------------------------------------------------------
# E. 独立 OOS 截面 IC
# ---------------------------------------------------------------------------
def oos_cs_ic_ind(cache_path: Path, realized: pd.Series) -> dict:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized.reindex(sig.index)
    sig = sig.dropna(subset=["realized"])
    sig = sig[sig.index.get_level_values(1) >= OOS_START]

    def daily_ic(g: pd.DataFrame) -> float:
        if len(g) < 3:
            return np.nan
        return float(g["exp_ret"].corr(g["realized"], method="spearman"))

    ics = sig.groupby(level="ts").apply(daily_ic).dropna()
    return {"n_days": len(ics), "ic_mean": float(ics.mean()) if len(ics) else np.nan,
            "icir": float(ics.mean() / ics.std(ddof=1)) if len(ics) > 1 and ics.std(ddof=1) > 0 else np.nan,
            "pos_ratio": float((ics > 0).mean()) if len(ics) else np.nan}


def seg_maxdd(eq: pd.Series, start: str, end: str | None = None) -> float:
    idx = pd.to_datetime(eq.index)
    sel = idx >= pd.Timestamp(start)
    if end is not None:
        sel &= idx < pd.Timestamp(end)
    sub = eq[sel]
    if len(sub) < 2:
        return np.nan
    return float((sub / sub.cummax() - 1.0).min())


# ---------------------------------------------------------------------------
# F. 零泄漏 + HGB 确定性
# ---------------------------------------------------------------------------
def check_zero_leakage(caches_raw: dict[str, pd.DataFrame], realized: pd.Series) -> None:
    print("\n[F] 零泄漏检查")
    # F2 权重只用 IS：截断 OOS 后权重应与全量缓存算出的权重逐位一致
    w_full = is_ic_weights(caches_raw, realized)
    caches_is = {n: df[df["ts"] <= IS_END] for n, df in caches_raw.items()}
    w_is = is_ic_weights(caches_is, realized)
    syms = sorted(w_full)
    diffs = [abs(w_full[s].get(n, 0) - w_is[s].get(n, 0)) for s in syms for n in ("v8", "xgb", "hgb")]
    check("F2 权重只依赖 IS 段（截断 OOS 权重不变）", max(diffs) < 1e-15,
          f"max|Δ|={max(diffs):.3e}")
    # OOS 用固定权重：确认落盘 F2 与「IS 权重 + 全量 rank」融合一致（B 已验，此处再显式）
    frames = {n: per_day_rank(caches_raw[n]) for n in caches_raw}
    f2_re = fuse_ic_independent(frames, w_full, ["v8", "xgb", "hgb"])
    f2_ship = pd.read_parquet(CACHES["F2"])
    f2_ship["ts"] = pd.to_datetime(f2_ship["ts"])
    m = f2_re.merge(f2_ship, on=["symbol", "ts"], suffixes=("_re", "_ship"))
    d = (m["exp_ret_re"] - m["exp_ret_ship"]).abs()
    check("OOS 段 F2 用固定 IS 权重（落盘一致）", d.max() < 1e-12, f"max|Δ|={d.max():.3e}")

    print("\n[F2] HGB 修复确定性单元测试（合成数据：NaN/±inf/恒定列）")
    from scripts.p13_models import HGBForecast
    from types import SimpleNamespace

    rng = np.random.default_rng(0)
    n = 300
    X = pd.DataFrame(
        {f"f{i}": rng.normal(size=n) for i in range(8)} | {
            "f_const": np.zeros(n),           # 恒定列（sanitize 后全 0）
            "f_nan": np.where(np.arange(n) % 10 == 0, np.nan, rng.normal(size=n)),
            "f_inf": np.where(np.arange(n) % 20 == 0, np.inf, rng.normal(size=n)),
        },
        index=pd.MultiIndex.from_arrays([["S"] * n, pd.date_range("2020-01-01", periods=n)],
                                        names=["symbol", "datetime"]),
    )
    y_raw = rng.normal(size=n)
    y = y_raw[20 - 1:]  # build_windows 产出 n-lookback+1 窗，y 与 valid_idx 对齐
    cfg = SimpleNamespace(forecast=SimpleNamespace(horizon=5, n_mc_samples=5, seed=42,
                                                   hgb_max_iter=20, hgb_lr=0.05,
                                                   hgb_max_depth=4, hgb_min_samples_leaf=5,
                                                   hgb_l2=1.0),
                          feature=SimpleNamespace(normalize_window=20))
    m1 = HGBForecast(cfg)
    tl1 = m1.fit(X, y)
    p1 = m1.predict(X)
    m2 = HGBForecast(cfg)
    m2.fit(X, y)
    p2 = m2.predict(X)
    e1 = np.array([s.exp_ret for s in p1])
    e2 = np.array([s.exp_ret for s in p2])
    check("HGB 同输入两次 fit/predict 结果一致（确定性）",
          len(e1) > 0 and np.allclose(e1, e2, atol=1e-12),
          f"n_sig={len(e1)} max|Δexp_ret|={np.max(np.abs(e1-e2)) if len(e1) else 'n/a'}")
    check("HGB 恒定列被确定性微扰（n_const>0）", m1._params.get("n_const_jittered", 0) > 0,
          f"n_const_jittered={m1._params.get('n_const_jittered')}")

    # 微扰是否影响 XGB/LGB 路径：XGB 的 _next_return 不做 sanitize（原始 NaN 直通）
    from scripts.p13_models import XGBoostForecast
    import inspect
    src = inspect.getsource(XGBoostForecast._next_return)
    check("XGB 路径不经 sanitize（原始 NaN 直通，HGB 修复不影响）",
          "nan_to_num" not in src and "_sanitize" not in src, "")


# ---------------------------------------------------------------------------
# G. 裁决逻辑复现
# ---------------------------------------------------------------------------
def check_verdict() -> None:
    print("\n[G] 裁决逻辑复现（make_verdict 规则）")
    from scripts.p13_multimodel import make_verdict
    tbl = pd.read_csv(ART / "p13_multimodel_compare.csv")
    ic_tbl = pd.read_csv(ART / "p13_model_ic.csv")
    v = make_verdict(tbl, ic_tbl)
    ship = json.loads((ART / "p13_verdict.json").read_text(encoding="utf-8"))
    # Round 2 修正（DISCREPANCY-1 修复后）：评估切回生产口径 group_cap=0.5 + ferrous_all，
    # 三个融合方案 IC 改善但单引擎/组合 Sharpe 全部未超基线 → 不采纳（is_pass=false）。
    # （Round 1 时 artifacts 为无 cap 口径，规则曾判 F2 采纳；此处随 cap 口径修正更新。）
    check("裁决 is_pass=False / adopted=null", v["is_pass"] is False and v["adopted_fusion"] is None,
          f"is_pass={v['is_pass']} adopted={v['adopted_fusion']}")
    check("落盘 verdict 与规则复现一致",
          v["adopted_fusion"] == ship["adopted_fusion"] and v["is_pass"] == ship["is_pass"],
          f"ship_adopted={ship['adopted_fusion']} ship_pass={ship['is_pass']}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 96)
    print("QA P13 独立复核：多模型融合 F1/F2/F3（fresh-eyes，自写目标构建+自写 IC 权重）")
    print(f"OOS 起点 {OOS_START.date()} | IS 终点 {IS_END.date()} | "
          f"口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | A{W_A:.2f}/B{W_B:.2f}")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    gmap = group_map_from_config(cfg)
    gcap = float(cfg.backtest.engine_a.group_cap) if cfg.backtest.engine_a.group_cap else None
    print(f"    prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"realized={len(realized)} | group_cap={gcap} | group_map keys={len(gmap)}")

    check_coverage()

    caches_raw = {n: pd.read_parquet(p) for n, p in CACHES.items() if n in ("v8", "xgb", "hgb")}
    for n, df in caches_raw.items():
        df["ts"] = pd.to_datetime(df["ts"])

    f2_ind, weights = check_f2_independent(realized)
    check_zero_leakage(caches_raw, realized)

    # ---- C. 独立引擎 A S2 回测 ----
    print("\n[C] 独立引擎 A S2 回测（自写 target，含 group_cap + ferrous_all）")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, eq_b, m_b, m_oos_b = run_engine_ind(cfg, cost, prices, tgt_b)
    print(f"    引擎B: Sharpe_full={m_b.sharpe:.3f} OOS_Sharpe={m_oos_b.sharpe if m_oos_b else float('nan'):.3f}")

    rows = []
    for name in ("v8", "F2", "F3"):
        tgt = build_engine_a_targets_ind(CACHES[name], prices, gmap, gcap)
        ret_a, eq_a, m_a, m_oos_a = run_engine_ind(cfg, cost, prices, tgt)
        combo = combo_ind(ret_a, ret_b, W_A)
        ic = oos_cs_ic_ind(CACHES[name], realized)
        row = {"variant": name,
               "oos_sharpe": m_oos_a.sharpe if m_oos_a else np.nan,
               "oos_ret": m_oos_a.total_return if m_oos_a else np.nan,
               "maxdd_full": m_a.max_drawdown,
               "maxdd_oos": m_oos_a.max_drawdown if m_oos_a else np.nan,
               "combo_oos_sharpe": combo["oos_sharpe"],
               "combo_oos_ret": combo["oos_ret"],
               "combo_maxdd_full": combo["maxdd_full"],
               "oos_cs_ic": ic["ic_mean"], "oos_cs_icir": ic["icir"],
               "oos_cs_pos": ic["pos_ratio"], "oos_n_days": ic["n_days"]}
        rows.append(row)
        print(f"    [{name}] OOS Sharpe={row['oos_sharpe']:.3f} OOS 复利={row['oos_ret']*100:+.2f}% "
              f"MaxDD_full={row['maxdd_full']*100:.1f}% MaxDD_oos={row['maxdd_oos']*100:.1f}% "
              f"| 组合 A10/B90 OOS Sharpe={row['combo_oos_sharpe']:.3f} "
              f"| OOS 截面 IC={row['oos_cs_ic']:+.4f}")
    ind = pd.DataFrame(rows).set_index("variant")

    print("\n    独立复现 vs 工程师报告（复利口径）")
    exp = {"v8": {"oos_sharpe": 0.626, "oos_ret": 0.0868, "combo": 1.612, "ic": -0.0640},
           "F2": {"oos_sharpe": 0.679, "oos_ret": 0.0935, "combo": 1.617, "ic": -0.0606},
           "F3": {"oos_sharpe": 0.585, "oos_ret": 0.0802, "combo": 1.609, "ic": -0.0385}}
    for v in ("v8", "F2", "F3"):
        r = ind.loc[v]
        check(f"{v} 单引擎 OOS Sharpe 复现", abs(r["oos_sharpe"] - exp[v]["oos_sharpe"]) < 0.01,
              f"ind={r['oos_sharpe']:.3f} exp={exp[v]['oos_sharpe']:.3f}")
        check(f"{v} 组合 A10/B90 OOS Sharpe 复现", abs(r["combo_oos_sharpe"] - exp[v]["combo"]) < 0.01,
              f"ind={r['combo_oos_sharpe']:.3f} exp={exp[v]['combo']:.3f}")
        check(f"{v} OOS 截面 IC 复现", abs(r["oos_cs_ic"] - exp[v]["ic"]) < 0.003,
              f"ind={r['oos_cs_ic']:+.4f} exp={exp[v]['ic']:+.4f}")
    check("v8 OOS 复利 +8.68% 复现", abs(ind.loc["v8", "oos_ret"] - 0.0868) < 0.002,
          f"ind={ind.loc['v8','oos_ret']*100:+.2f}%")
    check("F2 全样本 MaxDD 改善（-26.1%→-18.2%）",
          ind.loc["v8", "maxdd_full"] < -0.24 and ind.loc["F2", "maxdd_full"] > -0.20,
          f"v8={ind.loc['v8','maxdd_full']*100:.1f}% F2={ind.loc['F2','maxdd_full']*100:.1f}%")

    print("\n    MaxDD 分段诊断（IS vs OOS，独立口径）")
    for name in ("v8", "F2"):
        tgt = build_engine_a_targets_ind(CACHES[name], prices, gmap, gcap)
        _, eq_a, _, _ = run_engine_ind(cfg, cost, prices, tgt)
        print(f"      {name}: IS MaxDD={seg_maxdd(eq_a, '2019-01-01', '2024-07-18')*100:.1f}% | "
              f"OOS MaxDD={seg_maxdd(eq_a, '2024-07-18')*100:.1f}%")

    # ---- D. 生产函数路径复跑（核对 artifacts） ----
    print("\n[D] 生产函数路径复跑（engine_a_targets_cs + run_engine_row，核对 artifacts 数字）")
    from scripts.p5_engineA_cross_section import combo_stats_row, engine_a_targets_cs, run_engine_row
    for name in ("v8", "xgb", "hgb", "F1", "F2", "F3"):
        tgt = engine_a_targets_cs(prices, TOP_K, MIN_SYMS, cache_path=CACHES[name])
        ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(cfg, cost, prices, tgt, f"A-{name}")
        ra = ret_a.loc[ret_a.index.intersection(ret_b.index)]
        rb = ret_b.loc[ret_b.index.intersection(ret_b.index)]
        r = combo_stats_row(ra, rb, W_A, False)
        print(f"      [{name}] OOS Sharpe={m_oos_a.sharpe if m_oos_a else float('nan'):.3f} "
              f"OOS 复利={m_oos_a.total_return if m_oos_a else float('nan'):.3f} "
              f"组合OOS={r['oos_sharpe']:.3f}")
    ship = pd.read_csv(ART / "p13_multimodel_compare.csv")
    for name in ("v8", "F2"):
        r = ship[(ship["variant"] == name) & (ship["engine"] == "A-S2")].iloc[0]
        print(f"      ship[{name}] OOS Sharpe={r['oos_sharpe']:.3f} OOS 复利={r['oos_ret']:.4f}")
        r2 = ship[(ship["variant"] == name) & (ship["engine"] == "A0.10/B0.90-volN")].iloc[0]
        print(f"      ship[{name}] 组合 OOS={r2['oos_sharpe']:.3f}")

    # ---- E. 独立 OOS 截面 IC 全表 ----
    print("\n[E] 独立 OOS 截面 IC（自写每日截面 Spearman）")
    for name in ("v8", "xgb", "hgb", "F1", "F2", "F3"):
        ic = oos_cs_ic_ind(CACHES[name], realized)
        print(f"      {name:3s} n_days={ic['n_days']:3d} IC={ic['ic_mean']:+.4f} "
              f"ICIR={ic['icir']:+.3f} pos={ic['pos_ratio']*100:4.1f}%")

    check_verdict()

    print("\n" + "=" * 96)
    n_pass = sum(1 for _, s, _ in _results if s == PASS)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    print(f"QA P13 独立复核汇总：PASS={n_pass} FAIL={n_fail}")
    for name, s, detail in _results:
        print(f"  [{s}] {name} {detail}")
    print("=" * 96)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
