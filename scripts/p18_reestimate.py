"""P18-2 历史回测重估：broker.py multiplier bug 修复后，重跑 P16/P11/P9/P5 关键指标。

背景
----
P18-P0 修复了 ``SimBroker`` 全局 multiplier=10 记账 bug（PnL 改用品种级乘数）。
P18-2 用**修复后 broker** 重估受影响的历史报告指标，并与修复前（P16/P11/P9/P5
既有 artifacts 记录）对比，评估生产决策（引擎 B 为主、A10/B90）是否受影响。

重估口径（与各报告原始口径一致）：
  - P16 组合：引擎 A v8 + group_cap=0.5（ferrous_all 合并映射，显式传参）+
    引擎 B win252/thr0.7 + A10/B90 + volN/volY（复利口径，OOS 2024-07-18 后）
  - P9 组合网格：引擎 A v8（默认无 cap 解析）+ 引擎 B × 权重网格
    A∈{0.10..0.35} × vol on/off（12 配置）
  - P11 引擎 B 网格：win∈{63,126,252,504} × thr∈{0.60,0.70,0.80}（12 组合）
  - P5 引擎 A 基线：v8 S2（min=3，默认无 cap 解析）→ OOS Sharpe（旧 -0.337）

全部：滑点1tick + 费0.005% + 保证金12% + CONTRACTS18 + INITIAL_CAPITAL=1e6；
评估函数复用 p5/p9/p11 既有实现（run_engine_row / combo_stats_row / run_combo），
保证与修复前报告同构、仅 broker 记账不同。

输出：artifacts/p18_reestimation.csv（修复前后各报告关键指标对比表）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START, engine_b_targets
from scripts.p5_engineA_cross_section import (
    TOP_K,
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
)
from scripts.p11_engineB_grid import THRS, WINS, run_combo

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"

# P10-1/P16 生产 group_map（与 configs/base.yaml 一致：黑色系 5 品种合并 ferrous_all）
BASE_GROUP_MAP: dict[str, str] = {
    "i0": "ferrous_all", "j0": "ferrous_all", "jm0": "ferrous_all",
    "rb0": "ferrous_all", "hc0": "ferrous_all",
    "cu0": "industrial", "al0": "industrial", "zn0": "industrial", "ni0": "industrial",
    "au0": "precious", "ag0": "precious",
    "y0": "agri_oil", "p0": "agri_oil",
    "m0": "agri_protein",
    "sr0": "agri_soft", "cf0": "agri_soft",
    "ta0": "chem_energy", "sc0": "chem_energy",
}

W_A_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
VOL_GRID = [False, True]


def _m_dict(m, prefix: str = "") -> dict:
    return {
        f"{prefix}oos_sharpe": float(m.sharpe) if m is not None else np.nan,
        f"{prefix}oos_ret": float(m.total_return) if m is not None else np.nan,
        f"{prefix}oos_maxdd": float(m.max_drawdown) if m is not None else np.nan,
    }


def load_old_p9_grid() -> pd.DataFrame:
    fp = ART / "p9_combo_grid.csv"
    if fp.exists():
        return pd.read_csv(fp)
    return pd.DataFrame()


def load_old_p11_grid() -> pd.DataFrame:
    fp = ART / "p11_engineB_grid.csv"
    if fp.exists():
        return pd.read_csv(fp)
    return pd.DataFrame()


def load_old_p5() -> pd.DataFrame:
    fp = ART / "p5_engineA_strategy_compare.csv"
    if fp.exists():
        return pd.read_csv(fp)
    return pd.DataFrame()


def main() -> None:
    print("=" * 100)
    print("P18-2 历史回测重估（broker.py multiplier bug 修复后）")
    print("=" * 100)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 | OOS 起点 {OOS_START} | v8 缓存存在={V8_PATH.exists()}")

    rows: list[dict] = []

    # ------------------------------------------------------------------
    # P5 引擎 A S2 基线（v8, min=3；P5 年代默认无 cap）
    # ------------------------------------------------------------------
    print("\n[1] P5 引擎 A S2 基线 ...")
    tgt_a_s2 = engine_a_targets_cs(prices, TOP_K, 3, cache_path=V8_PATH)
    ret_a_s2, _eq, m_a_s2, m_oos_a_s2, lr_a = run_engine_row(cfg, cost, prices, tgt_a_s2, "A-S2")
    print(f"  引擎 A S2（无 cap, v8+fixed）: 全样本 Sharpe={m_a_s2.sharpe:.3f} | "
          f"OOS Sharpe={m_oos_a_s2.sharpe:.3f} OOS 复利={m_oos_a_s2.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos_a_s2.max_drawdown*100:.1f}%")
    old_p5 = load_old_p5()
    old_a_s2 = old_p5[old_p5["strategy"] == "S2"]
    old_p5_sh = float(old_a_s2.iloc[0]["oos_sharpe"]) if len(old_a_s2) else np.nan
    rows.append({
        "report": "P5", "config": "引擎A S2(min=3,无cap)",
        "metric": "oos_sharpe", "pre_fix": old_p5_sh,
        "post_fix": round(float(m_oos_a_s2.sharpe), 4),
        "note": "旧值基于 v2 缓存(P5 时代)+buggy+P5-era 数据；新值 v8 缓存+fixed（归因见 §4）",
    })

    # P5 归因隔离矩阵（QA fresh-eyes 复核补充）：v2/v8 缓存 × buggy/fixed broker
    # 说明：转正主因是 P8-4 缓存升级 v2→v8（v8+buggy 已 +0.63）；broker 修复在 v8 上边际 +0.36。
    v2_path = ART / "signals_cache18_grouped_v2.parquet"
    if v2_path.exists():
        from hexbroker.backtest.engine import BacktestEngine as _BTE
        import hexbroker.backtest.engine as _bt_engine
        from scripts.p18_verify_broker_fix import LegacyBroker

        def _iso_sharpe(cache_path, broker_cls) -> float:
            tgt = engine_a_targets_cs(prices, TOP_K, 3, cache_path=cache_path)
            orig = _bt_engine.SimBroker
            _bt_engine.SimBroker = broker_cls
            try:
                eng = _BTE(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
                eq = eng.run(prices, tgt).equity_curve
            finally:
                _bt_engine.SimBroker = orig
            idx = pd.to_datetime(eq.index)
            m_oos = compute_metrics(eq[idx >= pd.Timestamp(OOS_START)], freq="daily")
            return float(m_oos.sharpe)

        iso = {
            "v2+buggy": _iso_sharpe(v2_path, LegacyBroker),
            "v2+fixed": _iso_sharpe(v2_path, _bt_engine.SimBroker),
            "v8+buggy": _iso_sharpe(V8_PATH, LegacyBroker),
            "v8+fixed": float(m_oos_a_s2.sharpe),
        }
        print("  [隔离矩阵] 引擎 A S2 OOS Sharpe（当前数据/全历史/OOS 切片）：")
        for k, v in iso.items():
            print(f"    {k:<9}: {v:+.4f}")
        for k, v in iso.items():
            rows.append({
                "report": "P5-ISO", "config": k, "metric": "oos_sharpe",
                "pre_fix": np.nan, "post_fix": round(v, 6),
                "note": "归因隔离矩阵（转正主因=缓存 v2→v8；broker 修复在 v8 上边际 +0.36）",
            })

    # ------------------------------------------------------------------
    # P16 生产组合（引擎 A cap=0.5 + 引擎 B win252/thr0.7 + A10/B90）
    # ------------------------------------------------------------------
    print("\n[2] P16 组合（引擎 A cap0.5 + B win252/thr0.7 + A10/B90）...")
    tgt_a_cap = engine_a_targets_cs(
        prices, TOP_K, 3, cache_path=V8_PATH, group_cap=0.5, group_map=BASE_GROUP_MAP
    )
    ret_a_cap, _eq, m_a_cap, m_oos_a_cap, lr_a_cap = run_engine_row(
        cfg, cost, prices, tgt_a_cap, "A-cap0.5")
    print(f"  引擎 A cap0.5: OOS Sharpe={m_oos_a_cap.sharpe:.3f} "
          f"OOS 复利={m_oos_a_cap.total_return*100:+.2f}%")
    tgt_b = engine_b_targets(prices, 252, 0.70)
    ret_b, _eq, m_b, m_oos_b, lr_b = run_engine_row(cfg, cost, prices, tgt_b, "B")
    print(f"  引擎 B win252/thr0.7: OOS Sharpe={m_oos_b.sharpe:.3f} "
          f"OOS 复利={m_oos_b.total_return*100:+.2f}%")

    old_p9 = load_old_p9_grid()
    for vt, tag in ((False, "volN"), (True, "volY")):
        c = combo_stats_row(ret_a_cap, ret_b, 0.10, vt)
        old_row = old_p9[(old_p9["w_a"] == 0.10) & (old_p9["vol_target"] == vt)]
        old_sh = float(old_row.iloc[0]["oos_sharpe"]) if len(old_row) else np.nan
        old_ret = float(old_row.iloc[0]["oos_ret"]) if len(old_row) else np.nan
        old_dd = float(old_row.iloc[0]["oos_maxdd"]) if len(old_row) else np.nan
        print(f"  A10/B90 {tag}: OOS Sharpe={c['oos_sharpe']:.3f} (旧 {old_sh:.3f}) | "
              f"OOS 复利={c['oos_ret']*100:+.2f}% (旧 {old_ret*100:+.2f}%) | "
              f"OOS MaxDD={c['oos_maxdd']*100:.1f}% (旧 {old_dd*100:.1f}%)")
        for metric, post, pre in (
            ("oos_sharpe", c["oos_sharpe"], old_sh),
            ("oos_ret", c["oos_ret"], old_ret),
            ("oos_maxdd", c["oos_maxdd"], old_dd),
        ):
            rows.append({
                "report": "P16", "config": f"A10/B90 {tag}",
                "metric": metric, "pre_fix": round(float(pre), 6) if pd.notna(pre) else np.nan,
                "post_fix": round(float(post), 6),
                "note": "旧值=P9 网格无 cap 口径(v8+buggy)；新值=cap0.5 生产口径；cap 差异 ~0.005 不改变结论",
            })

    # ------------------------------------------------------------------
    # P9 组合权重网格（引擎 A 无 cap 解析，与旧 p9 同构）→ A10/B90 是否仍最优
    # ------------------------------------------------------------------
    print("\n[3] P9 组合权重网格（12 配置，复利口径）...")
    grid_rows = []
    for w_a in W_A_GRID:
        for vt in VOL_GRID:
            r = combo_stats_row(ret_a_s2, ret_b, w_a, vt)
            r["w_b"] = round(1.0 - w_a, 4)
            grid_rows.append(r)
    grid = pd.DataFrame(grid_rows)
    grid["oos_rank"] = grid["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    grid = grid.sort_values("oos_rank")
    print(grid[["oos_rank", "w_a", "w_b", "vol_target", "oos_sharpe", "oos_ret", "oos_maxdd"]]
          .to_string(index=False))
    best_voln = grid[~grid["vol_target"]].iloc[0]
    best_any = grid.iloc[0]
    a10_voln = grid[(grid["w_a"] == 0.10) & (~grid["vol_target"])].iloc[0]
    a10_voly = grid[(grid["w_a"] == 0.10) & (grid["vol_target"])].iloc[0]
    print(f"  vol=N 最优: A{best_voln['w_a']:.2f}/B{best_voln['w_b']:.2f} "
          f"OOS Sharpe={best_voln['oos_sharpe']:.3f}")
    print(f"  全部最优  : A{best_any['w_a']:.2f}/B{best_any['w_b']:.2f} vol="
          f"{'Y' if best_any['vol_target'] else 'N'} OOS Sharpe={best_any['oos_sharpe']:.3f}")
    print(f"  A10/B90 volN OOS Sharpe={a10_voln['oos_sharpe']:.3f}（旧 1.6117）| "
          f"A10/B90 volY OOS Sharpe={a10_voly['oos_sharpe']:.3f}（旧 1.6619）")
    old_p9_any = old_p9.sort_values("oos_sharpe", ascending=False)
    old_best_voln = old_p9[~old_p9["vol_target"]].sort_values("oos_sharpe", ascending=False).iloc[0]
    old_best_any = old_p9.sort_values("oos_sharpe", ascending=False).iloc[0]
    rows.append({
        "report": "P9", "config": f"网格volN最优 A{best_voln['w_a']:.2f}/B{best_voln['w_b']:.2f}",
        "metric": "oos_sharpe",
        "pre_fix": round(float(old_best_voln["oos_sharpe"]), 6),
        "post_fix": round(float(best_voln["oos_sharpe"]), 6),
        "note": f"旧最优 A{old_best_voln['w_a']:.2f}/B{old_best_voln['w_b']:.2f}",
    })
    rows.append({
        "report": "P9", "config": f"网格全最优 A{best_any['w_a']:.2f}/B{best_any['w_b']:.2f} vol="
                                  f"{'Y' if best_any['vol_target'] else 'N'}",
        "metric": "oos_sharpe",
        "pre_fix": round(float(old_best_any["oos_sharpe"]), 6),
        "post_fix": round(float(best_any["oos_sharpe"]), 6),
        "note": f"旧最优 A{old_best_any['w_a']:.2f}/B{old_best_any['w_b']:.2f} vol="
                f"{'Y' if old_best_any['vol_target'] else 'N'}",
    })
    rows.append({
        "report": "P9", "config": "A10/B90 volN",
        "metric": "oos_sharpe", "pre_fix": 1.6117, "post_fix": round(float(a10_voln["oos_sharpe"]), 6),
        "note": "P16 生产权重",
    })
    rows.append({
        "report": "P9", "config": "A10/B90 volY",
        "metric": "oos_sharpe", "pre_fix": 1.6619, "post_fix": round(float(a10_voly["oos_sharpe"]), 6),
        "note": "P16 生产权重",
    })

    # ------------------------------------------------------------------
    # P11 引擎 B 参数网格（12 组合）→ win252/thr0.70 复验 + 排名变化
    # ------------------------------------------------------------------
    print("\n[4] P11 引擎 B 参数网格（12 组合）...")
    p11_rows = []
    for win in WINS:
        for thr in THRS:
            r = run_combo(cfg, cost, prices, win, thr)
            p11_rows.append(r)
            print(f"  win={win:>3} thr={thr:.2f}: OOS Sharpe={r['oos_sharpe']:.3f} "
                  f"OOS 复利={r['oos_ret']*100:+.2f}% OOS MaxDD={r['oos_maxdd']*100:.1f}%")
    p11_df = pd.DataFrame(p11_rows)
    p11_df["oos_rank"] = p11_df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    old_p11 = load_old_p11_grid()
    prod_new = p11_df[(p11_df["win"] == 252) & (p11_df["thr"] == 0.70)].iloc[0]
    prod_old = old_p11[(old_p11["win"] == 252) & (old_p11["thr"] == 0.70)].iloc[0]
    best_new = p11_df.loc[p11_df["oos_sharpe"].idxmax()]
    best_old = old_p11.loc[old_p11["oos_sharpe"].idxmax()]
    print(f"  win252/thr0.70: OOS Sharpe={prod_new['oos_sharpe']:.3f}（旧 {prod_old['oos_sharpe']:.3f}）| "
          f"OOS 复利={prod_new['oos_ret']*100:+.2f}%（旧 {prod_old['oos_ret']*100:+.2f}%）")
    print(f"  OOS 最优: win{int(best_new['win'])}/thr{best_new['thr']:.2f} "
          f"OOS Sharpe={best_new['oos_sharpe']:.3f}（旧 win{int(best_old['win'])}/thr"
          f"{best_old['thr']:.2f} {best_old['oos_sharpe']:.3f}）")
    for metric in ("oos_sharpe", "oos_ret", "oos_maxdd"):
        rows.append({
            "report": "P11", "config": "win252/thr0.70",
            "metric": metric,
            "pre_fix": round(float(prod_old[metric]), 6),
            "post_fix": round(float(prod_new[metric]), 6),
            "note": "生产配置",
        })
    rows.append({
        "report": "P11", "config": f"网格最优 win{int(best_new['win'])}/thr{best_new['thr']:.2f}",
        "metric": "oos_sharpe",
        "pre_fix": round(float(best_old["oos_sharpe"]), 6),
        "post_fix": round(float(best_new["oos_sharpe"]), 6),
        "note": f"旧最优 win{int(best_old['win'])}/thr{best_old['thr']:.2f}",
    })

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    out = pd.DataFrame(rows)
    out.to_csv(ART / "p18_reestimation.csv", index=False, encoding="utf-8-sig")
    print(f"\n[OK] → {ART / 'p18_reestimation.csv'}")
    print(out.to_string(index=False))

    # 附加：P11 网格保存（供明细）
    p11_df.to_csv(ART / "p18_engineB_grid_postfix.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(ART / "p18_combo_grid_postfix.csv", index=False, encoding="utf-8-sig")
    print(f"[OK] → {ART / 'p18_engineB_grid_postfix.csv'} / {ART / 'p18_combo_grid_postfix.csv'}")


if __name__ == "__main__":
    main()
