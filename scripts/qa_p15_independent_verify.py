"""P15 QA 独立复核（fresh-eyes）：稳定校准复验 + 整数手 sizing。

QA 独立复核脚本（自写实现，不调用 scripts/p15_contract_layer.py 的任何函数，
仅复用共享基础设施：hexbroker 包 / p3/p5/p8 的常量与辅助——与 P15 声明口径一致）。

复核范围
--------
A. v11_cal 缓存完整性：行数 8624、18 品种、与 v8 内连接、exp_ret 逐字节一致（100%）。
B. 稳定校准独立重实现（增长池因果 Platt）：
   ① p_up↔exp_ret rank corr（目标 0.3898）② p_up OOS 截面 IC（目标 +0.0412）
   ③ |IS-OOS|（目标 0.0925 / v11_is_symbol 0.0015）④ v8_raw / v10_cal 对照
C. sizing 矩阵独立重实现（v8 缓存 → int/frac/tradable/notional035 targets →
   BacktestEngine → 单引擎 OOS Sharpe，目标 0.646/0.488/0.326/0.544）
D. floor 质量过滤器 claim：frac 理想口径下 au0/cu0/i0/j0/sc0 贡献为负（零化消融）
E. 组合 A10/B90 volN/volY 基线（目标 1.614 / 1.664）

用法
----
  python scripts/qa_p15_independent_verify.py
"""
from __future__ import annotations

import json
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
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from hexbroker.forecast.calibration import PlattScaler
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import BASIS_THR, BASIS_WIN, OOS_START, TOP_K, engine_b_targets

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V10_CAL_PATH = ART / "signals_cache18_grouped_v10_cal.parquet"
V11_RAW_PATH = ART / "signals_cache18_grouped_v11_raw.parquet"
V11_CAL_PATH = ART / "signals_cache18_grouped_v11_cal.parquet"

IS_END = "2022-04-21"
OOS = OOS_START
HORIZON = 5
MIN_POOL = 50
CAL_SPLIT = 0.5
MIN_SYMBOLS_S2 = 3
COMBO_W_A = 0.10
VOL_TARGET = 0.175
VOL_HALFLIFE = 10
VOL_SCALE_CAP = 1.5
HIGH_PRICE = ["au0", "cu0", "i0", "j0", "sc0"]

# 生产 cap 口径（configs/base.yaml 显式 + 显式传参）
CFG_PATH = ROOT / "configs" / "base.yaml"


# ===========================================================================
# A. 缓存完整性
# ===========================================================================
def check_cache_integrity() -> dict:
    v8 = pd.read_parquet(V8_PATH)
    v11 = pd.read_parquet(V11_CAL_PATH)
    v11["ts"] = pd.to_datetime(v11["ts"])
    v8["ts"] = pd.to_datetime(v8["ts"])
    m = v8.merge(v11[["symbol", "ts", "exp_ret"]], on=["symbol", "ts"],
                 suffixes=("_v8", "_v11"))
    exact = float((m["exp_ret_v8"] == m["exp_ret_v11"]).mean())
    isclose = float(np.isclose(m["exp_ret_v8"], m["exp_ret_v11"],
                               rtol=1e-9, atol=1e-12).mean())
    return {
        "v8_rows": int(len(v8)), "v11_rows": int(len(v11)),
        "v11_symbols": int(v11["symbol"].nunique()),
        "inner_join": int(len(m)),
        "exp_ret_exact": exact,
        "exp_ret_isclose": isclose,
    }


# ===========================================================================
# B. 稳定校准独立重实现（增长池因果 Platt）
# ===========================================================================
def qa_realized(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    """已实现收益：close.shift(-horizon)/close - 1 → MultiIndex(symbol, datetime)。"""
    parts = []
    syms = prices.index.get_level_values(0).unique()
    for sym in syms:
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=syms).rename_axis(["symbol", "datetime"])


