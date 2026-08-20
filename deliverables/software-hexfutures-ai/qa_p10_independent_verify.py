"""QA P10-2 独立交叉验证（fresh-eyes，不调用 p5/p10 策略函数）。

目标：独立复现 P10-2 权重下界结论（A0/B100 OOS 1.622 > A5/B95 1.618 >
A10/B90 1.612 > A15/B85 1.602；全样本反向 0.972 < 1.023 < 1.049）。

独立性声明：
  - 复用（基础设施，非被验证策略逻辑）：load_prices / load_basis_panel /
    CONTRACTS18 / SYMBOLS18 / BacktestEngine / CostModel / compute_metrics /
    常量（INITIAL_CAPITAL / OOS_START / TOP_K / NOTIONAL_FRAC / BASIS_WIN /
    BASIS_THR）。
  - 自实现（被验证的策略逻辑）：引擎 A 每日截面 rank top30% + S2 min=3 targets；
    引擎 B 基差滚动 252 分位 >=0.70 targets；组合 w_a*ret_a+(1-w_a)*ret_b（vol=N）。
  - 刻意不 import：engine_a_targets_cs / engine_a_selection / combo_stats_row /
    run_engine_row / p10_weight_lower_bound。

口径：滑点1tick + 费0.005% + 保证金12% + CONTRACTS18 + 初始 1e6；
OOS 2024-07-18 后；复利口径评估（OOS 权益曲线 total_return）。

用法：python deliverables/software-hexfutures-ai/qa_p10_independent_verify.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_basis_panel, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    NOTIONAL_FRAC,
    OOS_START,
    TOP_K,
)

V8_PATH = ROOT / "artifacts" / "signals_cache18_grouped_v8.parquet"
W_A_GRID = [0.00, 0.05, 0.10, 0.15]


# ---------------------------------------------------------------------------
# 自实现：引擎 A targets（每日截面 rank top30% + S2 min=3）
# ---------------------------------------------------------------------------
def my_engine_a_targets(prices: pd.DataFrame) -> pd.DataFrame:
    sig = pd.read_parquet(V8_PATH)
    sig["ts"] = pd.to_datetime(sig["ts"])
    # 每日截面 rank(pct=True)：[0,1] 越大越强
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    # 价格对齐：按 (symbol, ts) 取 close（与参考实现同源数据，管道对齐）
    px = prices["close"].reset_index().rename(columns={"datetime": "ts"})
    sig = sig.merge(px, on=["symbol", "ts"], how="left")
    sig["_mult"] = sig["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18})
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    # 选中：rank_pct >= 1-top_k 且 价格存在 且 当日品种数 >= 3（S2）
    sel = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["close"].notna() & (sig["_day_cnt"] >= 3)
    sig["target"] = np.where(
        sel,
        np.floor(notional / (sig["close"] * sig["_mult"])).astype(int),
        0,
    )
    out = sig[sig["close"].notna()].set_index(["symbol", "ts"])[["target"]].sort_index()
    return out


# ---------------------------------------------------------------------------
# 自实现：引擎 B targets（基差品种内滚动 252 分位 >= 0.70 做多）
# ---------------------------------------------------------------------------
def my_engine_b_targets(prices: pd.DataFrame) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(BASIS_WIN, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]  # 按 (symbol, datetime) 对齐
    sig = sig.dropna(subset=["_px", "br_rank"])
    sym_level = sig.index.get_level_values("symbol")
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sig["target"] = np.where(
        sig["br_rank"] >= BASIS_THR,
        np.floor(notional / (sig["_px"] * sym_level.map(
            {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 自实现：回测 + 组合指标
# ---------------------------------------------------------------------------
def run_engine_rets(cfg, cost, prices, targets, label: str) -> pd.Series:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    print(f"  [{label}] 全样本 Sharpe={m.sharpe:.3f} MaxDD={m.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={m_oos.sharpe:.3f} OOS复利={m_oos.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos.max_drawdown*100:.1f}%")
    return ret


def my_combo_stats(ret_a: pd.Series, ret_b: pd.Series, w_a: float) -> dict:
    comb = w_a * ret_a + (1.0 - w_a) * ret_b  # vol_target=False
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "w_a": w_a,
        "w_b": round(1.0 - w_a, 4),
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
    }


def main() -> None:
    t0 = time.time()
    print("=" * 96)
    print("QA P10-2 独立交叉验证（fresh-eyes：自实现引擎A/B targets + 组合，不调用 p5/p10 策略函数）")
    print(f"OOS 起点 {OOS_START} | 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | 初始 {INITIAL_CAPITAL:,.0f}")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"v8 存在={V8_PATH.exists()}")

    print("\n[1] 自实现引擎 B（win252/thr0.7）")
    tgt_b = my_engine_b_targets(prices)
    ret_b = run_engine_rets(cfg, cost, prices, tgt_b, "B")

    print("\n[2] 自实现引擎 A（v8 每日截面 rank top30% + S2 min=3）")
    tgt_a = my_engine_a_targets(prices)
    ret_a = run_engine_rets(cfg, cost, prices, tgt_a, "A")

    print("\n[3] 组合权重网格 A∈{0.00,0.05,0.10,0.15}（vol=N）")
    rows = []
    for w_a in W_A_GRID:
        r = my_combo_stats(ret_a, ret_b, w_a)
        rows.append(r)
        print(f"  A{int(w_a*100):02d}/B{int((1-w_a)*100):02d}: "
              f"OOS Sharpe={r['oos_sharpe']:.3f} OOS复利={r['oos_ret']*100:+.2f}% "
              f"OOS MaxDD={r['oos_maxdd']*100:.2f}% 全样本 Sharpe={r['sharpe_full']:.3f}")

    # 与工程师/P10 csv 期望值对比（逐位容差 1e-9）
    expect = {
        0.00: (1.6222381681290192, 0.3104547286552688, 0.9721180327379031),
        0.05: (1.6184139670098845, 0.2987006239509882, 0.9978060378490342),
        0.10: (1.6116873407059440, 0.2870050184411526, 1.0234169205112693),
        0.15: (1.6015317645301745, 0.2753682412788980, 1.0485124548133420),
    }
    print("\n[4] 与 artifacts/p10_weight_lower_bound.csv 期望值对比")
    all_ok = True
    for r in rows:
        e_sh, e_ret, e_full = expect[r["w_a"]]
        ok_sh = abs(r["oos_sharpe"] - e_sh) < 1e-9
        ok_ret = abs(r["oos_ret"] - e_ret) < 1e-9
        ok_full = abs(r["sharpe_full"] - e_full) < 1e-9
        all_ok &= ok_sh and ok_ret and ok_full
        print(f"  A{int(r['w_a']*100):02d}/B{int(r['w_b']*100):02d}: "
              f"OOS Sharpe 复现={r['oos_sharpe']:.12f} vs 期望 {e_sh:.12f} {'OK' if ok_sh else 'MISMATCH'} | "
              f"OOS复利 {r['oos_ret']:.12f} vs {e_ret:.12f} {'OK' if ok_ret else 'MISMATCH'} | "
              f"全样本 {r['sharpe_full']:.12f} vs {e_full:.12f} {'OK' if ok_full else 'MISMATCH'}")
    print(f"\n  逐位一致（1e-9）: {'全部 OK' if all_ok else '存在 MISMATCH'}")

    # 单调性裁决
    seq = [r["oos_sharpe"] for r in rows]
    print(f"  OOS Sharpe 序列（A0→A15）: {[round(s,4) for s in seq]}")
    print(f"  OOS 单调（A0≥A5≥A10≥A15）: {all(seq[i] >= seq[i+1]-1e-12 for i in range(len(seq)-1))}")
    print(f"  全样本序列（A0→A15）: {[round(r['sharpe_full'],4) for r in rows]}（反向单调）")

    print("=" * 96)
    print(f"[DONE] 总耗时 {time.time()-t0:.0f}s")
    print("=" * 96)


if __name__ == "__main__":
    main()
