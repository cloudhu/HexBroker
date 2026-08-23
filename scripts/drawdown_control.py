"""P2 回撤控制：最优配置（v1 分组 17 品种 + 0.8exp/0.2mom90 + top35% + W20-linear）叠加风控。

变体：
  A  基线（无额外风控）
  B1/B2/B3  逐仓位止损：入场后价格回撤 -5%/-8%/-10% → 次日 target=0（严格因果）
  C1/C2/C3  组合回撤熔断：等权组合指数峰值回撤 -8%/-12%/-15% → 空仓 10 交易日
  D1/D2     波动率目标：20 日滚动波动，目标年化 10%/15% → scale 缩放

全样本 + OOS（2024-07-18 后留出），BacktestEngine 完整口径。
2026-08-18 执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.combo_validation import OOS_START
from scripts.eval_signals18 import build_rolling_spread18, load_prices18

CACHE = "artifacts/signals_cache18_grouped.parquet"  # v1 等价（18 品种）
DROP_P0 = [s for s in SYMBOLS18 if s != "p0"]
W1, W2, TOP_K, NOTIONAL = 0.8, 0.2, 0.35, 0.30
INIT_CAP = 1_000_000.0

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS18,
)


def load_signals() -> pd.DataFrame:
    """全 18 品种信号（spread 计算需全截面，与 p0_removal 口径一致）。"""
    sig = pd.read_parquet(CACHE)
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig.copy()


def base_targets(sig: pd.DataFrame, prices: pd.DataFrame, close_df: pd.DataFrame) -> pd.DataFrame:
    """基础 target：0.8·rank(exp_ret)+0.2·rank(mom90) top35% + W20-linear 监控。

    监控 spread 用全 18 品种截面（与 p0_removal 一致），targets 剔 p0（17 品种）。
    """
    mom90 = close_df / close_df.shift(90) - 1.0
    sig = sig.copy()
    sig["mom"] = sig.apply(
        lambda r: float(mom90.loc[r["ts"], r["symbol"]])
        if r["ts"] in mom90.index and r["symbol"] in mom90.columns else np.nan, axis=1,
    )
    sig["score"] = W1 * sig["exp_ret"].rank(pct=True) + W2 * sig["mom"].rank(pct=True)
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1,
    )
    sig["_mult"] = sig["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    pxm = sig["_px"].isna()
    qty = (NOTIONAL * INIT_CAP / (sig["_px"] * sig["_mult"])).astype(int)
    spreads = build_rolling_spread18(sig, 20)  # 全 18 品种截面
    scale = (spreads >= -0.003).astype(float).fillna(1.0).clip(0.0, 1.0)
    sig["_scale"] = sig["ts"].map(scale).fillna(1.0)
    sig["target"] = np.where((sig["score"] >= 1.0 - TOP_K) & ~pxm, (qty * sig["_scale"]).astype(int), 0)
    # 剔 p0（17 品种）
    sig = sig[sig["symbol"] != "p0"]
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


def apply_stop_loss(targets: pd.DataFrame, closes: dict[str, pd.Series], stop_pct: float) -> pd.DataFrame:
    """逐仓位止损：持仓后价格相对入场回撤超 stop_pct → 次日 target=0（严格因果）。"""
    tg = targets["target"].copy()
    dates = sorted(tg.index.get_level_values(1).unique())
    for sym, cs in closes.items():
        if sym not in tg.index.get_level_values(0):
            continue
        sym_idx = tg.index.get_level_values(0) == sym
        sym_dates = [d for d in dates if (sym, d) in tg.index]
        entry: float | None = None
        for d in sym_dates:
            cur_target = int(tg.loc[(sym, d)])
            if entry is not None and d in cs.index:
                px = float(cs.loc[d])
                if px / entry - 1.0 < -abs(stop_pct):
                    tg.loc[(sym, d)] = 0  # 当日 target=0 → 次日卖出
                    entry = None
                    continue
            if cur_target > 0 and entry is None and d in cs.index:
                entry = float(cs.loc[d])
            if cur_target == 0:
                entry = None
    return pd.DataFrame({"target": tg}).sort_index()


def apply_circuit_breaker(targets: pd.DataFrame, closes: dict[str, pd.Series], dd_pct: float, cooloff: int = 10) -> pd.DataFrame:
    """组合回撤熔断：等权组合指数峰值回撤超 dd_pct → 之后 cooloff 交易日空仓。"""
    all_closes = pd.DataFrame(closes).sort_index()
    port_ret = all_closes.pct_change().mean(axis=1).fillna(0.0)
    eq = (1.0 + port_ret).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1.0
    # 触发点（严格因果：用当日及以前数据判定 → 次日生效）
    trigger = (dd < -abs(dd_pct)).astype(int)
    cooldown = 0
    halt = pd.Series(0, index=eq.index)
    for i in range(len(eq)):
        if cooldown > 0:
            halt.iloc[i] = 1
            cooldown -= 1
        elif trigger.iloc[i]:
            halt.iloc[i] = 1
            cooldown = cooloff
    tg = targets["target"].copy()
    for (sym, d) in tg.index:
        if d in halt.index and halt.loc[d]:
            tg.loc[(sym, d)] = 0
    return pd.DataFrame({"target": tg}).sort_index()


def apply_vol_target(targets: pd.DataFrame, closes: dict[str, pd.Series], target_vol: float) -> pd.DataFrame:
    """波动率目标：组合 20 日滚动年化波动 → scale = clip(target/realized, 0, 1.5)。"""
    all_closes = pd.DataFrame(closes).sort_index()
    ret = all_closes.pct_change()
    roll_vol = ret.mean(axis=1).rolling(20, min_periods=20).std() * np.sqrt(252)
    scale = (target_vol / roll_vol).clip(0.0, 1.5).fillna(1.0)
    tg = targets["target"].copy()
    for (sym, d) in tg.index:
        if d in scale.index and pd.notna(scale.loc[d]):
            tg.loc[(sym, d)] = int(tg.loc[(sym, d)] * scale.loc[d])
    return pd.DataFrame({"target": tg}).sort_index()


def run_bt(targets: pd.DataFrame, prices: pd.DataFrame, label: str) -> None:
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INIT_CAP
    eng = BacktestEngine(cfg, cost=COST, initial_capital=INIT_CAP)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")

    oos_tg = targets[targets.index.get_level_values(1) >= OOS_START]
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=INIT_CAP).run(prices, oos_tg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")
    n_pos = int((targets["target"] > 0).sum())
    print(
        f"[{label:6s}] 全样本: 年化={m.annual_return*100:+6.2f}% 回撤={m.max_drawdown*100:6.2f}% "
        f"Sharpe={m.sharpe:.2f} | OOS: 年化={mo.annual_return*100:+5.2f}% 回撤={mo.max_drawdown*100:5.2f}% "
        f"Sharpe={mo.sharpe:.2f} | 持仓={n_pos}"
    )


def main() -> None:
    print("=" * 96)
    print(f"P2 回撤控制：v1 分组(剔p0 17品种) + {W1}exp/{W2}mom90 + top{TOP_K*100:.0f}% + W20-linear 监控，名义 {NOTIONAL*100:.0f}%")
    print("=" * 96)
    sig = load_signals()  # 全 18 品种（spread 用全截面）
    prices, close_by = load_prices18()
    closes = {sym: close_by[sym] for sym in SYMBOLS18}
    base = base_targets(sig, prices, pd.DataFrame(closes))  # targets 剔 p0 → 17 品种
    print(f"[OK] 基础 target {len(base)} 条（剔 p0 17 品种）")

    run_bt(base, prices, "A基线")

    # 止损/熔断/波动率目标在 17 品种 targets 上叠加
    closes17 = {sym: close_by[sym] for sym in DROP_P0}

    for stop in (0.05, 0.08, 0.10):
        tg = apply_stop_loss(base.copy(), closes, stop)
        run_bt(tg, prices, f"B止损{stop*100:.0f}%")

    for dd in (0.08, 0.12, 0.15):
        tg = apply_circuit_breaker(base.copy(), closes, dd)
        run_bt(tg, prices, f"C熔断{dd*100:.0f}%")

    for tv in (0.10, 0.15):
        tg = apply_vol_target(base.copy(), closes, tv)
        run_bt(tg, prices, f"D波目{tv*100:.0f}%")

    # 组合：止损8% + 熔断12%
    tg = apply_stop_loss(base.copy(), closes, 0.08)
    tg = apply_circuit_breaker(tg, closes, 0.12)
    run_bt(tg, prices, "E组合")


if __name__ == "__main__":
    main()
