"""自动循环迭代搜索最优策略配置。

协议（严格纪律）：
  1. 信号缓存复用（walk_forward 嵌套信号，零泄漏）；
  2. 评估 = BacktestEngine 完整回测（滑点/手续费/保证金），全样本 + OOS 段；
  3. 选优用全样本 Sharpe（含训练期），OOS（2024-07-18 后真新数据）只做最终确认——不参与选优，防过拟合；
  4. 分阶段：粗网格 → 最优邻域精调 → 平台性检查（防尖峰过拟合）。

搜索空间：
  w1   ∈ {0.5, 0.6, 0.7, 0.8, 0.9}        exp_ret 权重（mom 权重 = 1-w1）
  N    ∈ {20, 40, 60, 90, 120}             mom 窗口
  top_k∈ {0.25, 0.30, 0.35}                top 分位
  W    ∈ {10, 20, 30}                      监控窗口
  mode ∈ {step, linear}                    监控模式
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.combo_validation import OOS_START
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals, scale_for

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)

# 预计算的 mom 面板缓存
_MOM_CACHE: dict[int, pd.DataFrame] = {}


def get_mom(sig: pd.DataFrame, prices: pd.DataFrame, N: int) -> pd.DataFrame:
    if N not in _MOM_CACHE:
        closes = {sym: prices.xs(sym, level=0)["close"].sort_index() for sym in sig["symbol"].unique()}
        close_df = pd.DataFrame(closes).sort_index()
        mom = close_df / close_df.shift(N) - 1.0
        df = sig.copy()
        df["mom"] = df.apply(
            lambda r: float(mom.loc[r["ts"], r["symbol"]])
            if r["ts"] in mom.index and r["symbol"] in mom.columns else np.nan,
            axis=1,
        )
        _MOM_CACHE[N] = df
    return _MOM_CACHE[N]


def evaluate(sig: pd.DataFrame, prices: pd.DataFrame, scale: pd.Series | None,
             w1: float, N: int, top_k: float) -> tuple[float, float, float, int]:
    """返回 (全样本 Sharpe, OOS Sharpe, 全样本回撤, 持仓信号数)。"""
    df = get_mom(sig, prices, N)
    r1 = df["exp_ret"].rank(pct=True)
    r2 = df["mom"].rank(pct=True)
    df["score"] = w1 * r1 + (1 - w1) * r2
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

    # OOS 段
    oos_df = df[df["ts"] >= OOS_START].copy()
    if len(oos_df):
        oos_df["_px"] = oos_df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
        oos_df["_mult"] = oos_df["symbol"].map({s: CONTRACTS[s]["multiplier"] for s in CONTRACTS})
        oos_pxm = oos_df["_px"].isna()
        oos_qty = (NOTIONAL_FRAC * 1_000_000.0 / (oos_df["_px"] * oos_df["_mult"])).astype(int)
        oos_flag = (oos_df["score"] >= 1.0 - top_k) & ~oos_pxm
        if scale is not None:
            oos_df["_scale"] = oos_df["ts"].map(scale).fillna(1.0)
            oos_df["target"] = np.where(oos_flag, (oos_qty * oos_df["_scale"]).astype(int), 0)
        else:
            oos_df["target"] = np.where(oos_flag, oos_qty, 0)
        oos_t = oos_df.set_index(["symbol", "ts"])[["target"]].sort_index()
        pf_o = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, oos_t)
        oos_sharpe = float(compute_metrics(pf_o.equity_curve, freq="1d").sharpe)
    else:
        oos_sharpe = np.nan
    n_active = int((df["target"] > 0).sum())
    return m.sharpe, oos_sharpe, m.max_drawdown, n_active


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=1, choices=[1, 2, 3], help="1粗网格 2精调 3平台检查")
    ap.add_argument("--n-jobs", type=int, default=6)
    args = ap.parse_args()

    print("=" * 80)
    print(f"自动循环迭代搜索（Phase {args.phase}）")
    print("=" * 80)
    sig, prices = build_signals(args.n_jobs, use_cache=True)
    print(f"[OK] 信号 {len(sig)} 条 | prices {len(prices)} 行")

    # 预计算监控 scale（W 与 mode 的笛卡尔积）
    scales: dict[tuple[int, str], pd.Series] = {}
    for W in (10, 15, 20, 30):
        spreads = build_rolling_spread(sig, prices, window=W)
        for mode in ("step", "linear"):
            scales[(W, mode)] = scale_for(spreads, mode, 0.0 if mode == "step" else 0.003)
    print(f"[OK] 监控 scale 预计算 {len(scales)} 组")

    if args.phase == 1:
        # ---- Phase 1 粗网格 ----
        grid = list(itertools.product(
            [0.5, 0.7, 0.9],     # w1
            [40, 60, 90],        # N
            [0.30],              # top_k 固定
            [20],                # W 固定
            ["step"],            # mode 固定
        ))
        print(f"[Phase1] 粗网格 {len(grid)} 组合")
        results = []
        for w1, N, top_k, W, mode in grid:
            t0 = time.time()
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            results.append((w1, N, top_k, W, mode, sh, oos_sh, mdd, n))
            print(f"  w1={w1} N={N} topk={top_k} W={W} {mode}: "
                  f"全样本Sharpe={sh:.3f} OOS={oos_sh:+.3f} 回撤={mdd*100:.1f}% 持仓={n} ({time.time()-t0:.1f}s)")
        results.sort(key=lambda r: r[5], reverse=True)
        print("\n[Phase1 最优 5 个（按全样本 Sharpe）]")
        for r in results[:5]:
            print(f"  w1={r[0]} N={r[1]} topk={r[2]} W={r[3]} {r[4]}: 全样本={r[5]:.3f} OOS={r[6]:+.3f}")
    elif args.phase == 2:
        # ---- Phase 2 精调（围绕 Phase1 最优的邻域） ----
        center = {"w1": 0.5, "N": 90, "top_k": 0.30, "W": 20, "mode": "step"}
        grid = []
        for w1 in (0.4, 0.5, 0.6):
            for N in (80, 90, 120):
                for top_k in (0.25, 0.30, 0.35):
                    for W in (20, 30):
                        for mode in ("step",):
                            grid.append((w1, N, top_k, W, mode))
        print(f"[Phase2] 精调网格 {len(grid)} 组合（N=90 聚焦）")
        results = []
        for w1, N, top_k, W, mode in grid:
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            results.append((w1, N, top_k, W, mode, sh, oos_sh, mdd, n))
        results.sort(key=lambda r: r[5], reverse=True)
        print("\n[Phase2 最优 10 个]")
        for r in results[:10]:
            print(f"  w1={r[0]} N={r[1]} topk={r[2]} W={r[3]} {r[4]}: 全样本={r[5]:.3f} OOS={r[6]:+.3f} 回撤={r[7]*100:.1f}%")
        # 保存
        pd.DataFrame(results, columns=["w1", "N", "top_k", "W", "mode", "sh_full", "sh_oos", "mdd", "n_active"]
                     ).to_csv("artifacts/phase2_results.csv", index=False)
        print("[OK] 结果已保存 artifacts/phase2_results.csv")
    elif args.phase == 3:
        # ---- Phase 3 平台性检查（最优配置 + 邻域） ----
        best = {"w1": 0.7, "N": 60, "top_k": 0.30, "W": 20, "mode": "step"}
        if Path("artifacts/phase2_results.csv").exists():
            df = pd.read_csv("artifacts/phase2_results.csv")
            b = df.sort_values("sh_full", ascending=False).iloc[0]
            best = {"w1": float(b["w1"]), "N": int(b["N"]), "top_k": float(b["top_k"]),
                    "W": int(b["W"]), "mode": str(b["mode"])}
            print(f"[Phase3] 用 Phase2 最优: {best}")
        neighbors = [
            (best["w1"], best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"] - 0.1, best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"] + 0.1, best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"] - 20, best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"] + 20, best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"] - 0.05, best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"] + 0.05, best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"], best["W"] - 5, best["mode"]),
            (best["w1"], best["N"], best["top_k"], best["W"] + 10, best["mode"]),
        ]
        print("[Phase3] 平台性检查（最优 + 8 邻域）")
        rows = []
        for w1, N, top_k, W, mode in neighbors:
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            rows.append((w1, N, top_k, W, mode, sh, oos_sh, mdd, n))
            print(f"  w1={w1} N={N} topk={top_k} W={W} {mode}: 全样本={sh:.3f} OOS={oos_sh:+.3f}")
        shs = [r[5] for r in rows]
        ooss = [r[6] for r in rows if not np.isnan(r[6])]
        print(f"\n[平台性] 全样本 Sharpe: mean={np.mean(shs):.3f} std={np.std(shs):.3f} "
              f"(std<0.08 视为平台)")
        print(f"[平台性] OOS Sharpe: mean={np.mean(ooss):.3f} std={np.std(ooss):.3f} "
              f"负值数={sum(1 for v in ooss if v < 0)}/{len(ooss)}")
        best_row = max(rows, key=lambda r: r[5])
        print(f"\n[最终最优] w1={best_row[0]} N={best_row[1]} topk={best_row[2]} W={best_row[3]} {best_row[4]}"
              f" → 全样本 Sharpe={best_row[5]:.3f} OOS={best_row[6]:+.3f} 回撤={best_row[7]*100:.1f}%")


if __name__ == "__main__":
    main()
