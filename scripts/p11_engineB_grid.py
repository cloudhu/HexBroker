"""P11-1 引擎 B 参数复验网格（数据补齐后，P0）。

背景（P2-P10 定案）
------------------
- 引擎 B（基差收敛）：basis_ratio 品种内滚动分位 >= thr 做多。生产配置 win=252/thr=0.70
  （EngineBConfig 固化）。
- P2 网格基于旧数据（18 品种止于 2026-02-24、部分品种缺 2024/2022 段）：当时
  WINS=[126,252,504] × THRS=[0.5..0.9] 15 组合，15/15 OOS Sharpe 全正（0.40~1.34）。
- P6-4 已补齐数据（18 品种全到 2026-08-17，闭合率 100%）；P10 补测确认纯引擎 B
  OOS Sharpe=1.622 / OOS 复利=+31.05%（A0/B100，2024-07-18 后，复利口径）。

本脚本（数据补齐后复验）：
  网格 win∈{63,126,252,504} × thr∈{0.60,0.70,0.80}（12 组合；在 P2 网格基础上
  加 win=63 细网格、聚焦 0.6-0.8）。
  引擎 B 用 p3_combo_backtest.engine_b_targets（与生产同实现），BacktestEngine
  完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18+INITIAL_CAPITAL=1e6）。
  每组合输出（复利口径，compute_metrics）：
    全样本 Sharpe / IS(<=2022-04-21) Sharpe / OOS(>=2024-07-18) Sharpe /
    OOS 复利 / OOS MaxDD / 做多天数占比
  关键对比：生产配置 win252/thr0.70 在完整数据上的 OOS Sharpe（预期≈1.622）。
  落盘：artifacts/p11_engineB_grid.csv

零泄漏约束：本脚本不改任何配置默认；参数裁决只给建议（主理人定夺）。IS 段
(<=2022-04-21) 仅作稳健性参考——若某组合 OOS 显著更高但 IS 段也稳健，才提示候选。

用法：
  python scripts/p11_engineB_grid.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets

ART = ROOT / "artifacts"

# 复验网格：P2 网格 [126,252,504]×[0.5..0.9] 基础上加 win=63、聚焦 0.6-0.8
WINS = [63, 126, 252, 504]
THRS = [0.60, 0.70, 0.80]

# 零泄漏约束下允许的参数稳健性参考段（参数选定窗口，P2 定案时依据的 IS 边界）
IS_END = "2022-04-21"


def run_combo(cfg, cost, prices, win: int, thr: float) -> dict:
    """单组合：构建 targets → 完整回测 → 全样本/IS/OOS 指标（复利口径）。"""
    targets = engine_b_targets(prices, win, thr)
    n_long = int((targets["target"] > 0).sum())
    if n_long == 0:
        return {"win": win, "thr": thr, "n_long_days": 0, "note": "no_long_signal"}

    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    m = compute_metrics(eq, freq="daily")

    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None

    is_eq = eq[idx <= pd.Timestamp(IS_END)]
    m_is = compute_metrics(is_eq, freq="daily") if len(is_eq) > 30 else None

    # 做多天数占比（至少持有一个做多标的的日数 / 全部有信号日数）
    tgt = targets["target"]
    total_days = pd.Index(pd.to_datetime(targets.index.get_level_values(1).unique()))
    long_days = pd.Index(pd.to_datetime(tgt[tgt > 0].index.get_level_values(1).unique()))
    long_ratio = len(long_days) / len(total_days) if len(total_days) else float("nan")

    return {
        "win": win,
        "thr": thr,
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "is_sharpe": m_is.sharpe if m_is else np.nan,
        "is_ret": m_is.total_return if m_is else np.nan,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
        "n_long_days": n_long,
        "long_ratio": long_ratio,
    }


def main() -> None:
    t_start = time.time()
    print("=" * 100)
    print("P11-1 引擎 B 参数复验网格（数据补齐后）")
    print(f"网格 win∈{WINS} × thr∈{THRS}（{len(WINS) * len(THRS)} 组合）| "
          f"OOS 起点 {OOS_START} | IS 稳健性参考 <= {IS_END}")
    print(f"口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | INITIAL_CAPITAL={INITIAL_CAPITAL:,.0f} | "
          f"复利口径评估 | 生产配置 win252/thr0.70 锚点 OOS Sharpe≈1.622 (P10)")
    print("=" * 100)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种 | "
          f"数据范围 {prices.index.get_level_values(1).min()} ~ {prices.index.get_level_values(1).max()}")
    print()

    rows = []
    for win in WINS:
        for thr in THRS:
            r = run_combo(cfg, cost, prices, win, thr)
            rows.append(r)
            if r.get("note"):
                print(f"  win={win:>3} thr={thr:.2f}: {r['note']}")
                continue
            print(f"  win={win:>3} thr={thr:.2f}: 全样本Sharpe={r['sharpe_full']:.3f} "
                  f"IS<={IS_END[:10]} Sharpe={r['is_sharpe']:.3f} "
                  f"OOS Sharpe={r['oos_sharpe']:.3f} OOS复利={r['oos_ret']*100:+.2f}% "
                  f"OOS MaxDD={r['oos_maxdd']*100:.1f}% 做多天数={r['long_ratio']*100:.1f}%")

    df = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    df.to_csv(ART / "p11_engineB_grid.csv", index=False)
    print(f"\n[OK] 网格结果 → {ART / 'p11_engineB_grid.csv'}")

    # ---- 关键对比 ----
    print("\n" + "-" * 100)
    print("关键对比：生产配置 win252/thr0.70")
    prod = df[(df["win"] == 252) & (df["thr"] == 0.70)].iloc[0]
    print(f"  win252/thr0.70: 全样本 Sharpe={prod['sharpe_full']:.3f} | "
          f"OOS Sharpe={prod['oos_sharpe']:.3f} OOS 复利={prod['oos_ret']*100:+.2f}% "
          f"OOS MaxDD={prod['oos_maxdd']*100:.1f}% | 与 P10 锚点(1.622) "
          f"Δ={prod['oos_sharpe']-1.622:+.3f}")

    # ---- OOS 排名 ----
    print("\n按 OOS Sharpe 排名（复利口径）：")
    rank = df.sort_values("oos_sharpe", ascending=False).reset_index(drop=True)
    rank["oos_sharpe"] = rank["oos_sharpe"].map(lambda x: f"{x:.3f}")
    rank["oos_ret"] = rank["oos_ret"].map(lambda x: f"{x*100:+.2f}%")
    rank["oos_maxdd"] = rank["oos_maxdd"].map(lambda x: f"{x*100:.1f}%")
    rank["is_sharpe"] = rank["is_sharpe"].map(lambda x: f"{x:.3f}" if pd.notna(x) else "  n/a")
    rank["sharpe_full"] = rank["sharpe_full"].map(lambda x: f"{x:.3f}")
    print(rank[["win", "thr", "sharpe_full", "is_sharpe", "oos_sharpe", "oos_ret",
                "oos_maxdd"]].to_string(index=False))

    # ---- 裁决建议 ----
    print("\n" + "-" * 100)
    print("裁决建议（是否维持 win252/thr0.70）")
    dfr = pd.DataFrame(rows)
    prod_rank = int((dfr["oos_sharpe"] > prod["oos_sharpe"]).sum()) + 1
    print(f"  生产配置 OOS Sharpe 排名: {prod_rank}/{len(dfr)}")
    better = dfr[dfr["oos_sharpe"] > prod["oos_sharpe"] + 0.05].sort_values(
        "oos_sharpe", ascending=False)
    if better.empty:
        print("  无组合显著超越生产配置（ΔOOS Sharpe>0.05 为 0 个）→ 维持 win252/thr0.70")
    else:
        print(f"  OOS Sharpe 显著超越生产配置（Δ>0.05）的组合 {len(better)} 个：")
        for _, r in better.iterrows():
            is_ok = "IS稳健" if (pd.notna(r["is_sharpe"]) and r["is_sharpe"] > 0.5) else "IS一般"
            print(f"    win={int(r['win'])} thr={r['thr']:.2f}: "
                  f"OOS Sharpe={r['oos_sharpe']:.3f} (Δ={r['oos_sharpe']-prod['oos_sharpe']:+.3f}) "
                  f"OOS 复利={r['oos_ret']*100:+.2f}% IS Sharpe={r['is_sharpe']:.3f} [{is_ok}]")
    print("  注：本脚本仅给数据裁决建议；是否更新 EngineBConfig 由主理人定（脚本不改配置默认）。")

    print("\n" + "=" * 100)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s")
    print("=" * 100)


if __name__ == "__main__":
    main()
