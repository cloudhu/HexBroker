"""P2 引擎 B 回测：基差收敛策略（basis 均值回归）完整 BacktestEngine 口径。

策略逻辑（Sentinel-2 基本面引擎）：
  - 因子：品种内 basis_ratio 滚动分位（backwardation 升水 → 期货向现货收敛 → 上涨）
  - 做多：basis_ratio 分位 >= 阈值（自身历史高位，现货升水）
  - 空仓：分位 < 阈值
  - 名义等权：每标的名义金额 = 权益 × NOTIONAL_FRAC

口径与主引擎一致：滑点 1tick + 手续费 0.005% + 保证金 12% + 18 品种合约参数。

用法：
  python scripts/p2_basis_backtest.py [--win 252] [--thr 0.7] [--notional-frac 0.2]
"""
from __future__ import annotations

import argparse
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

INITIAL_CAPITAL = 1_000_000.0
FUND_DIR = Path("data/raw/fundamental")


def load_prices() -> pd.DataFrame:
    """18 品种 K 线 → MultiIndex(symbol, datetime) + close。"""
    import glob
    parts = []
    for sym in SYMBOLS18:
        for fp in sorted(glob.glob(f"data/raw/processed/{sym}/1d/*.parquet")):
            df = pd.read_parquet(fp)
            df["datetime"] = pd.to_datetime(df["datetime"])
            parts.append(df[["symbol", "datetime", "close"]])
    prices = pd.concat(parts, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    return prices


def load_basis_panel() -> pd.DataFrame:
    """基差面板 → MultiIndex(symbol, datetime) + basis_ratio。"""
    rows = []
    for sym in SYMBOLS18:
        sym_u = sym[:-1].upper()  # rb0 → RB
        f = FUND_DIR / f"basis_{sym_u}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df["datetime"] = pd.to_datetime(df["date"])
        df["symbol"] = sym
        rows.append(df[["symbol", "datetime", "basis_ratio", "basis", "spot_price"]])
    panel = pd.concat(rows, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
    return panel


def build_targets(prices: pd.DataFrame, win: int, thr: float, notional_frac: float) -> pd.DataFrame:
    """滚动分位信号 → target（名义等权手数）。"""
    basis = load_basis_panel()
    # 品种内滚动分位
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    # 信号：分位 >= thr → 做多
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]  # 对齐价格
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * notional_frac
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    targets = sig[["target"]].sort_index()
    return targets


def main() -> None:
    ap = argparse.ArgumentParser(description="引擎 B 基差收敛策略回测")
    ap.add_argument("--win", type=int, default=252, help="滚动分位窗口")
    ap.add_argument("--thr", type=float, default=0.70, help="做多分位阈值")
    ap.add_argument("--notional-frac", type=float, default=0.20)
    args = ap.parse_args()

    print("=" * 80)
    print(f"引擎 B 回测：基差收敛 | 滚动分位 win={args.win} thr={args.thr} "
          f"名义={args.notional_frac*100:.0f}%权益/标的")
    print("=" * 80)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)

    prices = load_prices()
    print(f"[OK] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种")

    targets = build_targets(prices, args.win, args.thr, args.notional_frac)
    n_long = int((targets["target"] > 0).sum())
    print(f"[OK] targets: {len(targets)} 行, 做多天数 {n_long} ({n_long/len(targets)*100:.1f}%)")
    if n_long == 0:
        print("[FAIL] 无做多信号")
        return

    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve  # Series: 索引=时间戳, 值=权益
    metrics = compute_metrics(eq, freq="daily")
    print()
    print("== 引擎 B 回测结果（全样本） ==")
    print(f"  总收益: {metrics.total_return*100:+.2f}%")
    print(f"  年化收益: {metrics.annual_return*100:+.2f}%")
    print(f"  年化波动: {metrics.volatility*100:+.2f}%")
    print(f"  Sharpe: {metrics.sharpe:.3f}")
    print(f"  MaxDD: {metrics.max_drawdown*100:.2f}%")
    print(f"  胜率(日): {metrics.win_rate*100:.2f}%")
    print(f"  最终权益: {pf.final_equity:,.0f}")

    # OOS 分段（2024-07-18 后，PandaData 真新数据）
    eq_idx = pd.to_datetime(eq.index)
    oos_eq = eq[eq_idx >= "2024-07-18"]
    if len(oos_eq) > 30:
        m_oos = compute_metrics(oos_eq, freq="daily")
        print()
        print(f"== OOS 分段（2024-07-18 后，{len(oos_eq)} bars） ==")
        print(f"  区间收益: {(oos_eq.iloc[-1]/oos_eq.iloc[0]-1)*100:+.2f}%")
        print(f"  年化收益: {m_oos.annual_return*100:+.2f}%")
        print(f"  Sharpe: {m_oos.sharpe:.3f}")
        print(f"  MaxDD: {m_oos.max_drawdown*100:.2f}%")

    # 交易级胜率：连续持仓期（target>0 的连续段）的区间收益
    tgt = targets["target"].sort_index()
    tgt_long = (tgt > 0).astype(int)
    trade_stats = []
    cur_sym = None
    entry_px = None
    entry_dt = None
    for (sym, ts), is_long in tgt_long.items():
        if is_long and entry_px is None:
            cur_sym, entry_dt = sym, ts
            entry_px = prices.xs(sym, level=0)["close"].get(ts)
        elif not is_long and entry_px is not None:
            exit_px = prices.xs(cur_sym, level=0)["close"].get(ts)
            if entry_px and exit_px:
                trade_stats.append(exit_px / entry_px - 1)
            entry_px = None
    if entry_px is not None:
        trade_stats.append(prices.xs(cur_sym, level=0)["close"].iloc[-1] / entry_px - 1)
    if trade_stats:
        arr = np.array(trade_stats)
        print()
        print(f"== 交易级统计（{len(arr)} 笔） ==")
        print(f"  胜率(交易): {np.mean(arr > 0)*100:.2f}%")
        print(f"  平均收益: {arr.mean()*100:+.2f}%")
        print(f"  盈亏比(均盈/均亏): {abs(arr[arr>0].mean()/arr[arr<=0].mean()):.2f}")

    # 保存结果
    out = Path("artifacts")
    out.mkdir(exist_ok=True)
    eq.to_frame(name="equity").to_parquet(out / f"engineB_eq_win{args.win}_thr{args.thr:.2f}.parquet")
    targets.to_parquet(out / f"engineB_targets_win{args.win}_thr{args.thr:.2f}.parquet")
    print(f"\n[OK] 结果 → artifacts/engineB_*_win{args.win}_thr{args.thr:.2f}.parquet")


if __name__ == "__main__":
    main()
