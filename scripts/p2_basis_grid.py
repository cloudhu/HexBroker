"""P2 引擎 B 参数敏感性网格：win × thr → 全样本 + OOS + 交易胜率。

用法：
  python scripts/p2_basis_grid.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.engine import BacktestEngine
from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices, load_basis_panel

WINS = [126, 252, 504]
THRS = [0.5, 0.6, 0.7, 0.8, 0.9]
NOTIONAL_FRAC = 0.20
OOS_START = "2024-07-18"


def build_targets(prices: pd.DataFrame, win: int, thr: float) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


def trade_win_rate(targets: pd.DataFrame, prices: pd.DataFrame) -> float:
    tgt = targets["target"].sort_index()
    tgt_long = (tgt > 0).astype(int)
    stats = []
    cur_sym, entry_px = None, None
    for (sym, ts), is_long in tgt_long.items():
        if is_long and entry_px is None:
            cur_sym, entry_dt = sym, ts
            entry_px = prices.xs(sym, level=0)["close"].get(ts)
        elif not is_long and entry_px is not None:
            exit_px = prices.xs(cur_sym, level=0)["close"].get(ts)
            if entry_px and exit_px:
                stats.append(exit_px / entry_px - 1)
            entry_px = None
    if entry_px is not None and cur_sym:
        stats.append(prices.xs(cur_sym, level=0)["close"].iloc[-1] / entry_px - 1)
    if not stats:
        return 0.0
    return float(np.mean(np.array(stats) > 0))


def main() -> None:
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    eq_idx_all = pd.to_datetime(prices.index.get_level_values(1).unique())

    rows = []
    for win in WINS:
        for thr in THRS:
            targets = build_targets(prices, win, thr)
            if (targets["target"] > 0).sum() == 0:
                continue
            engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
            pf = engine.run(prices, targets)
            eq = pf.equity_curve
            m = compute_metrics(eq, freq="daily")
            # OOS
            idx = pd.to_datetime(eq.index)
            oos = eq[idx >= OOS_START]
            m_oos = compute_metrics(oos, freq="daily") if len(oos) > 30 else None
            twr = trade_win_rate(targets, prices)
            rows.append({
                "win": win, "thr": thr,
                "sharpe": m.sharpe, "ann_ret": m.annual_return, "maxdd": m.max_drawdown,
                "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
                "oos_ann": m_oos.annual_return if m_oos else np.nan,
                "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
                "trade_win": twr, "n_long_days": int((targets["target"] > 0).sum()),
            })
            oos_sh = m_oos.sharpe if m_oos else float("nan")
            print(f"  win={win:>3} thr={thr:.1f}: Sharpe={m.sharpe:.3f} OOS_Sharpe={oos_sh:.3f} "
                  f"MaxDD={m.max_drawdown*100:.1f}% 交易胜率={twr*100:.1f}%")

    df = pd.DataFrame(rows)
    df.to_csv("artifacts/p2_basis_grid.csv", index=False)
    print("\n[OK] 网格结果 → artifacts/p2_basis_grid.csv")
    print("\n== 按 OOS Sharpe 排序 top8 ==")
    print(df.sort_values("oos_sharpe", ascending=False).head(8).to_string(index=False))


if __name__ == "__main__":
    main()
