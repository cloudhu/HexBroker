"""P4-1 组合权重精调：趋势引擎A + 基差引擎B 权重网格 × 波动率目标叠加。

背景
----
P3 已验证 A30/B70 组合 OOS Sharpe 0.859。P4-1 在固定引擎口径下做组合层精调：
  - 权重网格：A∈{0.25,0.30,0.35,0.40}（B=1-A）
  - 交叉：是否叠加组合层波动率目标（EWMA halflife=10，target 17.5% 年化，
    scale=clip(target/vol, 0, 1.5)，vol 用 shift(1) 避免前视）
  - 每个配置：engine_a_targets(top30%) + engine_b_targets(win252/thr0.7)
    各跑一次完整回测 → 日收益序列 → 加权组合 → 全样本 Sharpe + OOS(>=2024-07-18) Sharpe + OOS MaxDD
  - 选 OOS Sharpe 最优配置，与 P3 基线 A30/B70 对比

口径（铁律）：滑点 1tick + 手续费 0.005% + 保证金 12% + 18 品种合约参数。

用法：
  python scripts/p4_combo_tune.py
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
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import engine_a_targets, engine_b_targets, run_engine

BASIS_WIN = 252
BASIS_THR = 0.70
OOS_START = pd.Timestamp("2024-07-18")
W_A_GRID = [0.25, 0.30, 0.35, 0.40]
VOL_TARGET = 0.175          # 17.5% 年化
VOL_HALFLIFE = 10
VOL_SCALE_CAP = 1.5
ART = ROOT / "artifacts"


def combo_stats_row(ret_a: pd.Series, ret_b: pd.Series, w_a: float, vol_target: bool) -> dict:
    """加权组合日收益 → 权益 → 全样本 + OOS 指标（一行）。"""
    comb = w_a * ret_a + (1.0 - w_a) * ret_b
    if vol_target:
        # EWMA 波动率（halflife=10），shift(1) 只用 t-1 信息避免前视
        vol = comb.ewm(halflife=VOL_HALFLIFE, adjust=False).std().shift(1)
        scale = (VOL_TARGET / (vol * np.sqrt(252))).clip(lower=0.0, upper=VOL_SCALE_CAP)
        scale = scale.fillna(1.0)  # 预热期不缩放
        comb = comb * scale
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "w_a": w_a,
        "w_b": round(1.0 - w_a, 4),
        "vol_target": vol_target,
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
    }


def main() -> None:
    print("=" * 96)
    print("P4-1 组合权重精调：A∈{0.25,0.30,0.35,0.40} × 波动率目标叠加(EWMA hl=10, target 17.5%)")
    print("=" * 96)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[OK] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种\n")

    print("--- 单引擎回测（完整口径：滑点1tick + 手续费0.005% + 保证金12%） ---")
    tgt_a = engine_a_targets(prices)
    ret_a = run_engine(cfg, cost, prices, tgt_a, "引擎A 趋势(top30%)")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine(cfg, cost, prices, tgt_b, "引擎B 基差(win252/thr0.7)")

    # 对齐共同交易日
    common = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[common].sort_index(), ret_b.loc[common].sort_index()
    print(f"\n共同交易日: {len(common)} | 日收益相关性: {ra.corr(rb):.3f}")

    # 网格
    rows = []
    for w_a in W_A_GRID:
        for vt in (False, True):
            rows.append(combo_stats_row(ra, rb, w_a, vt))
    tbl = pd.DataFrame(rows)
    tbl["is_p3_baseline"] = (tbl["w_a"] == 0.30) & (~tbl["vol_target"])

    print("\n--- 组合网格结果 ---")
    show = tbl.copy()
    show["vol_target"] = show["vol_target"].map({True: "Y", False: "N"})
    show["sharpe_full"] = show["sharpe_full"].map(lambda v: f"{v:.3f}")
    show["oos_sharpe"] = show["oos_sharpe"].map(lambda v: f"{v:.3f}")
    show["oos_maxdd"] = show["oos_maxdd"].map(lambda v: f"{v*100:.1f}%")
    show["oos_ret"] = show["oos_ret"].map(lambda v: f"{v*100:+.1f}%")
    show["ann_ret_full"] = show["ann_ret_full"].map(lambda v: f"{v*100:+.1f}%")
    show["maxdd_full"] = show["maxdd_full"].map(lambda v: f"{v*100:.1f}%")
    show["mark"] = np.where(tbl["is_p3_baseline"], "<- P3基线A30/B70", "")
    print(show[["w_a", "w_b", "vol_target", "sharpe_full", "ann_ret_full", "maxdd_full",
                "oos_sharpe", "oos_maxdd", "oos_ret", "mark"]].to_string(index=False))

    # 保存
    ART.mkdir(exist_ok=True)
    out = tbl.drop(columns=["is_p3_baseline"])
    out.to_csv(ART / "p4_combo_tune.csv", index=False)
    print(f"\n[OK] 结果 → {ART / 'p4_combo_tune.csv'}")

    # 最优配置 vs P3 基线
    best = tbl.loc[tbl["oos_sharpe"].idxmax()]
    base = tbl[tbl["is_p3_baseline"]].iloc[0]
    print("\n--- 判定：OOS Sharpe 最优 vs P3 基线 A30/B70 ---")
    print(f"  P3 基线 A30/B70        : OOS Sharpe={base['oos_sharpe']:.3f} "
          f"OOS MaxDD={base['oos_maxdd']*100:.1f}% 全样本 Sharpe={base['sharpe_full']:.3f}")
    print(f"  最优配置 A{best['w_a']:.2f}/B{best['w_b']:.2f} "
          f"vol_target={'Y' if best['vol_target'] else 'N'}: "
          f"OOS Sharpe={best['oos_sharpe']:.3f} OOS MaxDD={best['oos_maxdd']*100:.1f}% "
          f"全样本 Sharpe={best['sharpe_full']:.3f}")
    delta = best["oos_sharpe"] - base["oos_sharpe"]
    verdict = "PASS(优于基线)" if delta > 0 else "NEUTRAL(不优于基线)"
    print(f"  OOS Sharpe 增量: {delta:+.3f} → {verdict}")
    print(f"  结论: 推荐 {'叠加' if best['vol_target'] else '不叠加'}波动率目标，"
          f"权重 A={best['w_a']:.2f} / B={best['w_b']:.2f}")


if __name__ == "__main__":
    main()
