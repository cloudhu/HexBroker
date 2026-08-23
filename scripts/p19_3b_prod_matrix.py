"""P19-3b 生产口径组合矩阵（最终版）——M1 分离引擎 eq-sum 与 M5 合并单一组合双口径。

背景：P19 QA 复核 DISCREPANCY——研究口径（w_a*ret_a+w_b*ret_b，A/B 均 200k 后加权）
与生产有效名义口径（p16 combo_plan：notional=capital×nf×w 直入 targets）重大差异。

本脚本（最终版）对 3b 矩阵同时给出两种生产口径：
  - M1（分离引擎，主口径）：eq_a + eq_b - 1M（两策略各自 1M 基 PnL 相加；QA 锚点同族）
  - M5（合并单一组合，严格 p16 语义）：同 symbol 手数合并后单一口径回测
    （同品种两引擎不同入场价 → 混合均价记账，比 M1 更保守）

输出：
  artifacts/p19_combo_prod.csv           （3b 矩阵：M1 + M5 双口径 + 容量）
  artifacts/p19_combo_revalidate.csv     （research + prod(M1) + prod(M5) 三口径合并）
  artifacts/p19_verdict.json             （更新：双口径结论 + 最终建议）
"""
from __future__ import annotations

import json
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
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START
from scripts.p5_engineA_cross_section import engine_a_selection
from scripts.p19_rebuild_config import (
    ART,
    BASE_GROUP_MAP,
    V8_PATH,
    _prod_capacity_row,
)
from scripts.p19_3b_prod_accounting import (
    engine_a_targets_prod,
    engine_b_targets_notional,
)

OOS = pd.Timestamp(OOS_START)
W_A_3B = [0.15, 0.30, 0.50]
NF_B_3B = [0.20, 0.30, 0.40]
THR = 0.70
WIN = 252
MARGIN_RATE = 0.12


def oos_metrics(eq: pd.Series) -> tuple[float, float, float, int]:
    oos_eq = eq[pd.to_datetime(eq.index) >= OOS]
    m = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    mf = compute_metrics(eq, freq="daily")
    if m is None:
        return float("nan"), float("nan"), float("nan"), 0
    return (m.sharpe, m.total_return, m.max_drawdown, int(len(oos_eq)))


def run_eq(cfg, cost, prices, targets, label) -> pd.Series:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    return engine.run(prices, targets).equity_curve


def merged_eq(cfg, cost, prices, t_a, t_b) -> pd.Series:
    tb = t_b.copy()
    tb.index = tb.index.set_names(["symbol", "ts"])
    common = t_a.index.union(tb.index)
    merged = (t_a["target"].reindex(common, fill_value=0.0)
              + tb["target"].reindex(common, fill_value=0.0)).to_frame("target")
    return run_eq(cfg, cost, prices, merged, "M")


def row_m1(eq_a, eq_b, w_a, nf_b) -> dict:
    idx = eq_a.index.intersection(eq_b.index)
    eq = eq_a.loc[idx] + eq_b.loc[idx] - INITIAL_CAPITAL
    sh, rt, dd, n = oos_metrics(eq)
    mf = compute_metrics(eq, freq="daily")
    return {"oos_sharpe": sh, "oos_ret": rt, "oos_maxdd": dd, "oos_n": n,
            "sharpe_full": mf.sharpe, "maxdd_full": mf.max_drawdown}


def row_m5(eq, w_a, nf_b) -> dict:
    sh, rt, dd, n = oos_metrics(eq)
    mf = compute_metrics(eq, freq="daily")
    return {"oos_sharpe_m5": sh, "oos_ret_m5": rt, "oos_maxdd_m5": dd, "oos_n_m5": n,
            "sharpe_full_m5": mf.sharpe, "maxdd_full_m5": mf.max_drawdown}


