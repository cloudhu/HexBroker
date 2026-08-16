"""MA 窗口网格验证（P1 待验证项）：trend 过滤的 MA 窗口敏感性。

在 top-30% 单边多头 + trend 过滤（close<MA_n 空仓）上，对 MA 窗口
{none, 5, 10, 20, 30, 50} 做完整回测，验证 Sharpe/回撤/年化的稳定性，
确认 MA20 是稳健选择而非过拟合尖峰。

walk_forward（嵌套口径）只跑一次，网格内仅重算过滤 + 回测。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config  # noqa: E402
from hexbroker.feature import build_features  # noqa: E402
from hexbroker.backtest.engine import BacktestEngine  # noqa: E402
from hexbroker.backtest.cost import CostModel  # noqa: E402
from hexbroker.evaluation.metrics import compute_metrics  # noqa: E402
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-16"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]
TOP_K = 0.30
NOTIONAL_FRAC = 1.00
INITIAL_CAPITAL = 1_000_000.0
MA_GRID = [5, 10, 20, 30, 50]
CONTRACTS = {
    "au0": {"multiplier": 1000.0, "min_tick": 0.02},
    "ag0": {"multiplier": 15.0, "min_tick": 0.01},
    "m0": {"multiplier": 10.0, "min_tick": 1.0},
}


def build_cfg():
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    return cfg


def make_targets(sig, prices, close_by_sym, ma_window: int | None) -> pd.DataFrame:
    mult_map = {sym: CONTRACTS[sym]["multiplier"] for sym in CONTRACTS}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map(mult_map)
    if ma_window is not None:
        def _trend_ok(sym, ts):
            st = close_by_sym[sym].loc[:ts]
            if len(st) < ma_window:
                return False
            ma = st.rolling(ma_window, min_periods=ma_window).mean().iloc[-1]
            return st.iloc[-1] >= ma
        df["_ok"] = df.apply(lambda r: _trend_ok(r["symbol"], r["ts"]), axis=1)
        df["target"] = np.where(
            (df["rank_pct"] >= 1.0 - TOP_K) & df["_px"].notna() & df["_ok"],
            (notional / (df["_px"] * df["_mult"])).astype(int),
            0,
        )
    else:
        df["target"] = np.where(
            (df["rank_pct"] >= 1.0 - TOP_K) & df["_px"].notna(),
            (notional / (df["_px"] * df["_mult"])).astype(int),
            0,
        )
    return df.set_index(["symbol", "ts"])[["target"]].sort_index()


def main() -> None:
    ap = argparse.ArgumentParser(description="MA 窗口网格验证（trend 过滤敏感性）")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print(f"MA 窗口网格：{MA_GRID}，top-30% + trend 过滤，名义{NOTIONAL_FRAC*100:.0f}%/标的")
    print("=" * 72)

    cfg0 = build_cfg()
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)

    wf = walk_forward_lightgbm(
        cfg0, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None,
        n_jobs_folds=args.n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()

    close_parts = []
    close_by_sym: dict[str, pd.Series] = {}
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_by_sym[sym] = sub.set_index("datetime")["close"].sort_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]

    cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                     slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS)

    rows = []
    variants = [("none（无过滤基线）", None)] + [(f"MA{ma}", ma) for ma in MA_GRID]
    for label, ma in variants:
        targets = make_targets(sig, prices, close_by_sym, ma)
        engine = BacktestEngine(cfg0, cost=cost, initial_capital=INITIAL_CAPITAL)
        portfolio = engine.run(prices, targets)
        m = compute_metrics(portfolio.equity_curve, freq="1d")
        n_long = int((targets["target"] > 0).sum())
        r = {
            "label": label, "ma_window": ma,
            "trigger_rate": n_long / len(sig),
            "annual_return": m.annual_return,
            "max_drawdown": m.max_drawdown,
            "sharpe": m.sharpe,
            "total_return": m.total_return,
            "n_trades": len(engine.broker.trades),
        }
        rows.append(r)
        print(f"[{label}] 触发率={r['trigger_rate']*100:.1f}% 年化={r['annual_return']*100:+.2f}% "
              f"回撤={r['max_drawdown']*100:.2f}% Sharpe={r['sharpe']:.2f} 笔数={r['n_trades']}")

    print("\n[SUMMARY]")
    base = rows[0]
    for r in rows:
        print(f"  {r['label']:<16s} 年化 {r['annual_return']*100:+6.2f}% 回撤 {r['max_drawdown']*100:6.2f}% "
              f"Sharpe {r['sharpe']:.2f} 触发率 {r['trigger_rate']*100:5.1f}%")

    # 稳健性判定：MA 窗口间 Sharpe 波动
    ma_rows = [r for r in rows if r["ma_window"] is not None]
    sharpes = [r["sharpe"] for r in ma_rows]
    print(f"\n[稳健性] MA 窗口 Sharpe 范围 [{min(sharpes):.2f}, {max(sharpes):.2f}]，"
          f"均值 {np.mean(sharpes):.2f}，跨度 {max(sharpes)-min(sharpes):.2f}")
    print(f"[判定] MA20 Sharpe={rows[[r['ma_window'] for r in rows].index(20)]['sharpe']:.2f}，"
          f"窗口网格内 {'稳健（全窗口>1.0）' if min(sharpes) > 1.0 else '有波动（部分窗口<1.0）'}")

    out = DELIVERABLE_DIR / f"ma-window-grid-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"rows": rows}, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
