"""P0-1 信号监控自适应降仓（止血）。

核心思想：截面排序信号的有效性会随 regime 漂移（反思已证实 OOS 段倒挂）。
用「过去 W 日跨品种 exp_ret 分位价差」滚动监控信号有效性（严格因果 ≤t），
价差转负/走弱时对单边多头目标降仓（scale∈[0,1]），避免在信号失效期持仓。

口径：嵌套 walk_forward 信号（6 品种，2018~2026-08）→ BacktestEngine 完整回测
分段：全样本（2018~2024-07-17，模型学习期，应无损/少损）
      OOS（2024-07-18~数据末，真新数据，止血主战场）
对比：baseline（无监控） vs step（spread<0→0 否则 1） vs linear（clip(spread/thr,0,1)）
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
TOP_K = 0.30          # 单边多头 top 30%
NOTIONAL_FRAC = 0.30  # 名义 30% 权益/标的
COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)


def build_signals(n_jobs: int, use_cache: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (sig, prices)。sig 含 symbol/ts/exp_ret，prices 为 MultiIndex(symbol,datetime)。
    use_cache=True 时复用 artifacts/signals_cache.parquet（walk_forward 单次，网格秒级）。"""
    from hexbroker.config import load_config

    cache_p = Path("artifacts/signals_cache.parquet")
    if use_cache and cache_p.exists():
        sig = pd.read_parquet(cache_p)
        print(f"[OK] 复用信号缓存 {len(sig)} 条")
        close_parts = []
        bars = load_local_bars(SYMBOLS6)
        for sym in bars.symbols:
            sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
            close_parts.append(sub)
        prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
        prices = prices.loc[~prices.index.duplicated(keep="last")]
        return sig, prices

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
    if use_cache:
        Path("artifacts").mkdir(exist_ok=True)
        sig.to_parquet(cache_p, index=False)
        print(f"[OK] 信号缓存已写入 {cache_p}（{len(sig)} 条）")

    close_parts = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    return sig, prices


def build_rolling_spread(sig: pd.DataFrame, prices: pd.DataFrame, window: int) -> pd.Series:
    """每个交易日 d：用过去 window 日（含 d，严格因果）的跨品种 (exp_ret, realized5)
    配对计算 Q4-Q0 分位价差。返回 {ts: spread}，冷启动为 NaN。"""
    # realized5 面板（5 日已实现收益）
    closes = {}
    for sym in sig["symbol"].unique():
        sub = prices.xs(sym, level=0)["close"]
        closes[sym] = sub
    close_df = pd.DataFrame(closes).sort_index()
    real5 = close_df.shift(-5) / close_df - 1.0

    # exp_ret 面板
    er = sig.set_index(["symbol", "ts"])["exp_ret"]
    er_panel = er.unstack(level=0)

    dates = close_df.index
    spreads: dict[pd.Timestamp, float] = {}
    # 向量化：用 rolling 窗口的配对计算（按日期推进，窗口内堆叠）
    er_stacked = er_panel.stack().replace([np.inf, -np.inf], np.nan).dropna()  # (ts, sym)
    r5_stacked = real5.stack().replace([np.inf, -np.inf], np.nan).dropna()
    pairs = pd.concat([er_stacked.rename("er"), r5_stacked.rename("r5")], axis=1).dropna()

    # 每个日期：取窗口内配对
    for i, d in enumerate(dates):
        if i < window:
            continue
        w0 = dates[max(0, i - window + 1)]
        sub = pairs.loc[(pairs.index.get_level_values(0) >= w0) & (pairs.index.get_level_values(0) <= d)]
        if len(sub) < 20:  # 至少 20 个配对才有统计意义
            continue
        q = pd.qcut(sub["er"].rank(pct=True), 5, labels=False, duplicates="drop")
        if q.nunique() < 3:
            continue
        m = sub.groupby(q)["r5"].mean()
        spreads[d] = float(m.iloc[-1] - m.iloc[0])
    return pd.Series(spreads)


def scale_for(spreads: pd.Series, mode: str, thr: float) -> pd.Series:
    """spread 序列 → scale 序列（0~1）。冷启动（NaN）→ 1（无监控满仓）。"""
    s = spreads.copy()
    if mode == "step":
        sc = (s >= thr).astype(float)
    elif mode == "linear":
        sc = np.clip(s / thr, 0.0, 1.0)
    else:
        raise ValueError(mode)
    sc = sc.fillna(1.0).clip(0.0, 1.0)
    return sc


def run_bt(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
           label: str) -> tuple[float, float, float, int]:
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["rank_pct"] >= 1.0 - TOP_K) & ~px_missing
    if scale is not None:
        df["_scale"] = df["ts"].map(scale).fillna(1.0)
        df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    else:
        df["target"] = np.where(long_flag, qty, 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")
    n_active = int((df["target"] > 0).sum())
    print(f"[{label}] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} 持仓信号={n_active}")
    return m.annual_return, m.max_drawdown, m.sharpe, n_active


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--no-cache", action="store_true", help="强制重跑 walk_forward")
    args = ap.parse_args()

    print("=" * 72)
    print(f"P0-1 信号监控自适应降仓（W={args.window}日滚动价差，严格因果 ≤t）")
    print("=" * 72)
    sig, prices = build_signals(args.n_jobs, use_cache=not args.no_cache)
    print(f"[OK] 信号 {len(sig)} 条 | prices {len(prices)} 行")
    oos_mask = sig["ts"] >= pd.Timestamp("2024-07-18")
    full, oos = sig, sig[oos_mask]
    print(f"[OK] OOS 段（真新数据）{len(oos)} 条")

    # 滚动监控价差
    spreads = build_rolling_spread(sig, prices, args.window)
    print(f"[OK] 滚动价差覆盖 {spreads.notna().sum()} 个交易日（窗口 {args.window}）")
    if len(spreads):
        print(f"  价差分布: 中位={spreads.median()*100:+.3f}%/5日 均值={spreads.mean()*100:+.3f}% "
              f"负值占比={(spreads<0).mean()*100:.1f}%")
        oos_sp = spreads[spreads.index >= pd.Timestamp("2024-07-18")]
        if len(oos_sp):
            print(f"  OOS 段价差: 中位={oos_sp.median()*100:+.3f}% 负值占比={(oos_sp<0).mean()*100:.1f}%")

    print("\n=== 全样本（2018~2024-07-17，应少损） ===")
    run_bt(full, prices, None, "baseline 无监控")
    run_bt(full, prices, scale_for(spreads, "step", 0.0), "step spread<0→0")
    run_bt(full, prices, scale_for(spreads, "linear", 0.003), "linear thr=0.3%")

    print("\n=== OOS（2024-07-18~数据末，真新数据，止血主战场） ===")
    run_bt(oos, prices, None, "baseline 无监控")
    run_bt(oos, prices, scale_for(spreads, "step", 0.0), "step spread<0→0")
    run_bt(oos, prices, scale_for(spreads, "linear", 0.003), "linear thr=0.3%")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/adaptive-exposure-2026-08-17.md")


if __name__ == "__main__":
    main()