def qa_growing_pool(raw: pd.DataFrame, realized: pd.Series,
                    min_pool: int = MIN_POOL, cal_split: float = CAL_SPLIT) -> pd.DataFrame:
    """独立重实现：逐品种、逐折（时间序）因果增长池 Platt 校准。

    折 i 校准池 = 折 0..i 校准子窗（前 cal_split 有效信号）；池 >= min_pool 时
    拟合单一 Platt（hexbroker PlattScaler），应用于本折评估子窗；否则 identity。
    返回 eval_df（含 p_up_cal / cal_pool_n / cal_slope）。
    """
    raw = raw.copy()
    raw["ts"] = pd.to_datetime(raw["ts"])
    raw["realized"] = realized.reindex(
        pd.MultiIndex.from_arrays([raw["symbol"], raw["ts"]])).to_numpy()
    raw["y"] = (raw["realized"] > 0).astype(float)
    raw["_valid"] = raw["realized"].notna()

    eval_parts = []
    for sym, sym_g in raw.groupby("symbol"):
        pool_p, pool_y = [], []
        for fi, fold_recs in sym_g.groupby("fi"):
            g = fold_recs.sort_values("ts").reset_index(drop=True)
            k = int(len(g) * cal_split)
            k = max(1, min(k, len(g) - 1))
            cal_recs, eval_recs = g.iloc[:k], g.iloc[k:]
            cal_valid = cal_recs[cal_recs["_valid"]]
            if len(cal_valid):
                pool_p.append(cal_valid["p_up"].to_numpy(dtype=float))
                pool_y.append(cal_valid["y"].to_numpy(dtype=float))
            pp = np.concatenate(pool_p) if pool_p else np.array([])
            py = np.concatenate(pool_y) if pool_y else np.array([])
            if len(pp) >= min_pool:
                scaler = PlattScaler()
                scaler.fit(pp, py)
                slope, intercept = float(scaler.a), float(scaler.b)
                pool_n = int(len(pp))
                p_cal = np.clip(scaler.transform(
                    eval_recs["p_up"].to_numpy(dtype=float)), 1e-6, 1 - 1e-6)
            else:
                slope, intercept, pool_n = np.nan, np.nan, int(len(pp))
                p_cal = eval_recs["p_up"].to_numpy(dtype=float)
            out = eval_recs[["symbol", "ts", "p_up", "exp_ret"]].copy()
            out["p_up_cal"] = p_cal
            out["cal_pool_n"] = pool_n
            out["cal_slope"] = slope
            out["cal_intercept"] = intercept
            eval_parts.append(out)
    return pd.concat(eval_parts, ignore_index=True).sort_values(["symbol", "ts"])


def qa_is_symbol_calibration(sig: pd.DataFrame, realized: pd.Series) -> pd.DataFrame:
    """独立重实现 v11_is_symbol：只用 IS 期（ts < OOS）每品种拟合 Platt → 应用到全部。"""
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s["realized"] = realized.reindex(
        pd.MultiIndex.from_arrays([s["symbol"], s["ts"]])).to_numpy()
    is_data = s[(s["ts"] < pd.Timestamp(OOS)) & s["realized"].notna()]
    out = []
    for sym, g in s.groupby("symbol"):
        g = g.copy()
        gis = is_data[is_data["symbol"] == sym]
        p_fit = gis["p_up"].to_numpy(dtype=float)
        y_fit = (gis["realized"] > 0).to_numpy(dtype=float)
        p_apply = g["p_up"].to_numpy(dtype=float)
        if len(p_fit) >= MIN_POOL:
            sc = PlattScaler()
            sc.fit(p_fit, y_fit)
            p_apply = np.clip(sc.transform(p_apply), 1e-6, 1 - 1e-6)
        g["p_up"] = p_apply
        out.append(g)
    return pd.concat(out)


def qa_cs_ic(sig: pd.DataFrame, realized: pd.Series, oos_start: str,
             score_col: str = "exp_ret") -> dict:
    """每日截面 Spearman IC（自实现，>=3 品种/日）。"""
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s = s.set_index(["symbol", "ts"]).sort_index()
    s["realized"] = realized.reindex(s.index)
    s = s.dropna(subset=["realized"])

    def daily(g):
        from scipy.stats import spearmanr
        if len(g) < 3:
            return np.nan
        return float(spearmanr(g[score_col], g["realized"]).statistic)

    ics = s.groupby(level="ts").apply(daily).dropna()
    ics.name = "ic"

    def summ(sub):
        if len(sub) == 0:
            return {"n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
                    "icir": np.nan, "pos_ratio": np.nan}
        sd = float(sub.std(ddof=1)) if len(sub) > 1 else 0.0
        return {
            "n_days": int(len(sub)),
            "ic_mean": float(sub.mean()),
            "ic_std": sd,
            "icir": float(sub.mean() / sd) if len(sub) > 1 and sd > 0 else np.nan,
            "pos_ratio": float((sub > 0).mean()),
        }

    return {
        "full": summ(ics),
        "is": summ(ics[ics.index < pd.Timestamp(IS_END)]),
        "oos": summ(ics[ics.index >= pd.Timestamp(oos_start)]),
    }


