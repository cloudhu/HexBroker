"""QA 独立复核 P6-3：v2/v3 缓存引擎 A S2 + 组合 A15/B85 + 新增日质量分析。

不调用 p5 的 engine_a_targets_cs；自写按日截面 rank + min=3 构造 targets，
完整 BacktestEngine 口径（滑点1tick + 费0.005% + 保证金12% + CONTRACTS18）。
"""
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
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets

TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
MIN_SYMBOLS = 3
V2 = ROOT / "artifacts/signals_cache18_grouped_v2.parquet"
V3 = ROOT / "artifacts/signals_cache18_grouped_v3.parquet"


# ---------------------------------------------------------------------------
# 独立实现：按日截面 rank + min=3（不依赖 p5）
# ---------------------------------------------------------------------------
def qa_engine_a_targets(prices: pd.DataFrame, cache_path, top_k=TOP_K,
                        min_symbols=MIN_SYMBOLS) -> pd.DataFrame:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC

    # 按日截面 rank（同日内 exp_ret 排序，pct 分位 [0,1] 越大越强）
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(mult_map)

    long_cond = (sig["rank_pct"] >= 1.0 - top_k) & sig["_px"].notna()
    if min_symbols is not None:
        long_cond = long_cond & (sig["_day_cnt"] >= min_symbols)

    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = notional / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(long_cond, raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def qa_run_engine(cfg, cost, prices, targets, label=""):
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, eq, m, m_oos


def qa_combo_stats(ret_a, ret_b, w_a):
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[common].sort_index(), ret_b.loc[common].sort_index()
    comb = w_a * ra + (1.0 - w_a) * rb
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return m, m_oos


def main() -> None:
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"prices: {len(prices)} 行 | OOS_START={OOS_START} | top_k={TOP_K} min={MIN_SYMBOLS}")

    # 引擎 B（固定 win252/thr0.7）
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, eq_b, m_b, m_b_oos = qa_run_engine(cfg, cost, prices, tgt_b, "B")
    print(f"pureB: full Sharpe={m_b.sharpe:.3f} OOS Sharpe={m_b_oos.sharpe:.3f}")

    print("\n=== [1] 独立引擎 A S2（自写截面 rank + min=3）===")
    results = {}
    for label, path in (("v2", V2), ("v3", V3)):
        tgt = qa_engine_a_targets(prices, path)
        ret, eq, m, m_oos = qa_run_engine(cfg, cost, prices, tgt, f"A-S2-{label}")
        n_long = int((tgt["target"] > 0).sum())
        n_days = pd.to_datetime(tgt.index.get_level_values(1)).nunique()
        print(f"[{label}] A-S2: full Sharpe={m.sharpe:.3f} 年化={m.annual_return*100:+.1f}% "
              f"MaxDD={m.max_drawdown*100:.1f}% | OOS Sharpe={m_oos.sharpe:.3f} "
              f"OOS MaxDD={m_oos.max_drawdown*100:.1f}% | 做多行={n_long} 有信号日={n_days}")
        results[label] = dict(ret=ret, eq=eq, m=m, m_oos=m_oos, tgt=tgt)

    # 与 p5 实现交叉校验（仅 sanity）
    print("\n[sanity] 与 p5 engine_a_targets_cs 对比（target 行数/求和）:")
    from scripts.p5_engineA_cross_section import engine_a_targets_cs
    for label, path in (("v2", V2), ("v3", V3)):
        t_p5 = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS, cache_path=path)
        t_qa = results[label]["tgt"]
        same = (t_p5["target"].reindex(t_qa.index).fillna(0) == t_qa["target"].fillna(0)).all()
        print(f"  {label}: p5 vs qa targets identical = {same} | "
              f"p5 sum={int(t_p5['target'].sum())} qa sum={int(t_qa['target'].sum())}")

    print("\n=== [2] 组合 A15/B85（vol_target=False，引擎B固定）===")
    for label in ("v2", "v3"):
        m, m_oos = qa_combo_stats(results[label]["ret"], ret_b, 0.15)
        print(f"[{label}] A15/B85: full Sharpe={m.sharpe:.3f} MaxDD={m.max_drawdown*100:.1f}% "
              f"| OOS Sharpe={m_oos.sharpe:.3f} OOS MaxDD={m_oos.max_drawdown*100:.1f}%")

    # 组合 A25/B75 参考
    print("\n=== [3] 组合 A25/B75 参考 ===")
    for label in ("v2", "v3"):
        m, m_oos = qa_combo_stats(results[label]["ret"], ret_b, 0.25)
        print(f"[{label}] A25/B75: OOS Sharpe={m_oos.sharpe:.3f}")

    print("\n=== [4] 结构分解：v3 是否仅在新增日拖累 ===")
    v2d = pd.read_parquet(V2)
    v2d["ts"] = pd.to_datetime(v2d["ts"])
    v2_days = set(v2d["ts"].dt.date.unique())
    # v3 过滤到仅 v2 日期 → 应与 v2 完全一致
    v3d = pd.read_parquet(V3)
    v3d["ts"] = pd.to_datetime(v3d["ts"])
    v3_on_v2 = v3d[v3d["ts"].dt.date.isin(v2_days)]
    v3_new = v3d[~v3d["ts"].dt.date.isin(v2_days)]
    print(f"v3 行: {len(v3d)} | 落在 v2 日期: {len(v3_on_v2)} | 新增日期行: {len(v3_new)}")
    print(f"新增日期数: {v3_new['ts'].dt.date.nunique()}")

    # 用 v3 但仅保留 v2 日期的 targets → 应与 v2 相同
    tmp = ROOT / "artifacts/_qa_v3_on_v2.parquet"
    v3_on_v2.to_parquet(tmp, index=False)
    t_sub = qa_engine_a_targets(prices, tmp)
    t_v2 = results["v2"]["tgt"]
    same = (t_sub["target"].reindex(t_v2.index).fillna(0) == t_v2["target"].fillna(0)).all()
    print(f"v3∩v2日期 targets == v2 targets: {same}")
    ret_sub, eq_sub, m_sub, m_sub_oos = qa_run_engine(cfg, cost, prices, t_sub, "v3-subset")
    print(f"[v3∩v2日期] A-S2: OOS Sharpe={m_sub_oos.sharpe:.3f}（v2 应为 {results['v2']['m_oos'].sharpe:.3f}）")

    print("\n=== [5] 新增信号日质量分析（OOS 段 2024-07-18+）===")
    # 用 realized 前向收益评估 exp_ret 方向性：prices close pct_change(h=5)
    fwd5 = prices.groupby(level=0)["close"].pct_change(5).rename("fwd5")
    fwd5 = fwd5.reset_index()
    fwd5["ts"] = pd.to_datetime(fwd5["datetime"])
    fwd5 = fwd5.set_index(["symbol", "ts"])["fwd5"]

    for name, df in (("v2", v2d), ("v3", v3d), ("v3_new", v3_new), ("v3_on_v2", v3_on_v2)):
        o = df[df["ts"] >= pd.Timestamp(OOS_START)]
        if len(o) == 0:
            print(f"[{name}] OOS 段无行")
            continue
        keys = pd.MultiIndex.from_frame(o[["symbol", "ts"]])
        o = o.copy()
        o["fwd5"] = fwd5.reindex(keys).values
        valid = o["fwd5"].notna()
        # 日截面 top30%（rank）在 OOS 的均值 fwd5
        o2 = o[valid].copy()
        o2["rank_pct"] = o2.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
        sel = o2[o2["rank_pct"] >= 1.0 - TOP_K]
        print(f"[{name}] OOS 行数={len(o)} 有效fwd={valid.sum()} | exp_ret均值={o['exp_ret'].mean():+.4f} "
              f"| 选中(top30%) fwd5均值={sel['fwd5'].mean():+.5f} "
              f"| 选中fwd5>0占比={100*(sel['fwd5']>0).mean():.1f}% "
              f"| 全部 fwd5均值={o2['fwd5'].mean():+.5f}")

    # 新增日 OOS 的分布对比
    print("\n  v3 新增日 vs v2 日（OOS 段）exp_ret 分布:")
    o_v2 = v2d[v2d["ts"] >= pd.Timestamp(OOS_START)]["exp_ret"]
    o_new = v3_new[v3_new["ts"] >= pd.Timestamp(OOS_START)]["exp_ret"]
    o_all3 = v3d[v3d["ts"] >= pd.Timestamp(OOS_START)]["exp_ret"]
    print(f"  v2 日 exp_ret: n={len(o_v2)} mean={o_v2.mean():+.4f} std={o_v2.std():.4f}")
    print(f"  v3 新增日 exp_ret: n={len(o_new)} mean={o_new.mean():+.4f} std={o_new.std():.4f}")
    print(f"  v3 全部 exp_ret: n={len(o_all3)} mean={o_all3.mean():+.4f} std={o_all3.std():.4f}")

    print("\n=== [6] 覆盖率机制验证（为何 1464 而非 ~1900）===")
    all_days = v3d["ts"].dt.date.unique()
    tr_days = pd.bdate_range(v3d["ts"].min(), v3d["ts"].max())
    n_tr = len(tr_days)
    print(f"v3 覆盖交易日: {len(all_days)} / 区间工作日 {n_tr} = {100*len(all_days)/n_tr:.1f}%")
    per_sym = v3d.groupby("symbol")["ts"].apply(lambda s: s.dt.date.nunique())
    print(f"单品种平均覆盖天数: {per_sym.mean():.0f} (min {per_sym.min()}, max {per_sym.max()})")
    # 单品种覆盖比例
    for s in ["m0", "cu0", "au0"]:
        sd = set(v3d[v3d["symbol"] == s]["ts"].dt.date.unique())
        print(f"  {s}: 覆盖 {len(sd)} 天 / {n_tr} = {100*len(sd)/n_tr:.0f}%")


if __name__ == "__main__":
    main()
