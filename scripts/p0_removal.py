"""P0 p0 剔除实验：负 IC 品种去留对组合的影响。

假设：负 IC 品种（exp_ret 排序反指）在全局 top35% 中可能拖累组合。
对比（最优参数 0.8/0.2/top35%/W20-linear）：
  A 基线：18 品种全部
  B 剔 p0：17 品种（p0 IC -0.197 最差）
  C 剔全部负 IC：jm0/au0/ag0/cf0/sr0/rb0/p0（IC<0 的全部剔除）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.eval_signals18 import build_rolling_spread18, load_prices18, load_sig18_sym_close
from scripts.combo_validation import OOS_START

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS18,
)
NEG_IC = ["p0", "jm0", "au0", "ag0", "cf0", "sr0", "rb0"]  # IC<0 品种


def run(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series,
        label: str, top_k: float = 0.35) -> None:
    df = sig.copy()
    df["score"] = 0.8 * df["exp_ret"].rank(pct=True) + 0.2 * df["mom"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    px_missing = df["_px"].isna()
    qty = (0.30 * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    df["_scale"] = df["ts"].map(scale).fillna(1.0)
    df["target"] = np.where((df["score"] >= 1.0 - top_k) & ~px_missing,
                            (qty * df["_scale"]).astype(int), 0)
    tg = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = 1_000_000.0
    pf = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, tg)
    m = compute_metrics(pf.equity_curve, freq="1d")

    oos = df[df["ts"] >= OOS_START].copy()
    oos["_px"] = oos.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos["_mult"] = oos["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    op = oos["_px"].isna()
    oq = (0.30 * 1_000_000.0 / (oos["_px"] * oos["_mult"])).astype(int)
    oos["_scale"] = oos["ts"].map(scale).fillna(1.0)
    oos["target"] = np.where((oos["score"] >= 1.0 - top_k) & ~op,
                             (oq * oos["_scale"]).astype(int), 0)
    otg = oos.set_index(["symbol", "ts"])[["target"]].sort_index()
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, otg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")
    n_sym = df["symbol"].nunique()
    print(f"[{label}] 品种={n_sym} 全样本: 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} | OOS: 年化={mo.annual_return*100:+.2f}% 回撤={mo.max_drawdown*100:.2f}% "
          f"Sharpe={mo.sharpe:.2f} 持仓={int((df['target']>0).sum())}")


def main() -> None:
    print("=" * 72)
    print("P0 p0 剔除实验（负 IC 品种去留）")
    print("=" * 72)
    sig = pd.read_parquet("artifacts/signals_cache18_grouped.parquet")
    sig["ts"] = pd.to_datetime(sig["ts"])
    prices, _ = load_prices18()
    print(f"[OK] v1 分组信号 {len(sig)} 条")

    # mom90 + 监控
    closes = {sym: load_sig18_sym_close(sym) for sym in SYMBOLS18}
    close_df = pd.DataFrame(closes).sort_index()
    mom90 = close_df / close_df.shift(90) - 1.0
    sig["mom"] = sig.apply(
        lambda r: float(mom90.loc[r["ts"], r["symbol"]])
        if r["ts"] in mom90.index and r["symbol"] in mom90.columns else np.nan,
        axis=1,
    )
    spreads = build_rolling_spread18(sig, 20)
    scale = (spreads >= -0.003).astype(float).fillna(1.0).clip(0.0, 1.0)

    # A 基线 18
    run(sig, prices, scale, "A 基线18品种")
    # B 剔 p0
    sig_b = sig[sig["symbol"] != "p0"].copy()
    run(sig_b, prices, scale, "B 剔p0(17品种)")
    # C 剔全部负 IC
    sig_c = sig[~sig["symbol"].isin(NEG_IC)].copy()
    run(sig_c, prices, scale, f"C 剔负IC {len(NEG_IC)}个({18-len(NEG_IC)}品种)")
    # D 只保留最强正 IC（hc/m/i/j/ta/zn/y 等前 8）
    POS8 = ["hc0", "m0", "i0", "j0", "ta0", "zn0", "y0", "ni0"]
    sig_d = sig[sig["symbol"].isin(POS8)].copy()
    run(sig_d, prices, scale, "D 仅正IC top8")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/p0-removal-2026-08-18.md")


if __name__ == "__main__":
    main()