def qa_rank_corr_pup_exp(sig: pd.DataFrame) -> float:
    from scipy.stats import spearmanr
    return float(spearmanr(sig["p_up"], sig["exp_ret"]).statistic)


def verify_calibration_metrics() -> dict:
    """独立复现 p15_cal_verify.csv 四行 + v11_is_symbol。"""
    prices = load_prices()
    realized = qa_realized(prices)
    raw = pd.read_parquet(V11_RAW_PATH)
    v8 = pd.read_parquet(V8_PATH)
    v10 = pd.read_parquet(V10_CAL_PATH)
    v11_cal = pd.read_parquet(V11_CAL_PATH)

    # 1) 独立增长池重实现 → 与 v11_cal 存储 p_up 逐字节对比
    t0 = time.time()
    qa_eval = qa_growing_pool(raw, realized)
    merged = qa_eval.merge(
        v11_cal[["symbol", "ts", "p_up"]].rename(columns={"p_up": "p_up_v11"}),
        on=["symbol", "ts"], how="inner")
    p_match = float((merged["p_up_cal"] == merged["p_up_v11"]).mean())
    p_isclose = float(np.isclose(merged["p_up_cal"], merged["p_up_v11"],
                                 rtol=1e-9, atol=1e-12).mean())
    print(f"[B] 独立增长池重实现: {len(qa_eval)} 行 | vs v11_cal p_up 逐字节一致率 "
          f"{p_match*100:.4f}% (isclose {p_isclose*100:.4f}%) 耗时 {time.time()-t0:.0f}s")

    # 2) 各变体指标（v8_raw / v10_cal / v11_grow）
    rows = {}
    v11g = qa_eval.drop(columns=["p_up"]).rename(columns={"p_up_cal": "p_up"})
    variants = {
        "v8_raw": (v8, "p_up"),
        "v10_cal": (v10, "p_up"),
        "v11_grow": (v11g, "p_up"),
    }
    for name, (sig, score) in variants.items():
        rc = qa_rank_corr_pup_exp(sig)
        summ = qa_cs_ic(sig, realized, OOS, score)
        rows[name] = {
            "rank_corr_pup_exp": rc,
            "ic_is": summ["is"]["ic_mean"],
            "ic_oos": summ["oos"]["ic_mean"],
            "abs_is_oos_diff": abs(summ["is"]["ic_mean"] - summ["oos"]["ic_mean"]),
            "ic_full": summ["full"]["ic_mean"],
            "oos_icir": summ["oos"]["icir"],
            "oos_pos_ratio": summ["oos"]["pos_ratio"],
            "oos_n_days": summ["oos"]["n_days"],
        }
    # 3) v11_is_symbol（IS-only 分品种）
    v11_is_sym = qa_is_symbol_calibration(v11_cal, realized)
    rc_sym = qa_rank_corr_pup_exp(v11_is_sym)
    summ_sym = qa_cs_ic(v11_is_sym, realized, OOS, "p_up")
    rows["v11_is_symbol"] = {
        "rank_corr_pup_exp": rc_sym,
        "ic_is": summ_sym["is"]["ic_mean"],
        "ic_oos": summ_sym["oos"]["ic_mean"],
        "abs_is_oos_diff": abs(summ_sym["is"]["ic_mean"] - summ_sym["oos"]["ic_mean"]),
        "ic_full": summ_sym["full"]["ic_mean"],
        "oos_icir": summ_sym["oos"]["icir"],
        "oos_pos_ratio": summ_sym["oos"]["pos_ratio"],
        "oos_n_days": summ_sym["oos"]["n_days"],
    }
    return rows


# ===========================================================================
# C. sizing 矩阵独立重实现（引擎 A targets + BacktestEngine）
# ===========================================================================
def qa_group_map_from_config() -> dict:
    cfg = load_config(str(CFG_PATH))
    return dict(cfg.backtest.engine_a.group_map)


def qa_capped_selection(ranked, target_count, cap, group_map):
    """独立重实现 p5._capped_selection 语义（剔除 + 替补，组敞口上限）。"""
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g, n, total):
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


