"""P1-2 板块分层：贵金属/黑色/有色/农产品差异化。

动机：OOS 段贵金属牛市（ag0 +1.16%/5日）vs 黑色熊市（rb0 -0.51%/5日）方向相反——
板块动量状态应差异化处理，避免用贵金属的动量逻辑交易黑色品种。

板块定义：
  贵金属 precious: au0, ag0
  黑色 ferrous:    rb0, i0
  有色 industrial: cu0
  农产品 agri:     m0

实验（基线 = 自动迭代最优：0.6exp + 0.4mom120, top25%, W20 step 监控）：
  S0 基线
  S1 板块动量过滤：板块平均 mom120 < 0 → 该板块品种不做多
  S2 板块动量三信号：score = w1·exp_ret + w2·mom_ind + w3·mom_sector（板块动量入分）
  S3 板块动量过滤 + 板块动量入分（组合）

诊断先行：各板块 mom120 的 OOS 段分布与后续收益关系。
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
from scripts.auto_iterate_search import get_mom
from scripts.combo_validation import OOS_START
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals, scale_for

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)

SECTORS = {
    "precious": ["au0", "ag0"],
    "ferrous": ["rb0", "i0"],
    "industrial": ["cu0"],
    "agri": ["m0"],
}
SYM2SECTOR = {s: sec for sec, syms in SECTORS.items() for s in syms}


def add_sector_mom(df: pd.DataFrame) -> pd.DataFrame:
    """给信号行附加板块动量 mom_sec（组内品种 mom120 按日截面均值，严格 ≤t）。"""
    out = df.copy()
    # 按 (ts, sector) 均值
    out["sector"] = out["symbol"].map(SYM2SECTOR)
    sec_mom = out.groupby(["ts", "sector"])["mom"].mean().rename("mom_sec")
    out = out.merge(sec_mom.reset_index(), on=["ts", "sector"], how="left")
    return out


def run_variant(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
                variant: str, w1: float = 0.6, w2: float = 0.4,
                top_k: float = 0.25, label: str = "") -> None:
    df = sig.copy()
    if "mom_sec" not in df.columns:
        df = add_sector_mom(df)

    if variant == "S0":
        df["score"] = w1 * df["exp_ret"].rank(pct=True) + w2 * df["mom"].rank(pct=True)
        df["_block"] = False
    elif variant == "S1":
        df["score"] = w1 * df["exp_ret"].rank(pct=True) + w2 * df["mom"].rank(pct=True)
        df["_block"] = df["mom_sec"] < 0  # 板块动量转负 → 板块内不做多
    elif variant == "S2":
        # 三信号：exp_ret + 品种动量 + 板块动量（板块动量权重独立）
        w3 = 0.2
        w1_, w2_ = 0.5, 0.3
        df["score"] = (w1_ * df["exp_ret"].rank(pct=True) + w2_ * df["mom"].rank(pct=True)
                       + w3 * df["mom_sec"].rank(pct=True))
        df["_block"] = False
    elif variant == "S3":
        w3 = 0.2
        w1_, w2_ = 0.5, 0.3
        df["score"] = (w1_ * df["exp_ret"].rank(pct=True) + w2_ * df["mom"].rank(pct=True)
                       + w3 * df["mom_sec"].rank(pct=True))
        df["_block"] = df["mom_sec"] < 0

    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["score"] >= 1.0 - top_k) & ~px_missing & ~df["_block"]
    if scale is not None:
        df["_scale"] = df["ts"].map(scale).fillna(1.0)
        df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    else:
        df["target"] = np.where(long_flag, qty, 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")
    n_active = int((df["target"] > 0).sum())
    print(f"[{label}] 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} 持仓信号={n_active}")


def main() -> None:
    print("=" * 72)
    print("P1-2 板块分层（基线 = 自动迭代最优 0.6exp+0.4mom120）")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    sig = get_mom(sig, prices, 120)  # 品种动量
    sig = add_sector_mom(sig)        # 板块动量
    print(f"[OK] 信号 {len(sig)} 条，板块映射 {SYM2SECTOR}")

    spreads = build_rolling_spread(sig, prices, window=20)
    scale = scale_for(spreads, "step", 0.0)

    # ---- 诊断：各板块 OOS 段动量状态与后续收益 ----
    oos = sig[sig["ts"] >= OOS_START].copy()
    print("\n[诊断] OOS 段各板块 mom120 状态（严格 ≤t）")
    for sec, syms in SECTORS.items():
        sub = oos[oos["symbol"].isin(syms)]
        if len(sub):
            print(f"  {sec:12s} {syms}: n={len(sub):3d} 板块动量中位={sub['mom_sec'].median()*100:+.2f}% "
                  f"负值占比={(sub['mom_sec']<0).mean()*100:.0f}% | "
                  f"品种动量中位={sub['mom'].median()*100:+.2f}%")

    print("\n=== 全样本（2018~2026-08） ===")
    run_variant(sig, prices, scale, "S0", label="S0 基线（最优配置）")
    run_variant(sig, prices, scale, "S1", label="S1 板块动量过滤")
    run_variant(sig, prices, scale, "S2", label="S2 板块动量入分")
    run_variant(sig, prices, scale, "S3", label="S3 过滤+入分")

    print("\n=== OOS 段（2024-07-18~2026-06，真新数据） ===")
    oos2 = oos.copy()
    run_variant(oos2, prices, scale, "S0", label="S0 基线")
    run_variant(oos2, prices, scale, "S1", label="S1 板块动量过滤")
    run_variant(oos2, prices, scale, "S2", label="S2 板块动量入分")
    run_variant(oos2, prices, scale, "S3", label="S3 过滤+入分")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/sector-layering-2026-08-17.md")


if __name__ == "__main__":
    main()
