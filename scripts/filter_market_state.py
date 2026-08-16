"""市场状态过滤器实验（P1，2026-08-16）。

基线：单边多头 exp_ret top-30%（网格最优，Sharpe 0.85）。测试市场状态过滤器能否
规避失效期（raw 诊断：2021/2024 方向维度负贡献）。

状态指标（全部严格因果，只用 ≤t 的 close 历史）：
- 趋势过滤：close < MA20（20 日）→ 空仓（不做多）；参数化 ma_window
- 波动过滤：20 日波动率 > 样本 70 分位 → 空仓（高波动期信号噪声大）
- 组合过滤：趋势差 OR 波动高 → 空仓

对比：无过滤（基线）vs 三过滤变体，完整回测口径（名义 100%/标的）。
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
MA_WINDOW = 20
VOL_WINDOW = 20
VOL_PCT = 0.70  # 波动率样本 70 分位为高波动阈值
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


def _state_series(close: pd.Series) -> pd.DataFrame:
    """计算品种状态序列（严格因果：rolling 不含当期以外的未来）。"""
    ma = close.rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    vol = close.pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std()
    return pd.DataFrame({"close": close, "ma": ma, "vol": vol})


def _trend_ok(sym: str, ts, close_by_sym: dict) -> bool:
    """close >= MA20（用 ≤ts 历史，严格因果）。"""
    st = close_by_sym[sym].loc[:ts]  # Series（close 值）
    if len(st) < MA_WINDOW:
        return False
    ma = st.rolling(MA_WINDOW, min_periods=MA_WINDOW).mean().iloc[-1]
    return st.iloc[-1] >= ma


def _vol_ok(sym: str, ts, close_by_sym: dict, vol_thr: dict) -> bool:
    """20 日波动率 <= 样本 70 分位阈值。"""
    st = close_by_sym[sym].loc[:ts]
    if len(st) < VOL_WINDOW:
        return False
    v = st.pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std().iloc[-1]
    return float(v) <= vol_thr[sym]


def make_targets(sig: pd.DataFrame, prices: pd.DataFrame, close_by_sym: dict,
                 filter_mode: str, vol_thr: dict[str, float] | None) -> pd.DataFrame:
    """构造 targets：top_k 做多 × 状态过滤（filter_mode ∈ none/trend/vol/combo）。"""
    mult_map = {sym: CONTRACTS[sym]["multiplier"] for sym in CONTRACTS}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    df = sig.copy()
    df["rank_pct"] = df["exp_ret"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map(mult_map)
    # 状态判定
    df["_ok"] = True
    if filter_mode in ("trend", "combo"):
        df["_trend_ok"] = df.apply(lambda r: _trend_ok(r["symbol"], r["ts"], close_by_sym), axis=1)
        df["_ok"] &= df["_trend_ok"]
    if filter_mode in ("vol", "combo"):
        df["_vol_ok"] = df.apply(lambda r: _vol_ok(r["symbol"], r["ts"], close_by_sym, vol_thr), axis=1)
        df["_ok"] &= df["_vol_ok"]
    df["target"] = np.where(
        (df["rank_pct"] >= 1.0 - TOP_K) & df["_px"].notna() & df["_ok"],
        (notional / (df["_px"] * df["_mult"])).astype(int),
        0,
    )
    return df.set_index(["symbol", "ts"])[["target"]].sort_index()


def run_bt(cfg0, prices, targets) -> dict:
    cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                     slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS)
    engine = BacktestEngine(cfg0, cost=cost, initial_capital=INITIAL_CAPITAL)
    portfolio = engine.run(prices, targets)
    m = compute_metrics(portfolio.equity_curve, freq="1d")
    return {
        "annual_return": m.annual_return,
        "max_drawdown": m.max_drawdown,
        "sharpe": m.sharpe,
        "total_return": m.total_return,
        "final_equity": portfolio.final_equity,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="市场状态过滤器实验")
    ap.add_argument("--n-jobs", type=int, default=12)
    args = ap.parse_args()

    print("=" * 72)
    print(f"市场状态过滤器实验（top-30% 基线，名义{NOTIONAL_FRAC*100:.0f}%/标的，嵌套口径）")
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

    # 波动率阈值：用全样本 70 分位（探索用；严格应训练窗内 rolling，标注风险）
    vol_thr = {}
    for sym in bars.symbols:
        vol = close_by_sym[sym].pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std()
        vol_thr[sym] = float(vol.quantile(VOL_PCT))
        print(f"[OK] {sym} 波动率 {VOL_PCT*100:.0f} 分位阈值 = {vol_thr[sym]*100:.3f}%/日")

    variants = [
        ("none（基线 top-30%）", "none"),
        ("trend 过滤（close<MA20 空仓）", "trend"),
        ("vol 过滤（波动>70 分位空仓）", "vol"),
        ("combo（趋势 OR 波动差→空仓）", "combo"),
    ]
    rows = []
    for label, mode in variants:
        targets = make_targets(sig, prices, close_by_sym, mode, vol_thr)
        r = run_bt(cfg0, prices, targets)
        n_long = int((targets["target"] > 0).sum())
        r.update({"label": label, "mode": mode, "trigger_rate": n_long / len(sig), "n_long": n_long})
        rows.append(r)
        print(f"[{label}] 触发率={r['trigger_rate']*100:.1f}% 年化={r['annual_return']*100:+.2f}% "
              f"回撤={r['max_drawdown']*100:.2f}% Sharpe={r['sharpe']:.2f}")

    print("\n[SUMMARY]")
    base = rows[0]
    for r in rows:
        d_sharpe = r["sharpe"] - base["sharpe"]
        d_ret = (r["annual_return"] - base["annual_return"]) * 100
        print(f"  {r['label']:<28s} 年化 {r['annual_return']*100:+6.2f}% (Δ{d_ret:+5.2f}pp) "
              f"Sharpe {r['sharpe']:.2f} (Δ{d_sharpe:+.2f}) 回撤 {r['max_drawdown']*100:6.2f}%")

    out = DELIVERABLE_DIR / f"market-state-filter-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "top_k": TOP_K, "ma_window": MA_WINDOW, "vol_pct": VOL_PCT},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] 报告：{out}")


if __name__ == "__main__":
    main()
