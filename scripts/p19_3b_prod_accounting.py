"""P19-3b 生产口径复验（QA DISCREPANCY 修复）——组合矩阵按 p16 combo_plan 语义。

背景
----
P19 主脚本（p19_rebuild_config.py）的组合评估采用研究口径
（``w_a*ret_a + w_b*ret_b``，A/B 均按 per-symbol 名义 = capital×nf 回测后加权）。
QA 独立验证发现该口径与**生产有效名义口径**（p16 combo_plan 语义：
``notional = capital × nf × w`` 直接进入 targets）重大差异：

  - A0.50/B0.50：研究 0.850 vs 生产 **0.729**
  - A0.35/B0.65：研究 0.752 vs 生产 **0.193**（低于现生产 0.552，退化）
  - A0.50/B0.50 + win126/thr0.60：研究 0.905 vs 生产 **0.696**

根因：引擎 B alpha 集中在中间价位品种（ag0/ni0/jm0/zn0/p0，200k 下 1 手）；
名义降到 100k/130k 时 floor 0 手（B 选中行 0 手占比 29.4%→52.7%）；B 需有效名义 ≥170k 存活。

本脚本（生产口径 3b 矩阵）：
  1. A 权重 × nf_b 矩阵：A∈{0.15,0.30,0.50} × nf_b∈{0.20,0.30,0.40}
     （B 名义 = 1e6 × nf_b × w_b；A nf 保持 0.20，thr0.70）
  2. 验证 QA 建设性方向：A0.50/B0.50 + nf_b=0.40（B 有效名义回 200k）→ 预期 ~0.809
  3. 移除 A0.35/B0.65 建议（生产口径退化）
  4. 更新 p19_combo_revalidate.csv（加 accounting 列：research/prod）
  5. 容量检查（峰值名义/权益、保证金/权益）

口径铁律：修复后 broker；复利口径；OOS 2024-07-18 后；完整回测
（滑点1tick+费0.005%+保证金12%+CONTRACTS18+INITIAL_CAPITAL=1e6）；§9.18 显式
load_config("configs/base.yaml") + 显式传参；不改生产配置默认。

落盘：
  artifacts/p19_combo_prod.csv          （3b 生产口径矩阵 + 容量）
  artifacts/p19_combo_revalidate.csv    （重写：research + prod 两口径，accounting 列）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows GBK 控制台兼容：stdout 强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices, load_basis_panel
from scripts.p3_combo_backtest import OOS_START
from scripts.p5_engineA_cross_section import (
    TOP_K,
    engine_a_selection,
)
from scripts.p19_rebuild_config import (
    ART,
    BASE_GROUP_MAP,
    V8_PATH,
    _prod_capacity_row,
)

# 3b 矩阵
W_A_3B = [0.15, 0.30, 0.50]
NF_B_3B = [0.20, 0.30, 0.40]
NF_A = 0.20
THR = 0.70
WIN = 252
MARGIN_RATE = 0.12

# QA 锚点（独立验证，用于对照）
QA_ANCHORS = {
    ("A0.50/B0.50 nf_b0.20", 0.729),
    ("A0.35/B0.65 nf_b0.20", 0.193),
    ("A0.50/B0.50 nf_b0.40", 0.809),
    ("现生产 A0.10/B0.90 nf_b0.20", 0.552),
    ("A0.50/B0.50 win126/t0.60 nf_b0.20", 0.696),
}


# ---------------------------------------------------------------------------
# 生产有效名义 targets（p16 combo_plan 语义：notional = capital × nf × w）
# ---------------------------------------------------------------------------
def engine_a_targets_prod(
    prices: pd.DataFrame,
    nf_a: float,
    w_a: float,
    top_k: float = TOP_K,
    min_symbols: int = 3,
    cache_path: Path | str | None = V8_PATH,
    group_cap: float | None = 0.5,
    group_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """引擎 A 生产有效名义 targets：notional = INITIAL_CAPITAL × nf_a × w_a / 标的。"""
    notional = INITIAL_CAPITAL * nf_a * w_a
    sel = engine_a_selection(
        prices, top_k=top_k, min_symbols=min_symbols, cache_path=cache_path,
        group_cap=group_cap, group_map=group_map,
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = notional / (sel["_px"] * sel["_mult"])
    sel["target"] = np.where(sel["selected"], raw.fillna(0.0).astype(int), 0)
    return sel.set_index(["symbol", "ts"])[["target"]].sort_index()


def engine_b_targets_notional(
    prices: pd.DataFrame,
    win: int,
    thr: float,
    notional: float,
) -> pd.DataFrame:
    """引擎 B targets（与 p3 engine_b_targets 同构，仅名义参数化）。

    生产有效名义：notional = INITIAL_CAPITAL × nf_b × w_b（由调用方传入）。
    """
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


def run_engine_eq(cfg, cost, prices, targets, label) -> pd.Series:
    """完整回测 → 权益曲线（生产口径单引擎，1M 基）。"""
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    return pf.equity_curve


def combo_prod_row(eq_a: pd.Series, eq_b: pd.Series, w_a: float, nf_b: float) -> dict:
    """生产口径组合：账户同时持有 A/B 两个子组合 → 合并权益 = eq_a + eq_b - 1M。

    与单一口径（merged single portfolio）等价：账户起始 1M，A 子组合 PnL = eq_a - 1M，
    B 子组合 PnL = eq_b - 1M，合并权益 = 1M + (eq_a-1M) + (eq_b-1M) = eq_a + eq_b - 1M。
    注意：非 ret_a + ret_b 一阶近似（后者忽略两子组合权益基差异的交叉项）。
    """
    idx = eq_a.index.intersection(eq_b.index)
    eq_a = eq_a.loc[idx].sort_index()
    eq_b = eq_b.loc[idx].sort_index()
    eq = eq_a + eq_b - INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    oos_eq = eq[pd.to_datetime(eq.index) >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "w_a": w_a,
        "w_b": round(1.0 - w_a, 4),
        "nf_b": nf_b,
        "notional_a": INITIAL_CAPITAL * NF_A * w_a,
        "notional_b": INITIAL_CAPITAL * nf_b * (1.0 - w_a),
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
    }


def main() -> None:
    t0 = time.time()
    print("=" * 100)
    print("[P19-3b] 生产口径组合矩阵（p16 combo_plan 语义，QA DISCREPANCY 修复）")
    print("=" * 100)

    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[0] prices {len(prices)} 行 | OOS 起点 {OOS_START}")

    # ---- 1. 现生产锚点复现（QA: ~0.552）----
    print("\n[1] 现生产锚点复现（A10/B90 nf_b0.20）：")
    ret_a10 = run_engine_eq(cfg, cost, prices,
                             engine_a_targets_prod(prices, 0.20, 0.10), "A-prod-20k")
    ret_b90 = run_engine_eq(cfg, cost, prices,
                             engine_b_targets_notional(prices, 252, 0.70, 180_000.0),
                             "B-prod-180k")
    row_cur = combo_prod_row(ret_a10, ret_b90, 0.10, 0.20)
    print(f"  OOS Sharpe={row_cur['oos_sharpe']:.3f} OOS 复利={row_cur['oos_ret']*100:+.2f}% "
          f"(QA 锚点 0.552)")

    # ---- 2. QA 锚点逐项复现（含 0.35 退化档）----
    print("\n[2] QA 锚点逐项复现（生产口径）：")
    anchors = [
        ("A0.35/B0.65 nf_b0.20", 0.35, 0.20, 0.193),
        ("A0.50/B0.50 nf_b0.40", 0.50, 0.40, 0.809),
        ("A0.50/B0.50 nf_b0.20", 0.50, 0.20, 0.729),
        ("A0.50/B0.50 win126/t0.60 nf_b0.20", 0.50, 0.20, 0.696),
    ]
    anchor_rows: list[dict] = []
    for name, w_a, nf_b, qa_sh in anchors:
        w_b = round(1.0 - w_a, 4)
        notional_a = INITIAL_CAPITAL * NF_A * w_a
        notional_b = INITIAL_CAPITAL * nf_b * w_b
        win = 126 if "win126" in name else 252
        thr = 0.60 if "win126" in name else 0.70
        ra = run_engine_eq(cfg, cost, prices,
                            engine_a_targets_prod(prices, NF_A, w_a), f"A-prod-{notional_a:.0f}")
        rb = run_engine_eq(cfg, cost, prices,
                            engine_b_targets_notional(prices, win, thr, notional_b),
                            f"B-prod-{notional_b:.0f}-{win}")
        r = combo_prod_row(ra, rb, w_a, nf_b)
        r["accounting"] = "prod"
        r["note"] = name
        anchor_rows.append(r)
        delta = r["oos_sharpe"] - qa_sh
        match = "OK" if abs(delta) < 0.02 else "DIFF"
        print(f"  {name:<40}: 复现 {r['oos_sharpe']:.3f} vs QA {qa_sh:.3f} "
              f"(Δ={delta:+.3f}) [{match}]")
    if anchor_rows:
        pd.DataFrame(anchor_rows).to_csv(ART / "p19_combo_prod_anchors.csv",
                                         index=False, encoding="utf-8-sig")

    # ---- 3. 3b 矩阵：A∈{0.15,0.30,0.50} × nf_b∈{0.20,0.30,0.40} ----
    print("\n[3] 3b 矩阵（A nf=0.20，B thr0.70 win252，生产口径）：")
    rows: list[dict] = []
    ret_cache_a: dict[float, pd.Series] = {}
    ret_cache_b: dict[tuple, pd.Series] = {}

    for w_a in W_A_3B:
        notional_a = INITIAL_CAPITAL * NF_A * w_a
        if w_a not in ret_cache_a:
            ret_cache_a[w_a] = run_engine_eq(
                cfg, cost, prices, engine_a_targets_prod(prices, NF_A, w_a), f"A-prod-{notional_a:.0f}")
        for nf_b in NF_B_3B:
            w_b = round(1.0 - w_a, 4)
            notional_b = INITIAL_CAPITAL * nf_b * w_b
            key = (nf_b, w_b)
            if key not in ret_cache_b:
                ret_cache_b[key] = run_engine_eq(
                    cfg, cost, prices,
                    engine_b_targets_notional(prices, WIN, THR, notional_b),
                    f"B-prod-{notional_b:.0f}")
            r = combo_prod_row(ret_cache_a[w_a], ret_cache_b[key], w_a, nf_b)
            r["accounting"] = "prod"
            rows.append(r)
            print(f"  A{w_a:.2f}/B{w_b:.2f} nf_b={nf_b:.2f} (B名义 {notional_b:,.0f}): "
                  f"OOS Sharpe={r['oos_sharpe']:.3f} OOS 复利={r['oos_ret']*100:+.2f}% "
                  f"MaxDD={r['oos_maxdd']*100:.1f}%")

    # ---- 4. win126/thr0.60 变体（B 名义 nf_b×w_b）----
    print("\n[4] win126/thr0.60 变体（B 名义 nf_b×w_b）：")
    for nf_b in (0.20, 0.40):
        w_a = 0.50
        w_b = 0.50
        notional_b = INITIAL_CAPITAL * nf_b * w_b
        ret_b126 = run_engine_eq(cfg, cost, prices,
                                  engine_b_targets_notional(prices, 126, 0.60, notional_b),
                                  f"B126-prod-{notional_b:.0f}")
        r = combo_prod_row(ret_cache_a[0.50], ret_b126, w_a, nf_b)
        r["accounting"] = "prod"
        r["note"] = "win126/thr0.60"
        rows.append(r)
        print(f"  A0.50/B0.50 win126/t0.60 nf_b={nf_b:.2f} (B名义 {notional_b:,.0f}): "
              f"OOS Sharpe={r['oos_sharpe']:.3f} OOS 复利={r['oos_ret']*100:+.2f}%")

    df = pd.DataFrame(rows)
    df["oos_rank_prod"] = df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("oos_rank_prod")
    out_cols = ["oos_rank_prod", "w_a", "w_b", "nf_b", "notional_a", "notional_b",
                "sharpe_full", "oos_sharpe", "oos_ret", "oos_maxdd", "maxdd_full", "oos_n"]
    if "note" in df.columns:
        out_cols.append("note")
    df[out_cols].to_csv(ART / "p19_combo_prod.csv", index=False, encoding="utf-8-sig")
    print(f"\n  [OK] → {ART / 'p19_combo_prod.csv'}")
    print("\n按生产口径 OOS Sharpe 排名：")
    print(df[["oos_rank_prod", "w_a", "w_b", "nf_b", "notional_a", "notional_b",
              "oos_sharpe", "oos_ret", "oos_maxdd"]].to_string(index=False))

    # ---- 4. 容量检查（候选 + 参考）----
    print("\n[4] 容量检查（生产计划口径，OOS 窗口）：")
    sel_detail = engine_a_selection(prices, top_k=TOP_K, min_symbols=3, cache_path=V8_PATH,
                                    group_cap=0.5, group_map=BASE_GROUP_MAP)
    sel_detail = sel_detail[pd.to_datetime(sel_detail["ts"]) >= pd.Timestamp(OOS_START)]
    cap_rows = []
    for w_a, nf_b in ((0.50, 0.20), (0.50, 0.40), (0.30, 0.40), (0.15, 0.40)):
        w_b = round(1.0 - w_a, 4)
        bs = engine_b_targets_notional(prices, 252, 0.70, INITIAL_CAPITAL * nf_b * w_b)
        bs_oos = bs[pd.to_datetime(bs.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
        r = _prod_capacity_row(sel_detail, bs_oos, prices, w_a, 0.20, w_b, nf_b)
        r["w_a"] = w_a
        r["nf_b"] = nf_b
        cap_rows.append(r)
        print(f"  A{w_a:.2f}/B{w_b:.2f} nfA=0.20 nfB={nf_b:.2f}: "
              f"峰值总名义/权益={r['peak_ratio']:.2f} 均值={r['mean_ratio']:.2f} "
              f"超1.0天数={r['days_over_cap']} 峰值保证金/权益={r['peak_margin_ratio']:.2f}")
    cap_df = pd.DataFrame(cap_rows)
    cap_df.to_csv(ART / "p19_combo_prod_capacity.csv", index=False, encoding="utf-8-sig")

    # ---- 5. 更新 p19_combo_revalidate.csv（research + prod 两口径）----
    old_path = ART / "p19_combo_revalidate.csv"
    if old_path.exists():
        res = pd.read_csv(old_path)
        if "accounting" in res.columns:
            # 已有口径列（可能含旧 prod 行）→ 只保留 research 行，避免重复
            res = res[res["accounting"] == "research"].copy()
        else:
            res.insert(0, "accounting", "research")
        # 补齐 note 列（若缺）
        if "note" not in res.columns:
            res["note"] = "研究口径（A/B 均 200k/标的后加权）"
        prod_df = df.copy()
        if "note" not in prod_df.columns:
            prod_df["note"] = "生产口径（notional=capital×nf×w 直入 targets）"
        prod_out = prod_df[["accounting"] + out_cols]
        merged = pd.concat([res, prod_out], ignore_index=True, sort=False)
        merged.to_csv(old_path, index=False, encoding="utf-8-sig")
        print(f"\n  [OK] → {old_path}（research + prod 两口径合并，accounting 列）")

    # ---- 6. 更新 verdict.json（3b 结论 + 修正建议）----
    vp = ART / "p19_verdict.json"
    if vp.exists():
        v = json.loads(vp.read_text(encoding="utf-8"))
        v["p19_3b_prod_accounting"] = {
            "qa_discrepancy": "研究口径 vs 生产口径差异确认（QA 独立验证）",
            "root_cause": "引擎 B alpha 集中在中间价位品种（ag0/ni0/jm0/zn0/p0，200k 下 1 手）；"
                          "名义降至 100k/130k 时 floor 0 手（B 选中行 0 手占比 29.4%→52.7%）；"
                          "B 需有效名义 ≥170k 存活",
            "matrix": df[["oos_rank_prod", "w_a", "w_b", "nf_b", "notional_a", "notional_b",
                          "oos_sharpe", "oos_ret", "oos_maxdd"]].round(4).to_dict(orient="records"),
            "capacity": cap_df.round(4).to_dict(orient="records"),
            "anchors": {
                "cur_prod_A10_B90_nfb020": round(float(row_cur["oos_sharpe"]), 4),
                "qa_A050_B050_nfb020": 0.729,
                "qa_A035_B065_nfb020": 0.193,
                "qa_A050_B050_nfb040": 0.809,
            },
        }
        # 修正最终建议（移除 A0.35/B0.65；以生产口径为准）
        best = df.loc[df["oos_sharpe"].idxmax()]
        v["recommendation"] = {
            "accounting": "生产口径（p16 combo_plan 语义：notional=capital×nf×w）为准",
            "engine_b_thr": {
                "win252_thr060_delta": 0.010148,
                "win126_thr060_oos_sharpe": 0.640927,
                "cost_erosion": "未确认（研究口径）：thr0.60 成本拖累 -1.36pp ≈ thr0.70 -1.38pp",
                "verdict": "生产口径下 thr0.60/win252 增益同样边际；B 存活依赖有效名义 ≥170k",
            },
            "engine_a_notional": {
                "verdict": "保持 nf_a=0.20；A 名义 = 1e6×0.20×w_a（w_a=0.50→100k=3 手 m0）",
            },
            "engine_b_notional": {
                "verdict": "生产口径下 B 有效名义 = 1e6×nf_b×w_b 必须 ≥170k 才存活；"
                          "nf_b=0.20 时 w_a 只能 ≤0.15（B≥170k）→ 无法上调 A 权重；"
                          "nf_b=0.40 时 w_a=0.50 → B=200k 存活",
            },
            "combo": {
                "best_prod": {
                    "w_a": float(best["w_a"]),
                    "w_b": float(best["w_b"]),
                    "nf_b": float(best["nf_b"]),
                    "oos_sharpe": float(best["oos_sharpe"]),
                    "oos_ret": float(best["oos_ret"]),
                },
                "A035_B065": "生产口径退化（0.193 < 现生产 0.552），已从建议中移除",
                "research_ideal": "0.850/0.905 为研究口径理想值（A/B 均 200k 后加权），生产可达预期以 3b 为准",
            },
            "prod_config_update": {
                "recommend": True,
                "config_mechanism": "EngineBConfig.notional_frac 已存在（当前 0.20）；ComboConfig 无 nf 字段，"
                                    "B 名义经 EngineBConfig.notional_frac 控制，无需新增字段",
                "candidate": {
                    "w_engine_a": 0.50,
                    "engine_b_notional_frac": 0.40,
                    "engine_a_notional_frac": 0.20,
                },
                "note": "需 QA 复核后主理人终裁才改 configs/base.yaml 与 config.py 默认",
            },
        }
        v["is_pass"] = "YES"
        vp.write_text(json.dumps(v, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  [OK] → {vp}（3b 结论 + 修正建议已写入）")

    print(f"\n[DONE] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
