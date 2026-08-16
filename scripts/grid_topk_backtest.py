"""触发阈值网格搜索（P1）：exp_ret top-k ∈ {10%..90%} 完整回测对比。

walk_forward（嵌套口径）只跑一次缓存信号；网格内仅重算 targets + BacktestEngine，
指标：年化/最大回撤/Sharpe/触发率/交易笔数/成本拖累。判定最优 top-k。
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
NOTIONAL_FRAC = 1.00  # 100% 权益名义/标的（与 P0 回测口径一致）
INITIAL_CAPITAL = 1_000_000.0
CONTRACTS = {
    "au0": {"multiplier": 1000.0, "min_tick": 0.02},
    "ag0": {"multiplier": 15.0, "min_tick": 0.01},
    "m0": {"multiplier": 10.0, "min_tick": 1.0},
}
TOP_K_GRID = [0.10, 0.20, 0.30, 0.40, 0.50, 0.70]


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


def make_targets(sig: pd.DataFrame, prices: pd.DataFrame, top_k: float, notional: float) -> pd.DataFrame:
    mult_map = {sym: CONTRACTS[sym]["multiplier"] for sym in CONTRACTS}
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map(mult_map)
    df["target"] = np.where(
        (df["rank_pct"] >= 1.0 - top_k) & df["_px"].notna(),
        (notional / (df["_px"] * df["_mult"])).astype(int),
        0,
    )
    return df.set_index(["symbol", "ts"])[["target"]].sort_index()


def run_once(cfg0, bars, features, prices, sig, top_k, n_jobs) -> dict:
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    targets = make_targets(sig, prices, top_k, notional)
    n_long = int((targets["target"] > 0).sum())
    cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                     slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS)
    engine = BacktestEngine(cfg0, cost=cost, initial_capital=INITIAL_CAPITAL)
    portfolio = engine.run(prices, targets)
    m = compute_metrics(portfolio.equity_curve, freq="1d")
    return {
        "top_k": top_k,
        "trigger_rate": n_long / len(sig),
        "n_long": n_long,
        "n_trades": len(engine.broker.trades),
        "total_return": m.total_return,
        "annual_return": m.annual_return,
        "max_drawdown": m.max_drawdown,
        "sharpe": m.sharpe,
        "final_equity": portfolio.final_equity,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="触发阈值网格搜索（top-k 完整回测）")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print(f"触发阈值网格：top-k {TOP_K_GRID}，名义{NOTIONAL_FRAC*100:.0f}%/标的，嵌套口径")
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
    print(f"[OK] 嵌套信号 {len(sig)} 条")

    close_parts = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]

    rows = []
    for top_k in TOP_K_GRID:
        r = run_once(cfg0, bars, features, prices, sig, top_k, args.n_jobs)
        rows.append(r)
        print(f"[top{top_k*100:.0f}%] 触发率={r['trigger_rate']*100:.1f}% 年化={r['annual_return']*100:+.2f}% "
              f"回撤={r['max_drawdown']*100:.2f}% Sharpe={r['sharpe']:.2f} 笔数={r['n_trades']}")

    print("\n[SUMMARY]")
    best = max(rows, key=lambda r: r["sharpe"])
    for r in rows:
        mark = " ◀ 最优" if r["top_k"] == best["top_k"] else ""
        print(f"  top{r['top_k']*100:3.0f}%: 年化 {r['annual_return']*100:+6.2f}% 回撤 {r['max_drawdown']*100:6.2f}% "
              f"Sharpe {r['sharpe']:.2f} 触发率 {r['trigger_rate']*100:5.1f}%{mark}")
    print(f"\n[判定] 最优 top-k = {best['top_k']*100:.0f}%（Sharpe {best['sharpe']:.2f}，年化 {best['annual_return']*100:.1f}%）")

    out = DELIVERABLE_DIR / f"topk-grid-backtest-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "best": best["top_k"], "notional_frac": NOTIONAL_FRAC},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
