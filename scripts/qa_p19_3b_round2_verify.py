"""QA P19-3b Round 2 fresh-eyes 复核（不调 p19_3b_prod_matrix / p19_3b_prod_accounting 任何函数）。

核心问题（Round 1 锚点归因）：
  - QA Round 1 声称 A0.50/B0.50 + nf_b0.40 生产口径 = 0.809（combo_production: ret-sum）
  - 工程师 M1(eq-sum)=0.684 / M3(ret+)=0.761 / M5(merged)=0.554
  - 本脚本用**完全独立的实现**（v8 + rank + S2 + cap0.5 截面；B 滚动分位）
    复算三种组合约定，回答：0.809 是否可复现？三约定差异本质？

方法（独立实现，不调 p19 函数）：
  1. 引擎 A：v8 缓存每日截面 rank top30% + S2 min=3 + group_cap=0.5（自写 _capped_selection）
  2. 引擎 B：basis_ratio 品种内滚动分位（win126/252 × thr 0.60/0.70）× 显式名义
  3. 组合三约定：
     - ret_sum（QA Round 1 锚点公式）：(1 + ra + rb).cumprod() × 1M
     - eq_sum（工程师 M1）：eq_a + eq_b - 1M
     - merged（工程师 M5 / p16 字面语义）：两引擎 targets 同品种手数合并 → 单一账户回测
  4. B 0 手占比 + B 名义存活扫描（200k→100k）

口径铁律：复利；OOS 2024-07-18 后；修复后 broker（BacktestEngine）；完整回测
（滑点1tick+费0.005%+保证金12%+CONTRACTS18+INITIAL_CAPITAL=1e6）；不修改生产配置。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL
from scripts.p3_combo_backtest import OOS_START
from scripts.qa_p19_independent_verify import (
    GROUP_CAP,
    engine_a_selection_ind,
    engine_a_targets_ind,
    engine_b_targets_ind,
    load_prices,
    run_engine,
)

OOS = pd.Timestamp(OOS_START)


def oos_metrics(eq: pd.Series) -> tuple[float, float, float, int]:
    oos_eq = eq[pd.to_datetime(eq.index) >= OOS]
    m = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    if m is None:
        return float("nan"), float("nan"), float("nan"), 0
    return m.sharpe, m.total_return, m.max_drawdown, int(len(oos_eq))


def combo_ret_sum(ret_a: pd.Series, ret_b: pd.Series) -> pd.Series:
    """QA Round 1 锚点公式：两引擎日收益求和后复利（单一 1M 基）。"""
    idx = ret_a.index.intersection(ret_b.index)
    ra, rb = ret_a.loc[idx].sort_index(), ret_b.loc[idx].sort_index()
    return (1.0 + ra + rb).cumprod() * INITIAL_CAPITAL


def combo_eq_sum(eq_a: pd.Series, eq_b: pd.Series) -> pd.Series:
    """工程师 M1：两引擎各自 1M 基权益曲线相加减 1M。"""
    idx = eq_a.index.intersection(eq_b.index)
    ea, eb = eq_a.loc[idx].sort_index(), eq_b.loc[idx].sort_index()
    return ea + eb - INITIAL_CAPITAL


def merged_run(cfg, cost, prices, t_a: pd.DataFrame, t_b: pd.DataFrame) -> pd.Series:
    """工程师 M5 / p16 字面语义：同品种同日期手数合并 → 单一账户回测。"""
    tb = t_b.copy()
    tb.index = tb.index.set_names(["symbol", "ts"])
    common = t_a.index.union(tb.index)
    merged = (t_a["target"].reindex(common, fill_value=0.0)
              + tb["target"].reindex(common, fill_value=0.0)).to_frame("target")
    _, eq = run_engine(cfg, cost, prices, merged)
    return eq


def main() -> None:
    t0 = time.time()
    print("=" * 100)
    print("[QA P19-3b Round 2] 独立复核：ret_sum vs eq_sum vs merged 三约定")
    print("=" * 100)
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    sel = engine_a_selection_ind(prices, group_cap=GROUP_CAP)
    print(f"[0] prices {len(prices)} 行 | A 选中 {int(sel['selected'].sum())} 行 "
          f"/ {sel.loc[sel['selected'], 'ts'].nunique()} 天 | OOS {OOS_START}")

    # ---------------- 1. B 0 手占比 + 名义存活扫描 ----------------
    print("\n--- 1. 引擎 B 选中行 0 手占比（独立复算，选中=br_rank>=thr）---")
    from scripts.qa_p19_independent_verify import load_basis_panel
    basis = load_basis_panel()
    for win, thr in ((252, 0.70),):
        basis_r = basis.copy()
        basis_r["br_rank"] = basis_r.groupby("symbol")["basis_ratio"].transform(
            lambda s: s.rolling(win, min_periods=60).rank(pct=True)
        )
        sel_rows = basis_r[basis_r["br_rank"] >= thr].copy()
        sel_rows["ts"] = pd.to_datetime(sel_rows.index.get_level_values(1))
        sel_rows["sym"] = sel_rows.index.get_level_values(0)
        sel_rows = sel_rows[sel_rows["ts"] >= OOS]
        px_df = prices["close"].rename("_px").reset_index()
        px_df["datetime"] = pd.to_datetime(px_df["datetime"])
        sel_rows = sel_rows.reset_index().merge(
            px_df, left_on=["sym", "ts"], right_on=["symbol", "datetime"],
            how="inner")
        sel_rows["_mult"] = sel_rows["sym"].map(
            {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18})
        for n in (200_000, 180_000, 130_000, 100_000):
            lots = (n / (sel_rows["_px"] * sel_rows["_mult"])).astype(int)
            n_zero = int((lots == 0).sum())
            n_tot = len(sel_rows)
            print(f"  B@{n:>7,}: 选中行={n_tot:>6}  0手={n_zero:>6} "
                  f"({100*n_zero/n_tot:.1f}%)")

    # 名义存活扫描（单引擎 OOS Sharpe）
    print("\n--- 1b. B 名义存活扫描（win252/thr0.70 单引擎 OOS）---")
    b_sweep = {}
    for n in (200_000, 180_000, 170_000, 160_000, 150_000, 140_000, 130_000, 100_000):
        tgt = engine_b_targets_ind(prices, 252, 0.70, n)
        _, eq = run_engine(cfg, cost, prices, tgt)
        sh, rt, dd, nn = oos_metrics(eq)
        b_sweep[n] = sh
        print(f"  B@{n:>7,}: OOS Sharpe={sh:.3f} ret={rt*100:+.2f}% MaxDD={dd*100:.1f}%")

    # ---------------- 2. 关键组合三约定对比 ----------------
    print("\n--- 2. 关键组合：ret_sum(QA锚点) vs eq_sum(M1) vs merged(M5) ---")
    cases = [
        # (label, w_a, nf_a, nf_b, win, thr)
        ("现生产 A10/B90 nf_b0.20", 0.10, 0.20, 0.20, 252, 0.70),
        ("A0.35/B0.65 nf_b0.20", 0.35, 0.20, 0.20, 252, 0.70),
        ("A0.50/B0.50 nf_b0.20", 0.50, 0.20, 0.20, 252, 0.70),
        ("A0.50/B0.50 nf_b0.40", 0.50, 0.20, 0.40, 252, 0.70),
        ("A0.50/B0.50 w126 nf_b0.20", 0.50, 0.20, 0.20, 126, 0.60),
        ("A0.50/B0.50 w126 nf_b0.40", 0.50, 0.20, 0.40, 126, 0.60),
        ("A0.30/B0.70 nf_b0.30", 0.30, 0.20, 0.30, 252, 0.70),
    ]
    print(f"  {'配置':<26} {'ret_sum':>8} {'eq_sum':>8} {'merged':>8}")
    results = []
    for label, w_a, nf_a, nf_b, win, thr in cases:
        w_b = round(1.0 - w_a, 4)
        notional_a = INITIAL_CAPITAL * nf_a * w_a
        notional_b = INITIAL_CAPITAL * nf_b * w_b
        t_a = engine_a_targets_ind(sel, notional_a)
        t_b = engine_b_targets_ind(prices, win, thr, notional_b)
        ret_a, eq_a = run_engine(cfg, cost, prices, t_a)
        ret_b, eq_b = run_engine(cfg, cost, prices, t_b)

        sh_rs, rt_rs, _, _ = oos_metrics(combo_ret_sum(ret_a, ret_b))
        sh_es, rt_es, _, _ = oos_metrics(combo_eq_sum(eq_a, eq_b))
        eq_m = merged_run(cfg, cost, prices, t_a, t_b)
        sh_m, rt_m, _, _ = oos_metrics(eq_m)
        results.append({"label": label, "w_a": w_a, "nf_a": nf_a, "nf_b": nf_b,
                        "win": win, "thr": thr,
                        "notional_a": notional_a, "notional_b": notional_b,
                        "ret_sum": sh_rs, "eq_sum": sh_es, "merged": sh_m,
                        "ret_sum_ret": rt_rs, "eq_sum_ret": rt_es, "merged_ret": rt_m})
        print(f"  {label:<26} {sh_rs:8.3f} {sh_es:8.3f} {sh_m:8.3f}")

    df = pd.DataFrame(results)
    df.to_csv(ROOT / "artifacts" / "p19_qa_round2_verify.csv", index=False,
              encoding="utf-8-sig")
    print("\n[OK] → artifacts/p19_qa_round2_verify.csv")

    # ---------------- 3. 关键裁决指标 ----------------
    print("\n--- 3. 主推/保守候选对比（M1/M5 双口径）---")
    for label in ("A0.50/B0.50 w126 nf_b0.40", "A0.30/B0.70 nf_b0.30",
                  "A0.15/B0.85 nf_b0.40", "现生产 A10/B90 nf_b0.20"):
        row = df[df["label"] == label].iloc[0]
        print(f"  {label:<26}: ret_sum={row['ret_sum']:.3f} eq_sum={row['eq_sum']:.3f} "
              f"merged={row['merged']:.3f} | ret {row['eq_sum_ret']*100:+.2f}% / "
              f"{row['merged_ret']*100:+.2f}%")

    print(f"\n[DONE] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