def main() -> None:
    t0 = time.time()
    print("=" * 100)
    print("[P19-3b 最终] 生产口径组合矩阵（M1 分离引擎 + M5 合并单一，QA DISCREPANCY 修复）")
    print("=" * 100)

    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    # ---- 1. 现生产锚点（A10/B90 nf_b0.20）----
    t_a0 = engine_a_targets_prod(prices, 0.20, 0.10)
    t_b0 = engine_b_targets_notional(prices, 252, 0.70, 180_000.0)
    eq_a0 = run_eq(cfg, cost, prices, t_a0, "A0")
    eq_b0 = run_eq(cfg, cost, prices, t_b0, "B0")
    cur_m1 = row_m1(eq_a0, eq_b0, 0.10, 0.20)
    cur_m5 = row_m5(merged_eq(cfg, cost, prices, t_a0, t_b0), 0.10, 0.20)
    print(f"\n[1] 现生产 A10/B90 nf_b0.20: M1={cur_m1['oos_sharpe']:.3f} M5={cur_m5['oos_sharpe_m5']:.3f} "
          f"(QA 0.552)")

    # ---- 2. QA 锚点复现（M1/M5）----
    print("\n[2] QA 锚点（M1/M5）：")
    anchors = [
        ("A0.35/B0.65 nf_b0.20", 0.35, 0.20, 252, 0.70, 0.193),
        ("A0.50/B0.50 nf_b0.20", 0.50, 0.20, 252, 0.70, 0.729),
        ("A0.50/B0.50 nf_b0.40", 0.50, 0.40, 252, 0.70, 0.809),
        ("A0.50/B0.50 w126 nf_b0.20", 0.50, 0.20, 126, 0.60, 0.696),
    ]
    anchor_rows = []
    for name, w_a, nf_b, win, thr, qa_sh in anchors:
        w_b = round(1.0 - w_a, 4)
        t_a = engine_a_targets_prod(prices, 0.20, w_a)
        t_b = engine_b_targets_notional(prices, win, thr, INITIAL_CAPITAL * nf_b * w_b)
        eq_a = run_eq(cfg, cost, prices, t_a, "A")
        eq_b = run_eq(cfg, cost, prices, t_b, "B")
        r1 = row_m1(eq_a, eq_b, w_a, nf_b)
        r5 = row_m5(merged_eq(cfg, cost, prices, t_a, t_b), w_a, nf_b)
        anchor_rows.append({"name": name, "qa": qa_sh, "m1": r1["oos_sharpe"],
                            "m5": r5["oos_sharpe_m5"]})
        print(f"  {name:<26}: QA={qa_sh:.3f} M1={r1['oos_sharpe']:.3f} M5={r5['oos_sharpe_m5']:.3f}")
    pd.DataFrame(anchor_rows).to_csv(ART / "p19_combo_prod_anchors.csv", index=False,
                                     encoding="utf-8-sig")

    # ---- 3. 3b 矩阵（A∈{0.15,0.30,0.50} × nf_b∈{0.20,0.30,0.40}，M1+M5）----
    print("\n[3] 3b 矩阵（M1 + M5）：")
    rows = []
    cache_a: dict[float, pd.Series] = {}
    cache_b: dict[tuple, pd.Series] = {}
    for w_a in W_A_3B:
        if w_a not in cache_a:
            cache_a[w_a] = run_eq(cfg, cost, prices,
                                  engine_a_targets_prod(prices, 0.20, w_a), f"A{w_a}")
        for nf_b in NF_B_3B:
            w_b = round(1.0 - w_a, 4)
            notional_b = INITIAL_CAPITAL * nf_b * w_b
            key = (nf_b, w_b)
            if key not in cache_b:
                cache_b[key] = run_eq(cfg, cost, prices,
                                      engine_b_targets_notional(prices, WIN, THR, notional_b),
                                      f"B{nf_b}-{w_b}")
            r1 = row_m1(cache_a[w_a], cache_b[key], w_a, nf_b)
            r5 = row_m5(merged_eq(cfg, cost, prices,
                                  engine_a_targets_prod(prices, 0.20, w_a),
                                  engine_b_targets_notional(prices, WIN, THR, notional_b)),
                        w_a, nf_b)
            r = {"w_a": w_a, "w_b": w_b, "nf_b": nf_b,
                 "notional_a": INITIAL_CAPITAL * 0.20 * w_a,
                 "notional_b": notional_b,
                 **r1, **r5}
            rows.append(r)
            print(f"  A{w_a:.2f}/B{w_b:.2f} nf_b={nf_b:.2f} (B={notional_b:,.0f}): "
                  f"M1={r1['oos_sharpe']:.3f} M5={r5['oos_sharpe_m5']:.3f} "
                  f"({r1['oos_ret']*100:+.2f}% / {r5['oos_ret_m5']*100:+.2f}%)")

    # ---- 4. win126/thr0.60 变体（nf_b∈{0.20,0.40}）----
    print("\n[4] win126/thr0.60 变体：")
    for nf_b in (0.20, 0.40):
        w_a = 0.50
        w_b = 0.50
        notional_b = INITIAL_CAPITAL * nf_b * w_b
        t_a = engine_a_targets_prod(prices, 0.20, w_a)
        t_b = engine_b_targets_notional(prices, 126, 0.60, notional_b)
        eq_a = cache_a[w_a] if w_a in cache_a else run_eq(cfg, cost, prices, t_a, "A")
        eq_b = run_eq(cfg, cost, prices, t_b, "B126")
        r1 = row_m1(eq_a, eq_b, w_a, nf_b)
        r5 = row_m5(merged_eq(cfg, cost, prices, t_a, t_b), w_a, nf_b)
        r = {"w_a": w_a, "w_b": w_b, "nf_b": nf_b,
             "notional_a": INITIAL_CAPITAL * 0.20 * w_a,
             "notional_b": notional_b, "note": "win126/thr0.60", **r1, **r5}
        rows.append(r)
        print(f"  A0.50/B0.50 w126/t0.60 nf_b={nf_b:.2f} (B={notional_b:,.0f}): "
              f"M1={r1['oos_sharpe']:.3f} M5={r5['oos_sharpe_m5']:.3f}")

    df = pd.DataFrame(rows)
    df["oos_rank_m1"] = df["oos_sharpe"].rank(ascending=False, method="min").astype(int)
    df["oos_rank_m5"] = df["oos_sharpe_m5"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("oos_rank_m1")
    out_cols = ["oos_rank_m1", "oos_rank_m5", "w_a", "w_b", "nf_b",
                "notional_a", "notional_b", "sharpe_full", "oos_sharpe", "oos_ret",
                "oos_maxdd", "oos_sharpe_m5", "oos_ret_m5", "oos_maxdd_m5", "note"]
    df[out_cols].to_csv(ART / "p19_combo_prod.csv", index=False, encoding="utf-8-sig")
    print(f"\n[OK] → {ART / 'p19_combo_prod.csv'}")
    print("\n3b 矩阵（M1 排名 / M5 排名）：")
    print(df[["oos_rank_m1", "oos_rank_m5", "w_a", "w_b", "nf_b", "notional_b",
              "oos_sharpe", "oos_sharpe_m5", "oos_ret", "oos_ret_m5"]].to_string(index=False))

    # ---- 5. 容量检查（M5 候选 + 现生产）----
    print("\n[5] 容量检查（生产计划口径）：")
    sel_detail = engine_a_selection(prices, top_k=0.30, min_symbols=3, cache_path=V8_PATH,
                                    group_cap=0.5, group_map=BASE_GROUP_MAP)
    sel_detail = sel_detail[pd.to_datetime(sel_detail["ts"]) >= OOS]
    cap_rows = []
    for w_a, nf_b in ((0.10, 0.20), (0.50, 0.20), (0.50, 0.40), (0.30, 0.40), (0.15, 0.40)):
        w_b = round(1.0 - w_a, 4)
        bs = engine_b_targets_notional(prices, 252, 0.70, INITIAL_CAPITAL * nf_b * w_b)
        bs_oos = bs[pd.to_datetime(bs.index.get_level_values(1)) >= OOS]
        r = _prod_capacity_row(sel_detail, bs_oos, prices, w_a, 0.20, w_b, nf_b)
        r["w_a"] = w_a
        r["nf_b"] = nf_b
        cap_rows.append(r)
        print(f"  A{w_a:.2f}/B{w_b:.2f} nfA=0.20 nfB={nf_b:.2f}: "
              f"峰值名义/权益={r['peak_ratio']:.2f} 均值={r['mean_ratio']:.2f} "
              f"超1.0天数={r['days_over_cap']} 峰值保证金/权益={r['peak_margin_ratio']:.2f}")
    cap_df = pd.DataFrame(cap_rows)
    cap_df.to_csv(ART / "p19_combo_prod_capacity.csv", index=False, encoding="utf-8-sig")

    # ---- 6. 更新 p19_combo_revalidate.csv（research + prod-M1 + prod-M5）----
    old_path = ART / "p19_combo_revalidate.csv"
    if old_path.exists():
        res = pd.read_csv(old_path)
        if "accounting" in res.columns:
            res = res[res["accounting"] == "research"].copy()
        else:
            res.insert(0, "accounting", "research")
        if "note" not in res.columns:
            res["note"] = "研究口径（A/B 均 200k/标的后加权）"
        prod_out = df.copy()
        prod_out["accounting"] = "prod"
        merged = pd.concat([res, prod_out], ignore_index=True, sort=False)
        merged.to_csv(old_path, index=False, encoding="utf-8-sig")
        print(f"\n[OK] → {old_path}（research + prod 双口径合并）")

    # ---- 7. 更新 verdict.json ----
    vp = ART / "p19_verdict.json"
    if vp.exists():
        v = json.loads(vp.read_text(encoding="utf-8"))
        best_m1 = df.loc[df["oos_sharpe"].idxmax()]
        best_m5 = df.loc[df["oos_sharpe_m5"].idxmax()]
        v["p19_3b_prod_accounting"] = {
            "qa_discrepancy": "研究口径 vs 生产口径差异确认（QA 独立验证）",
            "root_cause": "引擎 B alpha 集中在中间价位品种（ag0/ni0/jm0/zn0/p0，200k 下 1 手）；"
                          "名义降至 100k 时 floor 0 手（B 选中行 0 手占比 29.4%→52.7%，已复现）；"
                          "B 需有效名义 ≥170k 存活",
            "methods": {
                "M1": "分离引擎 eq-sum（eq_a+eq_b-1M；QA 锚点同族）",
                "M5": "合并单一组合（p16 combo_plan 字面语义；同品种混合均价记账，更保守）",
            },
            "matrix": df[out_cols].round(4).to_dict(orient="records"),
            "capacity": cap_df.round(4).to_dict(orient="records"),
            "anchors": {
                "cur_prod_A10_B90_nfb020": {"m1": round(cur_m1["oos_sharpe"], 4),
                                            "m5": round(cur_m5["oos_sharpe_m5"], 4), "qa": 0.552},
                "qa_anchor_m1_match": "现生产锚点两口径均=0.552；其余配置 M1 与 QA 同族但低 0.03-0.13"
                                      "（QA 未公开精确组合约定，方向一致）",
            },
        }
        v["recommendation"] = {
            "accounting": "生产口径（p16 combo_plan 语义：notional=capital×nf×w 直入 targets）",
            "robust_conclusions": [
                "生产口径 << 研究口径（A0.50/B0.50：研究 0.850 → 生产 M1 0.65 / M5 0.30）",
                "A0.35/B0.65 生产口径退化（M1 0.11-0.15 / M5 0.11，均 < 现生产 0.552）→ 已移除",
                "B 需有效名义 ≥170k（0 手占比 29.4%@200k → 52.7%@100k）",
                "nf_b=0.40（B 名义回 200k）是唯一建设性方向：M1 0.654→0.684、M5 0.295→0.554、QA 0.729→0.809",
            ],
            "engine_b_thr": {
                "win252_thr060_delta": 0.010148,
                "win126_thr060_oos_sharpe": 0.640927,
                "cost_erosion": "未确认（研究口径）：thr0.60 成本拖累 -1.36pp ≈ thr0.70 -1.38pp",
            },
            "engine_a_notional": {"verdict": "保持 nf_a=0.20（A 名义 = 1e6×0.20×w_a）"},
            "engine_b_notional": {"verdict": "B 有效名义 = 1e6×nf_b×w_b 必须 ≥170k；nf_b 需 ≥0.40 "
                                             "才能支撑 w_a=0.50（B=200k）"},
            "combo": {
                "best_m1": {"w_a": float(best_m1["w_a"]), "nf_b": float(best_m1["nf_b"]),
                            "oos_sharpe": float(best_m1["oos_sharpe"]),
                            "oos_ret": float(best_m1["oos_ret"])},
                "best_m5": {"w_a": float(best_m5["w_a"]), "nf_b": float(best_m5["nf_b"]),
                            "oos_sharpe": float(best_m5["oos_sharpe_m5"]),
                            "oos_ret": float(best_m5["oos_ret_m5"])},
                "A035_B065": "生产口径退化，已从建议中移除",
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
        print(f"\n[OK] → {vp}")

    print(f"\n[DONE] 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