def qa_engine_a_targets_sized(prices, cache_path, score_col="exp_ret", sizing="int",
                              group_cap=0.5, group_map=None, min_symbols=MIN_SYMBOLS_S2,
                              notional_frac=0.20):
    """独立重实现 P15 sizing 目标生成（S2 min=3 + cap 0.5 + 显式 group_map）。"""
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    px_lookup = prices["close"]
    sig["_px"] = [px_lookup.get((s, t)) for s, t in zip(sig["symbol"], sig["ts"])]
    sig["_mult"] = sig["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18})
    sig["group"] = sig["symbol"].map(lambda s: group_map.get(s, "other"))

    notional = INITIAL_CAPITAL * notional_frac
    with np.errstate(invalid="ignore", divide="ignore"):
        sig["raw_lots"] = (notional / (sig["_px"] * sig["_mult"])).fillna(0.0)

    if sizing == "tradable":
        work = sig[sig["raw_lots"] >= 1.0].copy()
    else:
        work = sig.copy()

    # 每日截面 rank + 组敞口 cap 选择（自实现）
    work["rank_pct"] = work.groupby("ts")[score_col].rank(pct=True, ascending=True)
    work["_day_cnt"] = work.groupby("ts")["symbol"].transform("count")
    work["selected"] = False
    elig_mask = work["_px"].notna()
    if min_symbols is not None:
        elig_mask = elig_mask & (work["_day_cnt"] >= min_symbols)
    elig = work[elig_mask]
    for _ts, g in elig.groupby("ts"):
        g = g.sort_values(score_col, ascending=False)
        target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
        if target_count <= 0:
            continue
        sel_syms = qa_capped_selection(g["symbol"].tolist(), target_count,
                                       float(group_cap), group_map)
        work.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True

    if sizing == "int":
        work["target"] = np.where(work["selected"], work["raw_lots"].astype(int), 0)
    elif sizing == "frac":
        work["target"] = np.where(work["selected"], work["raw_lots"], 0.0)
    elif sizing in ("tradable", "notional035"):
        work["target"] = np.where(work["selected"], work["raw_lots"].astype(int), 0)
    else:
        raise ValueError(sizing)

    if sizing == "tradable":
        # 不可交易行（raw_lots < 1）也要出现在 targets 中且 target=0
        full = sig.copy()
        full["selected"] = False
        full.loc[work.index[work["selected"]], "selected"] = True
        full["target"] = np.where(full["selected"], full["raw_lots"].astype(int), 0)
        work = full
    return work.set_index(["symbol", "ts"])[["target"]].sort_index()


def qa_run_engine(cfg, prices, targets):
    """独立回测封装：BacktestEngine + CostModel → 全样本/OOS 指标 + 日收益。"""
    cost = CostModel.from_config(cfg)
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    idx = pd.to_datetime(eq.index)
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    oos_eq = eq[idx >= pd.Timestamp(OOS)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, eq, m, m_oos


def qa_combo_stats(ret_a, ret_b, w_a, vol_target):
    comb = w_a * ret_a + (1.0 - w_a) * ret_b
    if vol_target:
        vol = comb.ewm(halflife=VOL_HALFLIFE, adjust=False).std().shift(1)
        scale = (VOL_TARGET / (vol * np.sqrt(252))).clip(lower=0.0, upper=VOL_SCALE_CAP)
        scale = scale.fillna(1.0)
        comb = comb * scale
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "sharpe_full": m.sharpe,
    }


# ===========================================================================
# D. floor 质量过滤器验证（frac 理想口径高价合约贡献）
# ===========================================================================
def zero_symbols(targets: pd.DataFrame, syms: list[str]) -> pd.DataFrame:
    t = targets.copy()
    idx = t.index
    sym_level = idx.get_level_values(0)
    t.loc[sym_level.isin(syms), "target"] = 0.0
    return t


def main() -> None:
    t_all = time.time()
    print("=" * 100)
    print("P15 QA 独立复核（fresh-eyes）：稳定校准复验 + 整数手 sizing")
    print("口径: 复利 | OOS 2024-07-18 后 | 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"生产 cap（显式 {CFG_PATH.name} + 显式传参）")
    print("=" * 100, flush=True)

    cfg = load_config(str(CFG_PATH))
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    prices = load_prices()
    realized = qa_realized(prices)
    group_map = qa_group_map_from_config()
    group_cap = float(cfg.backtest.engine_a.group_cap)
    print(f"生产口径: group_cap={group_cap} group_map 键数={len(group_map)} "
          f"ferrous_all={set(group_map).issuperset({'i0','j0','jm0','rb0','hc0'})} | "
          f"prices {len(prices)} 行")

    report = {"env": {
        "group_cap": group_cap, "group_map_keys": len(group_map),
        "ferrous_all_ok": set(group_map).issuperset({'i0', 'j0', 'jm0', 'rb0', 'hc0'}),
        "oos_start": OOS, "is_end": IS_END,
    }}

    # ---- A. 缓存完整性 ----
    print("-" * 100)
    print("[A] 缓存完整性")
    report["cache"] = check_cache_integrity()
    print(f"  v8 {report['cache']['v8_rows']} 行 | v11_cal {report['cache']['v11_rows']} 行 | "
          f"{report['cache']['v11_symbols']} 品种 | 内连接 {report['cache']['inner_join']} | "
          f"exp_ret 逐字节一致 {report['cache']['exp_ret_exact']*100:.2f}%")

    # ---- B. 稳定校准指标 ----
    print("-" * 100)
    print("[B] 稳定校准独立重实现（增长池 Platt）")
    cal = verify_calibration_metrics()
    report["calibration"] = cal
    targets_cal = {
        "v8_raw": {"rank_corr_pup_exp": 0.9612956649502851, "ic_is": 0.022446303360147853,
                   "ic_oos": -0.04789662955962365, "abs_is_oos_diff": 0.0703429329197715,
                   "ic_full": 0.009598296658149564, "oos_icir": -0.1142931566903285,
                   "oos_pos_ratio": 0.475, "oos_n_days": 160.0},
        "v10_cal": {"rank_corr_pup_exp": 0.08842774538517363, "ic_is": -0.03507180996228674,
                    "ic_oos": 0.1394788404541081, "abs_is_oos_diff": 0.17455065041639484,
                    "ic_full": 0.03488194276008499, "oos_icir": 0.36309632677330744,
                    "oos_pos_ratio": 0.64375, "oos_n_days": 160.0},
        "v11_grow": {"rank_corr_pup_exp": 0.3898124869467138, "ic_is": -0.051247324178526746,
                     "ic_oos": 0.04124623028387596, "abs_is_oos_diff": 0.0924935544624027,
                     "ic_full": -0.0025638112772474286, "oos_icir": 0.11234507100243436,
                     "oos_pos_ratio": 0.50625, "oos_n_days": 160.0},
        "v11_is_symbol": {"rank_corr_pup_exp": -0.09918970045327216,
                          "ic_is": 0.02695650088142014, "ic_oos": 0.02846743036892463,
                          "abs_is_oos_diff": 0.0015109294875044893,
                          "ic_full": 0.06427311924090294, "oos_icir": 0.08022506545003516,
                          "oos_pos_ratio": 0.51875, "oos_n_days": 160.0},
    }
    print(f"  {'calibration':<14}{'rank_corr':>10}{'IS_IC':>10}{'OOS_IC':>10}"
          f"{'|IS-OOS|':>10}{'OOS_ICIR':>10}{'pos%':>7}")
    for k in ["v8_raw", "v10_cal", "v11_grow", "v11_is_symbol"]:
        c = cal[k]
        print(f"  {k:<14}{c['rank_corr_pup_exp']:>10.4f}{c['ic_is']:>10.4f}"
              f"{c['ic_oos']:>10.4f}{c['abs_is_oos_diff']:>10.4f}{c['oos_icir']:>10.4f}"
              f"{c['oos_pos_ratio']*100:>6.1f}%")
    # 对比表
    print("  --- 与 p15_cal_verify.csv 对比（差） ---")
    diffs = {}
    for k, t in targets_cal.items():
        c = cal[k]
        d = {kk: (float(c[kk]) - float(v)) for kk, v in t.items()}
        diffs[k] = d
        print(f"  {k:<14} max|diff|={max(abs(v) for v in d.values()):.6e}  "
              f"rank_corr diff={d['rank_corr_pup_exp']:+.6e}  "
              f"OOS_IC diff={d['ic_oos']:+.6e}")
    report["calibration_diffs"] = diffs

    # ---- C. sizing 矩阵（v8/exp_ret 4 种 sizing） ----
    print("-" * 100)
    print("[C] sizing 矩阵独立重实现（v8 缓存, exp_ret, S2 min=3 + cap 0.5）")
    sizing_rows = {}
    for sizing in ["int", "frac", "tradable", "notional035"]:
        nf = {"int": 0.20, "frac": 0.20, "tradable": 0.20, "notional035": 0.35}[sizing]
        t0 = time.time()
        tgt = qa_engine_a_targets_sized(
            prices, V8_PATH, score_col="exp_ret", sizing=sizing,
            group_cap=group_cap, group_map=group_map, notional_frac=nf)
        _r, _eq, _m, m_oos = qa_run_engine(cfg, prices, tgt)
        oos_sh = m_oos.sharpe if m_oos else np.nan
        oos_ret = m_oos.total_return if m_oos else np.nan
        sizing_rows[sizing] = {"oos_sharpe": oos_sh, "oos_ret": oos_ret,
                               "full_sharpe": _m.sharpe}
        print(f"  [{sizing:<10}] OOS Sharpe={oos_sh:.4f} OOS 复利={oos_ret*100:+.2f}% "
              f"(耗时 {time.time()-t0:.0f}s)")
    report["sizing"] = sizing_rows
    targets_sizing = {"int": 0.6459911924883134, "frac": 0.48825328490641834,
                      "tradable": 0.3256963889467175, "notional035": 0.5436843685220752}
    for k, v in targets_sizing.items():
        print(f"  {k}: QA={sizing_rows[k]['oos_sharpe']:.4f} vs P15={v:.4f} "
              f"diff={sizing_rows[k]['oos_sharpe']-v:+.6f}")

    # ---- D. floor 质量过滤器验证 ----
    print("-" * 100)
    print("[D] floor 质量过滤器 claim：frac 理想口径下高价合约贡献")
    frac_tgt = qa_engine_a_targets_sized(
        prices, V8_PATH, score_col="exp_ret", sizing="frac",
        group_cap=group_cap, group_map=group_map, notional_frac=0.20)
    _r, _eq, _m, m_oos_full = qa_run_engine(cfg, prices, frac_tgt)
    full_ret = m_oos_full.total_return
    print(f"  frac 全组合 OOS 复利 = {full_ret*100:+.2f}%")
    ablate = {"full": full_ret}
    for sym in HIGH_PRICE:
        tgt0 = zero_symbols(frac_tgt, [sym])
        _r, _eq, _m, m_oos = qa_run_engine(cfg, prices, tgt0)
        ablate[f"zero_{sym}"] = m_oos.total_return
        print(f"  zero {sym:<4}: OOS 复利 {m_oos.total_return*100:+.2f}% "
              f"(Δ vs full {100*(m_oos.total_return-full_ret):+.2f}pp)")
    tgt0_all = zero_symbols(frac_tgt, HIGH_PRICE)
    _r, _eq, _m, m_oos = qa_run_engine(cfg, prices, tgt0_all)
    ablate["zero_all5"] = m_oos.total_return
    print(f"  zero all5(au0/cu0/i0/j0/sc0): OOS 复利 {m_oos.total_return*100:+.2f}% "
          f"(Δ vs full {100*(m_oos.total_return-full_ret):+.2f}pp)")
    report["floor_ablation"] = ablate

    # ---- E. 组合基线（v8/int/exp_ret A10/B90） ----
    print("-" * 100)
    print("[E] 组合 A10/B90 基线（v8/int/exp_ret）")
    tgt_a = qa_engine_a_targets_sized(
        prices, V8_PATH, score_col="exp_ret", sizing="int",
        group_cap=group_cap, group_map=group_map, notional_frac=0.20)
    ret_a, _eq_a, _m_a, m_oos_a = qa_run_engine(cfg, prices, tgt_a)
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, _eq_b, _m_b, _m_oos_b = qa_run_engine(cfg, prices, tgt_b)
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[common].sort_index(), ret_b.loc[common].sort_index()
    combo = {}
    for vt in (False, True):
        c = qa_combo_stats(ra, rb, COMBO_W_A, vt)
        combo["volY" if vt else "volN"] = c
        print(f"  A10/B90 {'volY' if vt else 'volN'}: OOS Sharpe={c['oos_sharpe']:.4f} "
              f"OOS 复利={c['oos_ret']*100:+.2f}% 全样本={c['sharpe_full']:.4f}")
    report["combo"] = combo
    print(f"  单引擎 A int: OOS Sharpe={m_oos_a.sharpe:.4f} OOS 复利={m_oos_a.total_return*100:+.2f}%")
    print(f"  引擎 B: OOS Sharpe={_m_oos_b.sharpe:.4f} OOS 复利={_m_oos_b.total_return*100:+.2f}%")

    # ---- 落盘 ----
    out = ART / "qa_p15_independent_results.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=float)
    print(f"[DONE] 总耗时 {time.time()-t_all:.0f}s → {out}")


if __name__ == "__main__":
    main()
