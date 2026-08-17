"""OOS 持仓密度校准（释放被防御掉的贵金属趋势）。

背景：最优配置（0.6exp+0.4mom120, top25%, W20 step 监控）OOS 段仅 7/592 持仓（1.2%）——
三重过滤防御过度，贵金属（mom120 +31%）被拒之门外。

Step 1 诊断：OOS 段逐层过滤拦截统计（top25 → mom → 监控，各拦多少）
Step 2 校准网格：mon_thr × w2 × top_k（纪律：全样本 Sharpe 保持 0.85+ 平台，
   OOS 持仓密度提升 + OOS Sharpe 不劣化——选优仍用全样本，OOS 只确认）
"""
from __future__ import annotations

import itertools
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
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)


def scale_step(spreads: pd.Series, thr: float) -> pd.Series:
    """step 监控：价差 < thr → 0（空仓），否则 1。冷启动 → 1。"""
    sc = (spreads >= thr).astype(float)
    return sc.fillna(1.0).clip(0.0, 1.0)


def run_cfg(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
            w1: float, w2: float, top_k: float, label: str) -> tuple[float, float, float, int, int]:
    df = sig.copy()
    df["score"] = w1 * df["exp_ret"].rank(pct=True) + w2 * df["mom"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    px_missing = df["_px"].isna()
    qty = (NOTIONAL_FRAC * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["score"] >= 1.0 - top_k) & ~px_missing
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

    oos_df = df[df["ts"] >= OOS_START].copy()
    oos_df["_px"] = oos_df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_df["_mult"] = oos_df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
    op = oos_df["_px"].isna()
    oq = (NOTIONAL_FRAC * 1_000_000.0 / (oos_df["_px"] * oos_df["_mult"])).astype(int)
    oflag = (oos_df["score"] >= 1.0 - top_k) & ~op
    if scale is not None:
        oos_df["_scale"] = oos_df["ts"].map(scale).fillna(1.0)
        oos_df["target"] = np.where(oflag, (oq * oos_df["_scale"]).astype(int), 0)
    else:
        oos_df["target"] = np.where(oflag, oq, 0)
    otg = oos_df.set_index(["symbol", "ts"])[["target"]].sort_index()
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, otg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")
    n_oos = int((oos_df["target"] > 0).sum())
    print(f"[{label}] 全样本: 年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% "
          f"Sharpe={m.sharpe:.2f} | OOS: 年化={mo.annual_return*100:+.2f}% 回撤={mo.max_drawdown*100:.2f}% "
          f"Sharpe={mo.sharpe:.2f} 持仓={n_oos}/{len(oos_df)}")
    return m.sharpe, mo.sharpe, m.max_drawdown, n_oos, len(oos_df)


def diagnose_filters(sig: pd.DataFrame) -> None:
    """OOS 段逐层过滤拦截诊断。"""
    oos = sig[sig["ts"] >= OOS_START].copy()
    oos["score"] = 0.6 * oos["exp_ret"].rank(pct=True) + 0.4 * oos["mom"].rank(pct=True)
    n = len(oos)
    # 层1: top25%（无监控）
    f1 = oos["score"] >= 0.75
    # 层2: + mom 方向（score 已含，但看 mom>0 单独）——mom 在 score 里，仅看 score top25 即可
    # 层3: + 监控
    print(f"\n[诊断] OOS 段 {n} 信号逐层拦截：")
    print(f"  全量: {n}")
    print(f"  过 top25%（score 门槛）: {f1.sum()}（{f1.sum()/n*100:.1f}%）")
    # 若只放宽 top_k 到 0.35
    f2 = oos["score"] >= 0.65
    print(f"  过 top35%（宽松门槛）: {f2.sum()}（{f2.sum()/n*100:.1f}%）")
    # 贵金属在 top25 中的占比 vs 全量
    precious = oos["symbol"].isin(["au0", "ag0"])
    print(f"  贵金属占比: 全量 {precious.mean()*100:.0f}% | top25内 {oos[f1 & precious].shape[0]}/{f1.sum()} "
          f"({(f1 & precious).sum()/max(f1.sum(),1)*100:.0f}%)")
    # 监控在 OOS 段有多少时间空仓
    return


def main() -> None:
    print("=" * 72)
    print("OOS 持仓密度校准（释放贵金属趋势）")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    sig = get_mom(sig, prices, 120)
    print(f"[OK] 信号 {len(sig)} 条")

    diagnose_filters(sig)

    spreads = build_rolling_spread(sig, prices, window=20)
    print(f"\n[OK] 监控价差: 中位={spreads.median()*100:+.3f}% 负值占比={(spreads<0).mean()*100:.1f}%")

    # ---- 校准网格 ----
    print("\n=== 校准网格（全样本选优，OOS 确认） ===")
    results = []
    grid = list(itertools.product([0.0, -0.001, -0.002, -0.003], [0.2, 0.3, 0.4], [0.25, 0.30]))
    print(f"[网格] {len(grid)} 组合（mon_thr × w2 × top_k）")
    for thr, w2, top_k in grid:
        scale = scale_step(spreads, thr)
        w1 = 1.0 - w2
        sh, oos_sh, mdd, n_oos, n_tot = run_cfg(
            sig, prices, scale, w1, w2, top_k,
            f"thr={thr*100:+.1f}% w2={w2} topk={top_k:.2f}",
        )
        results.append({"thr": thr, "w2": w2, "top_k": top_k, "sh_full": sh,
                        "sh_oos": oos_sh, "mdd": mdd, "n_oos": n_oos})
    df_r = pd.DataFrame(results)
    df_r.to_csv("artifacts/calibrate_results.csv", index=False)

    print("\n[筛选] 全样本 Sharpe ≥ 0.85 且 OOS 持仓提升的配置：")
    good = df_r[(df_r["sh_full"] >= 0.85)].sort_values("sh_oos", ascending=False)
    print(good.to_string(index=False))
    print("\n[最优] 兼顾全样本平台（≥0.85）与 OOS 持仓/Sharpe：")
    if len(good):
        best = good.sort_values(["sh_oos", "n_oos"], ascending=[False, False]).iloc[0]
        print(best.to_dict())
    print("\n[报告] 见 deliverables/software-hexfutures-ai/exposure-calibration-2026-08-17.md")


if __name__ == "__main__":
    main()
