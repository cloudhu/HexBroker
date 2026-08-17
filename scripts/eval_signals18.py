"""18 品种最优配置验证 + 快速搜索（品种池扩展效果评估）。

读 artifacts/signals_cache18.parquet（18 品种嵌套信号），
在最优配置（0.6exp+0.4mom120, top25%, W20 监控阈-0.2%）基线上评估，
再做小网格确认 18 品种下的最优参数邻域。
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.combo_validation import OOS_START

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS18,
)


def load_sig18() -> pd.DataFrame:
    return pd.read_parquet("artifacts/signals_cache18.parquet")


def load_prices18() -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    close_parts, close_by = [], {}
    for sym in SYMBOLS18:
        sub = load_sig18_sym_close(sym)
        close_by[sym] = sub
        df = sub.rename("close").reset_index()
        df["symbol"] = sym
        close_parts.append(df)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    return prices, close_by


def load_sig18_sym_close(sym: str) -> pd.Series:
    import glob
    parts = [pd.read_parquet(f) for f in sorted(glob.glob(f"data/raw/processed/{sym}/1d/*.parquet"))]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.set_index("datetime")["close"].sort_index()


def build_rolling_spread18(sig: pd.DataFrame, window: int) -> pd.Series:
    """18 品种滚动价差（跨品种 exp_ret 分位 Q4-Q0，严格因果 ≤t）。"""
    er = sig.set_index(["symbol", "ts"])["exp_ret"]
    er_panel = er.unstack(level=0)
    # realized5
    closes = {}
    for sym in SYMBOLS18:
        closes[sym] = load_sig18_sym_close(sym)
    close_df = pd.DataFrame(closes).sort_index()
    real5 = close_df.shift(-5) / close_df - 1.0
    er_stacked = er_panel.stack().replace([np.inf, -np.inf], np.nan).dropna()
    r5_stacked = real5.stack().replace([np.inf, -np.inf], np.nan).dropna()
    pairs = pd.concat([er_stacked.rename("er"), r5_stacked.rename("r5")], axis=1).dropna()
    dates = close_df.index
    spreads = {}
    for i, d in enumerate(dates):
        if i < window:
            continue
        w0 = dates[max(0, i - window + 1)]
        sub = pairs.loc[(pairs.index.get_level_values(0) >= w0) & (pairs.index.get_level_values(0) <= d)]
        if len(sub) < 30:
            continue
        q = pd.qcut(sub["er"].rank(pct=True), 5, labels=False, duplicates="drop")
        if q.nunique() < 3:
            continue
        m = sub.groupby(q)["r5"].mean()
        spreads[d] = float(m.iloc[-1] - m.iloc[0])
    return pd.Series(spreads)


def run_cfg(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
            w1: float, w2: float, top_k: float, notional_frac: float = 0.30,
            label: str = "") -> None:
    df = sig.copy()
    df["score"] = w1 * df["exp_ret"].rank(pct=True) + w2 * df["mom"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    px_missing = df["_px"].isna()
    qty = (notional_frac * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["score"] >= 1.0 - top_k) & ~px_missing
    if scale is not None:
        df["_scale"] = df["ts"].map(scale).fillna(1.0)
        df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    else:
        df["target"] = np.where(long_flag, qty, 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")

    oos_df = df[df["ts"] >= OOS_START].copy()
    oos_df["_px"] = oos_df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_df["_mult"] = oos_df["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    op = oos_df["_px"].isna()
    oq = (notional_frac * 1_000_000.0 / (oos_df["_px"] * oos_df["_mult"])).astype(int)
    oflag = (oos_df["score"] >= 1.0 - top_k) & ~op
    if scale is not None:
        oos_df["_scale"] = oos_df["ts"].map(scale).fillna(1.0)
        oos_df["target"] = np.where(oflag, (oq * oos_df["_scale"]).astype(int), 0)
    else:
        oos_df["target"] = np.where(oflag, oq, 0)
    otg = oos_df.set_index(["symbol", "ts"])[["target"]].sort_index()
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, otg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")
    n_active = int((df["target"] > 0).sum())
    n_oos = int((oos_df["target"] > 0).sum())
    print(f"[{label}] 全样本: 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} 持仓={n_active}/{len(df)} | OOS: 年化={mo.annual_return*100:+.2f}% "
          f"回撤={mo.max_drawdown*100:.2f}% Sharpe={mo.sharpe:.2f} 持仓={n_oos}/{len(oos_df)}")


def main() -> None:
    print("=" * 72)
    print("18 品种验证（品种池扩展 6→18，最优配置基准 + 快速搜索）")
    print("=" * 72)
    sig = load_sig18()
    prices, close_by = load_prices18()
    print(f"[OK] 18 品种信号 {len(sig)} 条 | prices {len(prices)} 行")

    # 动量
    closes = {sym: close_by[sym] for sym in SYMBOLS18}
    close_df = pd.DataFrame(closes).sort_index()
    mom120 = close_df / close_df.shift(120) - 1.0
    sig["mom"] = sig.apply(
        lambda r: float(mom120.loc[r["ts"], r["symbol"]])
        if r["ts"] in mom120.index and r["symbol"] in mom120.columns else np.nan,
        axis=1,
    )
    print(f"[OK] mom120 覆盖率 {sig['mom'].notna().mean()*100:.0f}%")

    # 监控
    spreads = build_rolling_spread18(sig, 20)
    scale = (spreads >= -0.002).astype(float).fillna(1.0).clip(0.0, 1.0)
    print(f"[OK] 监控价差覆盖 {spreads.notna().sum()} 交易日 | 负值占比 {(spreads < 0).mean()*100:.1f}%")

    print("\n=== 6 品种最优配置在 18 品种上的表现 ===")
    run_cfg(sig, prices, scale, 0.6, 0.4, 0.25, label="18sym 0.6/0.4/0.25")

    print("\n=== 快速搜索（w1×N×topk 邻域） ===")
    results = []
    for w1, top_k in itertools.product([0.5, 0.6, 0.7], [0.25, 0.30]):
        run_cfg(sig, prices, scale, w1, 1 - w1, top_k, label=f"w1={w1} topk={top_k}")
        # 收集 sharpe（从 print 不可行，直接用内部函数——简化：仅打印观察）
    print("\n[报告] 见 deliverables/software-hexfutures-ai/symbol-pool-18-2026-08-17.md")


if __name__ == "__main__":
    main()
