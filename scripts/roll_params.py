"""P0-2 滚动重训参数（嵌套内定期重选）。

核心思想：静态参数（top-30%、MA20）是样本内选择的，regime 漂移后失效。
改为「每 T 天，用过去 L 天的已实现信号（≤t，严格因果）对参数网格做
BacktestEngine 完整回测，选 Sharpe 最优参数组合，应用到未来 T 天」。

参数网格：top_k ∈ {0.2, 0.3, 0.4} × ma ∈ {None, 10, 20, 30}（12 组合）
重选周期 T=120 天（~半年），选择窗口 L=360 天（一年）。

对比（全样本 + OOS 段）：
  A. 固定 top-30%（无 trend）——基线
  B. 固定 top-30% + MA20——旧最优静态配置
  C. 滚动重选 top_k + MA——本实验
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.monitor_adaptive_exposure import build_signals

CONTRACTS = {
    "au0": {"multiplier": 1000.0, "min_tick": 0.02},
    "ag0": {"multiplier": 15.0, "min_tick": 0.01},
    "m0": {"multiplier": 10.0, "min_tick": 1.0},
    "cu0": {"multiplier": 5.0, "min_tick": 10.0},
    "rb0": {"multiplier": 10.0, "min_tick": 1.0},
    "i0": {"multiplier": 100.0, "min_tick": 0.5},
}
NOTIONAL_FRAC = 0.30
COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)
TOP_K_GRID = [0.20, 0.30, 0.40]
MA_GRID = [None, 10, 20, 30]


def _trend_ok(close_by: dict[str, pd.Series], sym: str, ts, ma: int | None) -> bool:
    if ma is None:
        return True
    st = close_by[sym].loc[:ts]
    if len(st) < ma:
        return False
    return bool(st.iloc[-1] >= st.rolling(ma, min_periods=ma).mean().iloc[-1])


def build_targets(seg: pd.DataFrame, prices: pd.DataFrame, close_by: dict[str, pd.Series],
                  top_k: float, ma: int | None) -> pd.DataFrame:
    df = seg.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    df["_trend"] = df.apply(lambda r: _trend_ok(close_by, r["symbol"], r["ts"], ma), axis=1)
    long_flag = (df["rank_pct"] >= 1.0 - top_k) & ~px_missing & df["_trend"]
    df["target"] = np.where(long_flag, qty, 0)
    return df.set_index(["symbol", "ts"])[["target"]].sort_index()


def backtest_sharpe(targets: pd.DataFrame, prices: pd.DataFrame) -> float:
    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    return float(compute_metrics(pf.equity_curve, freq="1d").sharpe)


def roll_params(sig: pd.DataFrame, prices: pd.DataFrame, close_by: dict[str, pd.Series],
                T: int, L: int) -> tuple[pd.DataFrame, list[dict]]:
    """滚动重选。返回 (带 _topk/_ma 列的 sig, 重选记录)。"""
    ts = sig["ts"].unique()
    ts = pd.DatetimeIndex(sorted(ts))
    record = []
    df = sig.copy()
    df["_topk"], df["_ma"] = 0.30, None  # 默认（首个窗口用静态）

    # 每个重选时点：选择窗口 [t-L, t] 内信号，网格回测选最优
    rebal_dates = []
    t0 = ts[0]
    d = t0
    while d <= ts[-1]:
        rebal_dates.append(d)
        d += pd.Timedelta(days=T)

    for rbi, rb_t in enumerate(rebal_dates):
        sel = df[(df["ts"] >= rb_t - pd.Timedelta(days=L)) & (df["ts"] <= rb_t)]
        if len(sel) < 50:
            continue
        best_sh, best = -1e9, None
        for tk in TOP_K_GRID:
            for ma in MA_GRID:
                try:
                    tgt = build_targets(sel, prices, close_by, tk, ma)
                    sh = backtest_sharpe(tgt, prices)
                except Exception:
                    sh = -1e9
                if sh > best_sh:
                    best_sh, best = sh, (tk, ma)
        if best is None:
            continue
        # 应用窗口 (rb_t, rb_t + T]
        app = df[(df["ts"] > rb_t) & (df["ts"] <= rb_t + pd.Timedelta(days=T))]
        df.loc[app.index, "_topk"] = best[0]
        df.loc[app.index, "_ma"] = best[1]
        record.append({"rebal": rb_t, "topk": best[0], "ma": best[1],
                       "sharpe": best_sh, "n_sel": len(sel)})
    return df, record


def run_fixed(sig: pd.DataFrame, prices: pd.DataFrame, close_by: dict[str, pd.Series],
              top_k: float, ma: int | None, label: str) -> None:
    tgt = build_targets(sig, prices, close_by, top_k, ma)
    sh = backtest_sharpe(tgt, prices)
    # 重新取指标（含回撤/年化）
    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    pf = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, tgt)
    m = compute_metrics(pf.equity_curve, freq="1d")
    print(f"[{label}] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f}")
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--T", type=int, default=120, help="重选周期（天）")
    ap.add_argument("--L", type=int, default=360, help="选择窗口（天）")
    args = ap.parse_args()

    print("=" * 72)
    print(f"P0-2 滚动重训参数（T={args.T}天重选，L={args.L}天选择窗，严格因果 ≤t）")
    print("=" * 72)
    sig, prices = build_signals(args.n_jobs, use_cache=True)
    print(f"[OK] 信号 {len(sig)} 条 | prices {len(prices)} 行")

    # close_by
    close_by: dict[str, pd.Series] = {}
    for sym in sig["symbol"].unique():
        c = prices.xs(sym, level=0)["close"]
        close_by[sym] = c.sort_index()

    oos_mask = sig["ts"] >= pd.Timestamp("2024-07-18")
    print("\n=== 全样本（2018~2026-08） ===")
    run_fixed(sig, prices, close_by, 0.30, None, "A 固定 top30%（基线）")
    run_fixed(sig, prices, close_by, 0.30, 20, "B 固定 top30%+MA20（旧静态最优）")

    rolled, rec = roll_params(sig, prices, close_by, args.T, args.L)
    print(f"[OK] 重选 {len(rec)} 次")
    if rec:
        df_r = pd.DataFrame(rec)
        print(df_r.to_string(index=False))
        print(f"  参数分布: topk={df_r['topk'].value_counts().to_dict()} "
              f"ma={df_r['ma'].value_counts().to_dict()}")
    # 滚动配置回测（用 _topk/_ma）
    tgt = build_targets(rolled, prices, close_by, 0.30, None)  # 占位，实际按列
    df = rolled.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    rows = []
    for _, r in df.iterrows():
        ok_trend = _trend_ok(close_by, r["symbol"], r["ts"], r["_ma"])
        long_flag = (r["rank_pct"] >= 1.0 - r["_topk"]) and (not px_missing[r.name]) and ok_trend
        rows.append(int(qty[r.name]) if long_flag else 0)
    df["target"] = rows
    tgt_r = df.set_index(["symbol", "ts"])[["target"]].sort_index()
    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    pf = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, tgt_r)
    m = compute_metrics(pf.equity_curve, freq="1d")
    print(f"[C 滚动重选 topk+MA] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f}")

    print("\n=== OOS 段（2024-07-18~2026-06，真新数据） ===")
    oos = sig[oos_mask].copy()
    run_fixed(oos, prices, close_by, 0.30, None, "A 固定 top30%（基线）")
    run_fixed(oos, prices, close_by, 0.30, 20, "B 固定 top30%+MA20")
    oos_r = rolled[oos_mask].copy()
    oos_r["rank_pct"] = oos_r["exp_ret"].rank(pct=True)
    oos_r["_px"] = oos_r.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_r["_mult"] = oos_r["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    pxm = oos_r["_px"].isna()
    qty_o = (NOTIONAL_FRAC * 1_000_000.0 / (oos_r["_px"] * oos_r["_mult"])).astype(int)
    rows_o = []
    for i, r in oos_r.iterrows():
        ok_trend = _trend_ok(close_by, r["symbol"], r["ts"], r["_ma"])
        long_flag = (r["rank_pct"] >= 1.0 - r["_topk"]) and (not pxm[i]) and ok_trend
        rows_o.append(int(qty_o[i]) if long_flag else 0)
    oos_r["target"] = rows_o
    tgt_o = oos_r.set_index(["symbol", "ts"])[["target"]].sort_index()
    pf = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, tgt_o)
    m = compute_metrics(pf.equity_curve, freq="1d")
    print(f"[C 滚动重选 topk+MA] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f}")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/rolling-params-2026-08-17.md")


if __name__ == "__main__":
    main()
