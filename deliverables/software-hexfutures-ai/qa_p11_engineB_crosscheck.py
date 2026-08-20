"""QA P11-1 独立交叉验证：不调用 p11 脚本函数，独立复算引擎 B OOS Sharpe。

口径（与生产/P10/P11 一致）：
  - engine_b_targets（p3 生产实现，win/thr 参数化）构建 target
  - BacktestEngine 完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18+1e6）
  - 复利口径：OOS 段(>=2024-07-18)权益曲线的日收益 → Sharpe
  - 独立实现 OOS Sharpe / OOS 复利 / OOS MaxDD（不调用 compute_metrics，
    直接用 numpy 计算，验证指标口径本身）

用法：
  python scripts/qa_p11_engineB_crosscheck.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets

OOS_START_TS = pd.Timestamp(OOS_START)


def independent_sharpe(eq: pd.Series) -> dict:
    """独立指标：复利口径（权益曲线日收益）。"""
    idx = pd.to_datetime(eq.index)
    oos = eq[idx >= OOS_START_TS]
    rets = oos.pct_change().dropna()
    n = len(rets)
    mean_r = rets.mean()
    std_r = rets.std(ddof=1)
    sharpe = mean_r / std_r * np.sqrt(252) if std_r > 1e-12 else 0.0
    total_ret = float(oos.iloc[-1] / oos.iloc[0] - 1.0)
    peak = oos.cummax()
    maxdd = float((oos / peak - 1.0).min())
    return {
        "oos_n": int(n),
        "oos_sharpe": sharpe,
        "oos_ret": total_ret,
        "oos_maxdd": maxdd,
        "oos_start_eq": float(oos.iloc[0]),
        "oos_end_eq": float(oos.iloc[-1]),
    }


def main() -> None:
    print("=" * 100)
    print("QA P11-1 独立交叉验证（不调用 p11 函数；engine_b_targets + 独立指标）")
    print("=" * 100)
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种 | "
          f"{prices.index.get_level_values(1).min()} ~ {prices.index.get_level_values(1).max()}")

    combos = [(252, 0.70), (252, 0.60), (63, 0.70), (504, 0.80)]
    rows = []
    for win, thr in combos:
        targets = engine_b_targets(prices, win, thr)
        n_long = int((targets["target"] > 0).sum())
        engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
        pf = engine.run(prices, targets)
        eq = pf.equity_curve
        r = independent_sharpe(eq)
        r.update({"win": win, "thr": thr, "n_long_days": n_long})
        rows.append(r)
        print(f"  win={win:>3} thr={thr:.2f}: OOS Sharpe={r['oos_sharpe']:.4f} "
              f"OOS 复利={r['oos_ret']*100:+.2f}% OOS MaxDD={r['oos_maxdd']*100:.2f}% "
              f"OOS n={r['oos_n']} 做多天数={n_long}")

    print("\n" + "-" * 100)
    print("与 p11_engineB_grid.csv 对比（期望 OOS 段逐位一致）:")
    grid = pd.read_csv(ROOT / "artifacts" / "p11_engineB_grid.csv")
    for r in rows:
        g = grid[(grid["win"] == r["win"]) & (grid["thr"] == r["thr"])].iloc[0]
        d_sh = r["oos_sharpe"] - g["oos_sharpe"]
        d_ret = r["oos_ret"] - g["oos_ret"]
        d_dd = r["oos_maxdd"] - g["oos_maxdd"]
        ok = abs(d_sh) < 1e-9 and abs(d_ret) < 1e-9 and abs(d_dd) < 1e-9
        print(f"  win={r['win']} thr={r['thr']}: ΔSharpe={d_sh:+.4f} ΔRet={d_ret:+.6f} "
              f"ΔMaxDD={d_dd:+.6f} → {'MATCH' if ok else 'MISMATCH'}")
        if not ok:
            print(f"    QA: {r['oos_sharpe']:.6f}/{r['oos_ret']:.6f}/{r['oos_maxdd']:.6f} | "
                  f"grid: {g['oos_sharpe']:.6f}/{g['oos_ret']:.6f}/{g['oos_maxdd']:.6f}")

    print("\n" + "=" * 100)
    print("[DONE]")
    print("=" * 100)


if __name__ == "__main__":
    main()
