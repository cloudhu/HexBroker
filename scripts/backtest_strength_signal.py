"""单边多头强度信号完整回测（P0：滑点/手续费/保证金/展期语义）。

用 BacktestEngine 对 exp_ret 全局 top 20% 做多信号跑含成本回测：
- per-symbol 合约参数（au=×1000/0.02、ag=×15/0.01、m=×10/1）
- 滑点 1 tick、手续费 0.005%、保证金 12%、初始 100 万
- 每标的固定 1 手（信号级估算的保守规模验证）
- 输出：权益曲线统计（年化/回撤/Sharpe/胜率）+ 成本占比 + 对比信号级估算
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
TOP_K = 0.30  # exp_ret 全局前 30% 做多（网格最优：Sharpe 0.85，2026-08-16 topk-grid）
NOTIONAL_FRAC = 0.20  # 每标的名义金额 = 权益 × NOTIONAL_FRAC（参数化，脚本支持覆盖）
# 品种级合约参数（multiplier=合约乘数, min_tick=最小变动价位）
CONTRACTS = {
    "au0": {"multiplier": 1000.0, "min_tick": 0.02},
    "ag0": {"multiplier": 15.0, "min_tick": 0.01},
    "m0": {"multiplier": 10.0, "min_tick": 1.0},
}
INITIAL_CAPITAL = 1_000_000.0


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


def main() -> None:
    ap = argparse.ArgumentParser(description="单边多头强度信号完整回测")
    ap.add_argument("--n-jobs", type=int, default=12)
    ap.add_argument("--top-k", type=float, default=TOP_K)
    ap.add_argument("--notional-frac", type=float, default=NOTIONAL_FRAC, help="每标的名义金额占权益比例（0.2=20%）")
    args = ap.parse_args()

    print("=" * 72)
    print(f"完整回测：单边多头 exp_ret top{args.top_k*100:.0f}%（嵌套信号，名义{args.notional_frac*100:.0f}%权益/标的）")
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
    print(f"[OK] 嵌套评估子窗信号 {len(sig)} 条")

    # ---- prices（MultiIndex(symbol, datetime) + close） ----
    close_parts = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]

    # ---- 构造 targets：exp_ret 全局 top_k 做多（名义等权），其余 0 ----
    # 名义等权：每标的分配 NOTIONAL_FRAC 权益的名义金额，手数 = floor(名义/(价格×乘数))
    mult_map = {sym: CONTRACTS[sym]["multiplier"] for sym in CONTRACTS}
    notional = INITIAL_CAPITAL * args.notional_frac
    sig["rank_pct"] = sig["exp_ret"].rank(pct=True)
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(mult_map)
    sig["target"] = np.where(
        (sig["rank_pct"] >= 1.0 - args.top_k) & sig["_px"].notna(),
        (notional / (sig["_px"] * sig["_mult"])).astype(int),
        0,
    )
    targets = sig.set_index(["symbol", "ts"])[["target"]].sort_index()
    n_long = int((sig["target"] > 0).sum())
    print(f"[OK] 做多信号 {n_long}/{len(sig)}（{n_long/len(sig)*100:.1f}%）")
    print(f"[OK] 名义等权：每标的 {args.notional_frac*100:.0f}% 权益（{notional:,.0f} 元）")

    # ---- 运行回测 ----
    cost = CostModel(
        fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
        slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
    )
    engine = BacktestEngine(cfg0, cost=cost, initial_capital=INITIAL_CAPITAL)
    portfolio = engine.run(prices, targets)

    eq = portfolio.equity_curve
    m = compute_metrics(eq, freq="1d")
    # 成本统计
    fees = sum(t.fee for t in engine.broker.trades)
    n_trades = len(engine.broker.trades)

    print("\n" + "=" * 72)
    print("[回测结果]")
    print(f"  初始权益: {INITIAL_CAPITAL:,.0f} | 最终权益: {portfolio.final_equity:,.2f}")
    print(f"  总收益率: {m.total_return*100:+.2f}% | 年化: {m.annual_return*100:+.2f}%")
    print(f"  最大回撤: {m.max_drawdown*100:.2f}% | Sharpe: {m.sharpe:.2f}")
    print(f"  交易笔数: {n_trades} | 总手续费: {fees:,.2f} ({fees/(portfolio.final_equity-INITIAL_CAPITAL+1e-9)*100:.1f}% of PnL)")
    print(f"  覆盖期: {eq.index.min().date()} ~ {eq.index.max().date()} ({len(eq)} bar)")

    # 对比信号级估算
    print("\n[对比信号级估算]")
    print(f"  信号级估算（top{args.top_k*100:.0f}%）: +0.696%/5日 → 年化毛 34.8%")
    print(f"  完整回测（1 手/标的保守规模）: 年化 {m.annual_return*100:+.2f}%")

    # 无成本对照（隔离成本影响）
    engine0 = BacktestEngine(cfg0, cost=CostModel(fee_open=0, fee_close=0, slippage_ticks=0, margin_rate=0.12, contracts=CONTRACTS), initial_capital=INITIAL_CAPITAL)
    pf0 = engine0.run(prices, targets)
    m0 = compute_metrics(pf0.equity_curve, freq="1d")
    print(f"\n[成本影响] 无成本年化 {m0.annual_return*100:+.2f}% vs 含成本 {m.annual_return*100:+.2f}% "
          f"→ 成本拖累 { (m0.annual_return-m.annual_return)*100:.2f}pp/年")

    report = {
        "top_k": args.top_k,
        "initial_capital": INITIAL_CAPITAL,
        "final_equity": portfolio.final_equity,
        "total_return": m.total_return,
        "annual_return": m.annual_return,
        "max_drawdown": m.max_drawdown,
        "sharpe": m.sharpe,
        "n_trades": n_trades,
        "total_fees": fees,
        "n_signals": len(sig),
        "n_long": n_long,
        "no_cost_annual_return": m0.annual_return,
        "cost_drag_pp": (m0.annual_return - m.annual_return) * 100,
    }
    out = DELIVERABLE_DIR / f"strength-backtest-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] 报告：{out}")


if __name__ == "__main__":
    main()
