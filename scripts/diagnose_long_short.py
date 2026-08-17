"""全项目反思·关键验证：跨品种多空 vs 单边多头（同段同口径）。

核心假设：alpha 是截面排序（合并分位 Q4-Q0 = 0.688%/5日，品种内≈0）——
排序两端都有信息，单边多头丢弃了空头侧 alpha。跨品种多空应释放这部分收益。

口径：嵌套 walk_forward 信号（6 品种）→ BacktestEngine 完整回测（含成本/保证金）
分段：OOS 2024-07-18 ~ 数据末（真新数据，未参与训练/选择）
对比：none(单边多头 top30%) vs LS(多空 top30/bottom30)
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
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import SYMBOLS6, load_local_bars

CONTRACTS = {
    "au0": {"multiplier": 1000.0, "min_tick": 0.02},
    "ag0": {"multiplier": 15.0, "min_tick": 0.01},
    "m0": {"multiplier": 10.0, "min_tick": 1.0},
    "cu0": {"multiplier": 5.0, "min_tick": 10.0},
    "rb0": {"multiplier": 10.0, "min_tick": 1.0},
    "i0": {"multiplier": 100.0, "min_tick": 0.5},
}
OOS_START = pd.Timestamp("2024-07-18")
NOTIONAL_FRAC = 0.30  # 每侧 30% 权益名义


def build_signals(n_jobs: int) -> pd.DataFrame:
    from hexbroker.config import load_config

    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS6)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": ["spx", "uup"]}

    bars = load_local_bars(SYMBOLS6)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in ["spx", "uup"]}
    features = build_features(bars, cfg, global_close=gc)
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None, n_jobs_folds=n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig


def build_prices(bars) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    close_parts, close_by = [], {}
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_by[sym] = sub.set_index("datetime")["close"].sort_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    return prices, close_by


def run_bt(sig: pd.DataFrame, prices: pd.DataFrame, mode: str, notional: float) -> tuple[float, float, float, int]:
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    if mode == "long_only":
        df["target"] = np.where((df["rank_pct"] >= 0.7) & ~px_missing,
                                (notional / (df["_px"] * df["_mult"])).astype(int), 0)
    elif mode == "long_short":
        long_qty = (notional / (df["_px"] * df["_mult"])).astype(int)
        df["target"] = np.where((df["rank_pct"] >= 0.7) & ~px_missing, long_qty,
                                np.where((df["rank_pct"] <= 0.3) & ~px_missing, -long_qty, 0))
    else:
        raise ValueError(mode)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()
    cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                     slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS)
    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=cost, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")
    n_active = int((df["target"] != 0).sum())
    return m.annual_return, m.max_drawdown, m.sharpe, n_active


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    args = ap.parse_args()

    print("=" * 72)
    print("反思验证：跨品种多空 vs 单边多头（OOS 真新数据，BacktestEngine 完整口径）")
    print("=" * 72)
    sig = build_signals(args.n_jobs)
    bars = load_local_bars(SYMBOLS6)
    prices, _ = build_prices(bars)

    oos = sig[sig["ts"] >= OOS_START].copy()
    print(f"[OK] OOS 信号 {len(oos)} 条（{oos['ts'].min().date()}~{oos['ts'].max().date()}）")
    notional = 1_000_000.0 * NOTIONAL_FRAC

    # ---- Q0 空头侧结构分解（向量化 5 日实际收益） ----
    closes = {}
    for sym in bars.symbols:
        c = bars.by_symbol(sym)["close"].astype(float)
        if isinstance(c.index, pd.MultiIndex):
            c = c.reset_index(level=0, drop=True)
        closes[sym] = c
    close_df = pd.DataFrame(closes).sort_index()
    real_df = close_df.shift(-5) / close_df - 1.0
    rows = []
    for _, r in oos.iterrows():
        if r["ts"] in real_df.index and r["symbol"] in real_df.columns:
            v = real_df.loc[r["ts"], r["symbol"]]
            if pd.notna(v):
                rows.append((r["symbol"], r["ts"], r["exp_ret"], float(v)))
    rd = pd.DataFrame(rows, columns=["symbol", "ts", "exp_ret", "real5"])
    if len(rd):
        rd["rank_pct"] = rd["exp_ret"].rank(pct=True)
        rd["q"] = pd.cut(rd["rank_pct"], [0, 0.2, 0.4, 0.6, 0.8, 1.0], labels=[0, 1, 2, 3, 4]).astype(int)
        print("\n[OOS 分位桶实际 5 日收益（诊断空头侧）]")
        for q, sub in rd.groupby("q"):
            print(f"  Q{q}: n={len(sub):3d} 实际={sub['real5'].mean()*100:+.3f}% 上涨率={(sub['real5']>0).mean()*100:.0f}%")
        mm = rd.groupby("q")["real5"].mean()
        print(f"  Q4-Q0 价差={mm.iloc[-1]*100-mm.iloc[0]*100:+.3f}% | Q0 做空侧={mm.iloc[0]*100:+.3f}% → 做空 Q0 期望 {-mm.iloc[0]*100:.3f}%/5日")
        print("\n[OOS 各品种 base rate]")
        for sym, sub in rd.groupby("symbol"):
            print(f"  {sym}: n={len(sub):3d} 平均={sub['real5'].mean()*100:+.3f}% 上涨率={(sub['real5']>0).mean()*100:.0f}%")

    results = {}
    for mode, label in [("long_only", "单边多头 top30%"), ("long_short", "跨品种多空 top30/bottom30")]:
        ar, mdd, sh, n = run_bt(oos, prices, mode, notional)
        results[mode] = (ar, mdd, sh, n)
        print(f"[{label}] 年化={ar*100:+.2f}% 回撤={mdd*100:.2f}% Sharpe={sh:.2f} 持仓信号={n}")

    lo, ls = results["long_only"], results["long_short"]
    print()
    print("[对比]")
    print(f"  单边多头: 年化 {lo[0]*100:+.2f}% / Sharpe {lo[2]:.2f}")
    print(f"  跨品种多空: 年化 {ls[0]*100:+.2f}% / Sharpe {ls[2]:.2f}")
    if ls[0] > lo[0]:
        print(f"[结论] 多空释放空头侧 alpha：年化 {ls[0]/lo[0]*100 if lo[0]>0 else float('inf'):.1f}× 于单边多头")
    else:
        print("[结论] 多空未胜出——空头侧 alpha 被成本/趋势抵消")


if __name__ == "__main__":
    main()
