"""P10-2 权重下界补测（QA：A10/B90 是网格下边界最优 → 真正最优可能 <0.10）。

背景（P9 定案 + QA 补测要求）
----------------------------
- P9-1 网格（A∈{0.10..0.35} × vol on/off，复利口径）：vol=N 时 OOS Sharpe 随
  w_a 单调下降（A10=1.612 > A15=1.602 > A20=1.587 > A25=1.569 > A30=1.545 >
  A35=1.515），A10/B90 是网格**下边界**最优 → 真正最优可能 <0.10。
- QA 补测要求：A5/B95、A0/B100（vol=N，与 P9 网格同口径：引擎 A v8 + S2 min=3、
  引擎 B win252/thr0.7、复利口径 OOS 2024-07-18 后）。

本脚本（自包含，复用 P9 逻辑）：
  Stage 1  权重下界网格 A∈{0.00, 0.05, 0.10, 0.15}（B=1-A）× vol=N
           → 全样本/OOS Sharpe、OOS 复利、OOS MaxDD 对比表
           → artifacts/p10_weight_lower_bound.csv
  Stage 2  生产 cap 附录：引擎 A 叠加部署 yaml（configs/base.yaml，P10-1）的
           group_cap=0.5 + ferrous_all 合并映射，A∈{0.00,0.05,0.10} 复测
           （验证生产配置下权重下界结论是否稳健）
  Stage 3  裁决建议：A5/B95 或 A0/B100 是否优于 A10/B90（OOS 1.612）；
           是否更新 ComboConfig（A0/B100=纯引擎 B，需确认"引擎 A 完全剔除"
           的语义——给出裁决建议，是否改配置由主理人定）。

口径铁律：与 P9 网格完全同口径（基线引擎 A v8 + S2 min=3，无 cap → 与 P9
网格可比）；复利口径评估（OOS 权益曲线 total_return）；OOS 2024-07-18 后；
完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）；不改 v8 缓存、不改数据文件。

用法：
  python scripts/p10_weight_lower_bound.py
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
from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    OOS_START,
    TOP_K,
    engine_b_targets,
)
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"

# 权重下界网格（vol=N，与 P9 网格同口径；A0=纯引擎 B）
W_A_LOWER = [0.00, 0.05, 0.10, 0.15]
MIN_SYMBOLS_S2 = 3  # P5 终裁：S2 截面 rank + min=3

# P9 基准（对比锚点）：A10/B90 vol=N OOS Sharpe
P9_A10_OOS_SHARPE = 1.612
P9_A10_OOS_RET = 0.2870


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%" if pd.notna(x) else "   n/a"


def fmt_sh(x: float) -> str:
    return f"{x:.3f}" if pd.notna(x) else "  n/a"


def align_rets(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def run_combo_table(ret_a: pd.Series, ret_b: pd.Series, weights: list[float],
                    tag: str) -> pd.DataFrame:
    """给定两引擎日收益，跑权重网格（vol=N）→ 对比表。"""
    rows = []
    for w_a in weights:
        ra, rb = align_rets(ret_a, ret_b)
        r = combo_stats_row(ra, rb, w_a, vol_target=False)
        r["w_b"] = round(1.0 - w_a, 4)
        r["variant"] = tag
        rows.append(r)
    return pd.DataFrame(rows)


def main() -> None:
    t_start = time.time()
    print("=" * 100)
    print("P10-2 权重下界补测（QA：A10/B90 是网格下边界最优 → 补测 A5/B95、A0/B100）")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"INITIAL_CAPITAL={INITIAL_CAPITAL:,.0f} | top_k={TOP_K} | 名义={0.20*100:.0f}%权益/标的 | "
          f"复利口径评估")
    print("=" * 100)

    # ---- 0. 环境 ----
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"\n[0] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种 | "
          f"v8 缓存: {V8_PATH.name}（存在={V8_PATH.exists()}）")

    # ---- 引擎 B（固定 win252/thr0.7）----
    print("\n[0b] 引擎 B（基差 win252/thr0.7）")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, eq_b, m_b, m_oos_b, lr_b = run_engine_row(cfg, cost, prices, tgt_b, "B")
    print(f"  引擎 B: Sharpe={m_b.sharpe:.3f} 年化={m_b.annual_return*100:+.1f}% "
          f"MaxDD={m_b.max_drawdown*100:.1f}% | OOS Sharpe={m_oos_b.sharpe:.3f} "
          f"OOS 复利={m_oos_b.total_return*100:+.2f}% OOS MaxDD={m_oos_b.max_drawdown*100:.1f}%")

    # ---- Stage 1: 权重下界网格（无 cap，与 P9 网格同口径可比）----
    print("-" * 100)
    print("[Stage 1] 权重下界网格 A∈{0.00,0.05,0.10,0.15} × vol=N（基线引擎 A v8 S2 无 cap）")
    tgt_a = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=V8_PATH)
    ret_a, eq_a, m_a, m_oos_a, lr_a = run_engine_row(cfg, cost, prices, tgt_a, "A-v8")
    print(f"  引擎 A v8 S2: Sharpe={m_a.sharpe:.3f} 年化={m_a.annual_return*100:+.1f}% "
          f"MaxDD={m_a.max_drawdown*100:.1f}% | OOS Sharpe={m_oos_a.sharpe:.3f} "
          f"OOS 复利={m_oos_a.total_return*100:+.2f}% OOS MaxDD={m_oos_a.max_drawdown*100:.1f}%")

    tbl = run_combo_table(ret_a, ret_b, W_A_LOWER, "baseline")
    ART.mkdir(exist_ok=True)

    # 单引擎参考行（引擎 B = A0/B100 等价；引擎 A 供上下文）
    rows_ctx = [{
        "w_a": np.nan, "w_b": np.nan, "vol_target": False, "variant": "engine_A",
        "sharpe_full": m_a.sharpe, "ann_ret_full": m_a.annual_return,
        "maxdd_full": m_a.max_drawdown,
        "oos_sharpe": m_oos_a.sharpe, "oos_maxdd": m_oos_a.max_drawdown,
        "oos_ret": m_oos_a.total_return, "oos_n": m_oos_a.n_bars,
    }]

    # ---- Stage 2: 生产 cap 附录（部署 yaml：group_cap=0.5 + ferrous_all）----
    print("-" * 100)
    print("[Stage 2] 生产 cap 附录（P10-1 部署 yaml：group_cap=0.5 + ferrous_all 合并映射）")
    prod_cfg = load_config("configs/base.yaml")
    prod_cap = prod_cfg.backtest.engine_a.group_cap
    prod_gm = dict(prod_cfg.backtest.engine_a.group_map or {})
    print(f"  group_cap={prod_cap!r} | group_map 键数={len(prod_gm)} | "
          f"黑色系→{ {s: prod_gm.get(s) for s in ('i0','j0','jm0','rb0','hc0')} }")
    tgt_a_cap = engine_a_targets_cs(prices, TOP_K, MIN_SYMBOLS_S2, cache_path=V8_PATH,
                                    group_cap=prod_cap, group_map=prod_gm)
    ret_a_cap, eq_a_cap, m_a_cap, m_oos_a_cap, lr_a_cap = run_engine_row(
        cfg, cost, prices, tgt_a_cap, "A-cap")
    print(f"  引擎 A v8 cap0.5: Sharpe={m_a_cap.sharpe:.3f} "
          f"年化={m_a_cap.annual_return*100:+.1f}% MaxDD={m_a_cap.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={m_oos_a_cap.sharpe:.3f} OOS 复利={m_oos_a_cap.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos_a_cap.max_drawdown*100:.1f}% | 做多天数占比={lr_a_cap*100:.1f}%")
    tbl_cap = run_combo_table(ret_a_cap, ret_b, [0.00, 0.05, 0.10], "prod_cap")

    # 合并落盘：Stage 1 表 + 引擎 A 参考行 + Stage 2 cap 表
    out_cols = ["variant", "w_a", "w_b", "vol_target", "sharpe_full", "ann_ret_full",
                "maxdd_full", "oos_sharpe", "oos_maxdd", "oos_ret", "oos_n"]
    out = pd.concat([
        pd.DataFrame(rows_ctx),
        tbl.assign(vol_target=False),
        tbl_cap.assign(vol_target=False),
    ], ignore_index=True)
    out = out[out_cols].sort_values(["variant", "w_a"], na_position="first")
    out.to_csv(ART / "p10_weight_lower_bound.csv", index=False)
    print(f"  [OK] → {ART / 'p10_weight_lower_bound.csv'}")

    # 展示 Stage 1 表
    show = tbl.copy()
    show["sharpe_full"] = show["sharpe_full"].map(fmt_sh)
    show["ann_ret_full"] = show["ann_ret_full"].map(fmt_pct)
    show["maxdd_full"] = show["maxdd_full"].map(fmt_pct)
    show["oos_sharpe"] = show["oos_sharpe"].map(fmt_sh)
    show["oos_maxdd"] = show["oos_maxdd"].map(fmt_pct)
    show["oos_ret"] = show["oos_ret"].map(fmt_pct)
    show["label"] = show["w_a"].map(lambda w: f"A{int(round(w*100))}/B{int(round((1-w)*100))}")
    print("\n  权重下界对比表（vol=N，基线无 cap，与 P9 网格同口径）：")
    print(show[["label", "w_a", "w_b", "sharpe_full", "ann_ret_full", "maxdd_full",
                "oos_sharpe", "oos_ret", "oos_maxdd"]].to_string(index=False))

    # Stage 2 展示
    show2 = tbl_cap.copy()
    show2["oos_sharpe"] = show2["oos_sharpe"].map(fmt_sh)
    show2["oos_ret"] = show2["oos_ret"].map(fmt_pct)
    show2["oos_maxdd"] = show2["oos_maxdd"].map(fmt_pct)
    show2["label"] = show2["w_a"].map(lambda w: f"A{int(round(w*100))}/B{int(round((1-w)*100))}")
    print("\n  生产 cap 附录（引擎 A 叠加 group_cap=0.5 + ferrous_all，vol=N）：")
    print(show2[["label", "oos_sharpe", "oos_ret", "oos_maxdd"]].to_string(index=False))

    # ---- Stage 3: 裁决建议 ----
    print("-" * 100)
    print("[Stage 3] 权重下界裁决建议")
    base = tbl[tbl["w_a"] == 0.10].iloc[0]
    a5 = tbl[tbl["w_a"] == 0.05].iloc[0]
    a0 = tbl[tbl["w_a"] == 0.00].iloc[0]
    a15 = tbl[tbl["w_a"] == 0.15].iloc[0]

    print(f"  A10/B90（P9 终裁）: OOS Sharpe={base['oos_sharpe']:.3f} "
          f"OOS 复利={base['oos_ret']*100:+.2f}% OOS MaxDD={base['oos_maxdd']*100:.1f}% "
          f"全样本 Sharpe={base['sharpe_full']:.3f}")
    print(f"  A05/B95           : OOS Sharpe={a5['oos_sharpe']:.3f} "
          f"OOS 复利={a5['oos_ret']*100:+.2f}% OOS MaxDD={a5['oos_maxdd']*100:.1f}% "
          f"全样本 Sharpe={a5['sharpe_full']:.3f}  | Δ(A5-A10)={a5['oos_sharpe']-base['oos_sharpe']:+.3f}")
    print(f"  A00/B100（纯 B）  : OOS Sharpe={a0['oos_sharpe']:.3f} "
          f"OOS 复利={a0['oos_ret']*100:+.2f}% OOS MaxDD={a0['oos_maxdd']*100:.1f}% "
          f"全样本 Sharpe={a0['sharpe_full']:.3f}  | Δ(A0-A10)={a0['oos_sharpe']-base['oos_sharpe']:+.3f}")
    print(f"  A15/B85（P8 基线）: OOS Sharpe={a15['oos_sharpe']:.3f} "
          f"OOS 复利={a15['oos_ret']*100:+.2f}%")

    # 单调性 + 下界判断（w_a 上升 → OOS Sharpe 应单调不增：A0 ≥ A5 ≥ A10 ≥ A15）
    sharpe_seq = [tbl.loc[tbl["w_a"] == w, "oos_sharpe"].iloc[0] for w in W_A_LOWER]
    monotone = all(sharpe_seq[i] >= sharpe_seq[i + 1] - 1e-12 for i in range(len(sharpe_seq) - 1))
    print(f"  OOS Sharpe 随 w_a 单调性（A0≥A5≥A10≥A15）: {'是' if monotone else '否'} "
          f"→ 序列 {[round(s,3) for s in sharpe_seq]}")

    best_w = W_A_LOWER[int(np.argmax(sharpe_seq))]
    print(f"  下界最优: A{best_w:.2f}/B{1-best_w:.2f}（OOS Sharpe={max(sharpe_seq):.3f}）")

    better_a5 = a5["oos_sharpe"] > base["oos_sharpe"] + 1e-9
    better_a0 = a0["oos_sharpe"] > base["oos_sharpe"] + 1e-9
    verdict = "CHANGE_LOWER" if (better_a5 or better_a0) else "KEEP_A10"
    if better_a0:
        rec = "A0/B100（纯引擎 B）OOS Sharpe 最优"
    elif better_a5:
        rec = "A5/B95 OOS Sharpe 更优"
    else:
        rec = "A10/B90 保持最优（下界补测未发现更优权重）"

    print(f"\n  裁决: {verdict} —— {rec}")
    print("  注意: A0/B100 = 引擎 A 完全剔除（纯引擎 B）。引擎 A OOS 截面 IC 为负")
    print("        （P9 记录 -0.064），权重下降单调改善符合先验一致；但引擎 A 提供")
    print("        组合分散化与全样本 Sharpe 抬升，是否完全剔除需主理人结合生产语义")
    print("        裁决（本脚本仅给数据裁决建议，不改 ComboConfig 默认值）。")

    # 生产 cap 下结论是否稳健
    cap_base = tbl_cap[tbl_cap["w_a"] == 0.10].iloc[0]
    cap_a5 = tbl_cap[tbl_cap["w_a"] == 0.05].iloc[0]
    cap_a0 = tbl_cap[tbl_cap["w_a"] == 0.00].iloc[0]
    cap_best = min(
        [(0.00, cap_a0["oos_sharpe"]), (0.05, cap_a5["oos_sharpe"]),
         (0.10, cap_base["oos_sharpe"])],
        key=lambda kv: -kv[1],
    )
    print(f"  生产 cap 下: A0/B100 OOS Sharpe={cap_a0['oos_sharpe']:.3f} | "
          f"A5/B95={cap_a5['oos_sharpe']:.3f} | A10/B90={cap_base['oos_sharpe']:.3f} "
          f"→ 下界最优 A{cap_best[0]:.2f}（{'与基线一致' if abs(cap_best[0]-best_w)<1e-9 else '与基线不同'}）")

    print("=" * 100)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s")
    print("=" * 100)


if __name__ == "__main__":
    main()
