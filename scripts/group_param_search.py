"""分组信号三阶段参数搜索（P0：分组×参数）。

信号：signals_cache18_grouped.parquet（分组建模，7952 条）
搜索空间：w1 ∈ {0.4,0.5,0.6,0.7,0.8} × N ∈ {40,60,90,120} × topk ∈ {0.25,0.3,0.35}
        × W ∈ {10,20,30} × mode ∈ {step,linear}
纪律：选优用全样本 Sharpe（含训练期），OOS（2024-07-18 后）完全留出只确认。
Phase1 粗网格 → Phase2 精调 → Phase3 平台性检查（防尖峰过拟合）。
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
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.eval_signals18 import build_rolling_spread18, load_prices18, load_sig18_sym_close
from scripts.combo_validation import OOS_START

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS18,
)
_MOM_CACHE: dict[int, pd.DataFrame] = {}


def get_sig_with_mom(sig: pd.DataFrame, N: int) -> pd.DataFrame:
    if N not in _MOM_CACHE:
        closes = {sym: load_sig18_sym_close(sym) for sym in SYMBOLS18}
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
    df = get_sig_with_mom(sig, N)
    df["score"] = w1 * df["exp_ret"].rank(pct=True) + (1 - w1) * df["mom"].rank(pct=True)
    df["_px"] = df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    df["_mult"] = df["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    px_missing = df["_px"].isna()
    qty = (0.30 * 1_000_000.0 / (df["_px"] * df["_mult"])).astype(int)
    long_flag = (df["score"] >= 1.0 - top_k) & ~px_missing
    if scale is not None:
        df["_scale"] = df["ts"].map(scale).fillna(1.0)
        df["target"] = np.where(long_flag, (qty * df["_scale"]).astype(int), 0)
    else:
        df["target"] = np.where(long_flag, qty, 0)
    targets = df.set_index(["symbol", "ts"])[["target"]].sort_index()

    from hexbroker.config import load_config
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = 1_000_000.0
    eng = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0)
    pf = eng.run(prices, targets)
    m = compute_metrics(pf.equity_curve, freq="1d")

    oos_df = df[df["ts"] >= OOS_START].copy()
    oos_df["_px"] = oos_df.apply(lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_df["_mult"] = oos_df["symbol"].map({s: CONTRACTS18[s]["multiplier"] for s in CONTRACTS18})
    op = oos_df["_px"].isna()
    oq = (0.30 * 1_000_000.0 / (oos_df["_px"] * oos_df["_mult"])).astype(int)
    oflag = (oos_df["score"] >= 1.0 - top_k) & ~op
    if scale is not None:
        oos_df["_scale"] = oos_df["ts"].map(scale).fillna(1.0)
        oos_df["target"] = np.where(oflag, (oq * oos_df["_scale"]).astype(int), 0)
    else:
        oos_df["target"] = np.where(oflag, oq, 0)
    otg = oos_df.set_index(["symbol", "ts"])[["target"]].sort_index()
    pfo = BacktestEngine(cfg, cost=COST, initial_capital=1_000_000.0).run(prices, otg)
    mo = compute_metrics(pfo.equity_curve, freq="1d")
    n_active = int((df["target"] > 0).sum())
    return m.sharpe, mo.sharpe, m.max_drawdown, n_active


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=1, choices=[1, 2, 3])
    args = ap.parse_args()

    print("=" * 80)
    print(f"分组信号参数搜索（Phase {args.phase}）")
    print("=" * 80)
    sig = pd.read_parquet("artifacts/signals_cache18_grouped.parquet")
    sig["ts"] = pd.to_datetime(sig["ts"])
    prices, _ = load_prices18()
    print(f"[OK] 分组信号 {len(sig)} 条 | prices {len(prices)} 行")

    # 监控 scale 预计算
    scales: dict[tuple[int, str], pd.Series] = {}
    for W in (10, 15, 20, 30):
        spreads = build_rolling_spread18(sig, W)
        for mode in ("step", "linear"):
            scales[(W, mode)] = ((spreads >= (0.0 if mode == "step" else -0.003))
                                  .astype(float).fillna(1.0).clip(0.0, 1.0))
    print(f"[OK] 监控 scale 预计算 {len(scales)} 组")

    if args.phase == 1:
        grid = list(itertools.product([0.4, 0.5, 0.6, 0.7, 0.8], [40, 60, 90, 120], [0.30], [20], ["step"]))
        print(f"[Phase1] 粗网格 {len(grid)} 组合")
        results = []
        for w1, N, top_k, W, mode in grid:
            t0 = time.time()
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            results.append((w1, N, top_k, W, mode, sh, oos_sh, mdd, n))
            print(f"  w1={w1} N={N} topk={top_k} W={W} {mode}: 全样本={sh:.3f} OOS={oos_sh:+.3f} "
                  f"回撤={mdd*100:.1f}% 持仓={n} ({time.time()-t0:.1f}s)")
        results.sort(key=lambda r: r[5], reverse=True)
        print("\n[Phase1 最优 5]")
        for r in results[:5]:
            print(f"  w1={r[0]} N={r[1]}: 全样本={r[5]:.3f} OOS={r[6]:+.3f}")
    elif args.phase == 2:
        # 以 Phase1 最优方向（需人工确认后调整）为中心的邻域
        center = {"w1": 0.8, "N": 120, "top_k": 0.30, "W": 20, "mode": "step"}
        grid = []
        for w1 in (0.7, 0.8, 0.9):
            for N in (60, 90, 120, 150):
                for top_k in (0.25, 0.30, 0.35):
                    for W in (15, 20, 30):
                        for mode in ("step", "linear"):
                            grid.append((w1, N, top_k, W, mode))
        print(f"[Phase2] 精调网格 {len(grid)} 组合")
        results = []
        for w1, N, top_k, W, mode in grid:
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            results.append((w1, N, top_k, W, mode, sh, oos_sh, mdd, n))
        results.sort(key=lambda r: r[5], reverse=True)
        print("\n[Phase2 最优 10]")
        for r in results[:10]:
            print(f"  w1={r[0]} N={r[1]} topk={r[2]} W={r[3]} {r[4]}: 全样本={r[5]:.3f} "
                  f"OOS={r[6]:+.3f} 回撤={r[7]*100:.1f}%")
        pd.DataFrame(results, columns=["w1", "N", "top_k", "W", "mode", "sh_full", "sh_oos", "mdd", "n_active"]
                     ).to_csv("artifacts/group_phase2.csv", index=False)
        print("[OK] 保存 artifacts/group_phase2.csv")
    elif args.phase == 3:
        best = {"w1": 0.8, "N": 90, "top_k": 0.35, "W": 20, "mode": "linear"}
        if Path("artifacts/group_phase2.csv").exists():
            df = pd.read_csv("artifacts/group_phase2.csv")
            b = df.sort_values("sh_full", ascending=False).iloc[0]
            best = {"w1": float(b["w1"]), "N": int(b["N"]), "top_k": float(b["top_k"]),
                    "W": int(b["W"]), "mode": str(b["mode"])}
            print(f"[Phase3] 用 Phase2 最优: {best}")
        neighbors = [
            (best["w1"], best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"] - 0.1, best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"] + 0.1, best["N"], best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"] - 30, best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"] + 30, best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"] - 60, best["top_k"], best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"] - 0.05, best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"] + 0.05, best["W"], best["mode"]),
            (best["w1"], best["N"], best["top_k"], best["W"] - 5, best["mode"]),
            (best["w1"], best["N"], best["top_k"], best["W"] + 10, best["mode"]),
        ]
        print("[Phase3] 平台性检查（最优 + 8 邻域）")
        rows = []
        for w1, N, top_k, W, mode in neighbors:
            sh, oos_sh, mdd, n = evaluate(sig, prices, scales[(W, mode)], w1, N, top_k)
            rows.append((w1, N, top_k, W, mode, sh, oos_sh, mdd))
            print(f"  w1={w1} N={N} topk={top_k} W={W} {mode}: 全样本={sh:.3f} OOS={oos_sh:+.3f}")
        shs = [r[5] for r in rows]
        ooss = [r[6] for r in rows]
        print(f"\n[平台性] 全样本 mean={np.mean(shs):.3f} std={np.std(shs):.3f} | "
              f"OOS mean={np.mean(ooss):.3f} std={np.std(ooss):.3f} 负值数={sum(1 for v in ooss if v<0)}/{len(ooss)}")
        best_row = max(rows, key=lambda r: r[5])
        print(f"\n[最终最优] w1={best_row[0]} N={best_row[1]} topk={best_row[2]} W={best_row[3]} {best_row[4]}"
              f" → 全样本={best_row[5]:.3f} OOS={best_row[6]:+.3f} 回撤={best_row[7]*100:.1f}%")


if __name__ == "__main__":
    main()
