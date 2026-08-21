"""QA P13 Round 2 fresh-eyes 独立验证（严过关）：cap 口径修正 + 终裁确认。

与工程师实现（scripts/p13_multimodel.py）的独立性声明：
- 不调用 p13 的 evaluate_variant / build_fused_caches / engine_a_targets_cs；
- target 构建自写（capped_selection，P9-2 单组敞口上限语义）；
- 只用 BacktestEngine / CostModel / compute_metrics 核心引擎与数据加载；
- 显式加载 configs/base.yaml（与修正后的生产口径一致：group_cap=0.5 + ferrous_all）。

验证项：
  A. 显式 base.yaml 读取：group_cap=0.5 / group_map 18 键 / ferrous_all 合并确认。
  B. 独立 v8/F2/F3 引擎 A S2 回测（自写 target）→ 与工程师复跑 compare.csv 逐位对比
     （v8 0.646 / F2 0.597 / 组合 1.614 / 1.612 / OOS IC -0.0640 / -0.0606）。
  C. 独立组合 A10/B90（自写权重加权）→ 与 compare.csv 组合数字对比。
  D. 机制验证：cap vs 无 cap 下 F2 的 ferrous_all 组持仓占比 → 黑色系敞口被 cap 约束。
  E. 终裁规则复现：make_verdict 在 cap 口径下 is_pass=false / adopted_fusion=null。

口径铁律：复利口径；OOS 2024-07-18 后；滑点1tick+费0.005%+保证金12%+CONTRACTS18。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import BASIS_THR, BASIS_WIN, OOS_START, engine_b_targets

ART = ROOT / "artifacts"
IS_END = pd.Timestamp("2022-04-21")
TOP_K = 0.30
MIN_SYMS = 3
NOTIONAL_FRAC = 0.20
W_A = 0.10
W_B = 0.90
HORIZON = 5

CACHES = {
    "v8": ART / "signals_cache18_grouped_v8.parquet",
    "F2": ART / "signals_cache18_grouped_v9_f2.parquet",
    "F3": ART / "signals_cache18_grouped_v9_f3.parquet",
}
MULT = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
NOTIONAL = INITIAL_CAPITAL * NOTIONAL_FRAC

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name} {detail}")


def realized_returns(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"])


# ---------------------------------------------------------------------------
# A. 显式 base.yaml 生产口径
# ---------------------------------------------------------------------------
def load_prod_config():
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    ea = cfg.backtest.engine_a
    gcap = float(ea.group_cap) if getattr(ea, "group_cap", None) else None
    gmap = dict(ea.group_map) if getattr(ea, "group_map", None) else None
    return cfg, gcap, gmap


# ---------------------------------------------------------------------------
# B/C. 独立 target 构建（自写 cap 算法）+ 回测 + 组合
# ---------------------------------------------------------------------------
def capped_selection(ranked: list[str], target_count: int, cap: float,
                     group_map: dict[str, str]) -> list[str]:
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts: dict[str, int] = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g: str, n: int, total: int) -> bool:
        return total > 0 and n / total > cap

    changed = True
    while changed and len(selected) > 1:
        changed = False
        for i in range(len(selected) - 1, -1, -1):
            g = group_map.get(selected[i], "other")
            if over_cap(g, counts[g], len(selected)) and counts[g] >= 2:
                counts[g] -= 1
                selected.pop(i)
                changed = True
                break
    sel_set = set(selected)
    for s in ranked:
        if len(selected) >= target_count:
            break
        if s in sel_set:
            continue
        g = group_map.get(s, "other")
        if (counts.get(g, 0) + 1) / (len(selected) + 1) <= cap:
            selected.append(s)
            counts[g] = counts.get(g, 0) + 1
            sel_set.add(s)
    return selected


def build_targets(cache_path: Path, prices: pd.DataFrame, gmap: dict[str, str],
                  gcap: float | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (targets, 逐日选仓明细[ts, symbol, group])。"""
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    sig["_mult"] = sig["symbol"].map(MULT)
    sig["group"] = sig["symbol"].map(lambda s: gmap.get(s, "other"))
    base_cond = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)
    if gcap is None:
        sig["selected"] = base_cond
    else:
        sig["selected"] = False
        elig = sig[sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)]
        for _ts, g in elig.groupby("ts"):
            g = g.sort_values("exp_ret", ascending=False)
            target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
            if target_count <= 0:
                continue
            sel_syms = capped_selection(g["symbol"].tolist(), target_count, gcap, gmap)
            sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = NOTIONAL / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(sig["selected"], raw_lots.fillna(0.0).astype(int), 0)
    detail = sig[sig["selected"]][["ts", "symbol", "group"]].copy()
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index(), detail


def run_engine(cfg, cost, prices, targets: pd.DataFrame):
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, eq, m, m_oos


def combo_ind(ret_a: pd.Series, ret_b: pd.Series, w_a: float) -> dict:
    ra, rb = ret_a.align(ret_b, join="inner")
    comb = w_a * ra + (1.0 - w_a) * rb
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= pd.Timestamp(OOS_START)]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {"sharpe_full": m.sharpe,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan}


def oos_cs_ic_ind(cache_path: Path, realized: pd.Series) -> dict:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized.reindex(sig.index)
    sig = sig.dropna(subset=["realized"])
    sig = sig[sig.index.get_level_values(1) >= pd.Timestamp(OOS_START)]

    def daily_ic(g: pd.DataFrame) -> float:
        if len(g) < 3:
            return np.nan
        return float(g["exp_ret"].corr(g["realized"], method="spearman"))

    ics = sig.groupby(level="ts").apply(daily_ic).dropna()
    return {"n_days": len(ics), "ic_mean": float(ics.mean()) if len(ics) else np.nan}


def main() -> None:
    print("=" * 96)
    print("QA P13 Round 2 fresh-eyes：cap 口径修正 + 终裁确认（自写 target + 显式 base.yaml）")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | A{W_A:.2f}/B{W_B:.2f}")
    print("=" * 96)

    cfg, gcap, gmap = load_prod_config()
    check("显式 base.yaml group_cap=0.5", gcap == 0.5, f"group_cap={gcap!r}")
    check("显式 base.yaml group_map 18 键", gmap is not None and len(gmap) == 18,
          f"keys={len(gmap) if gmap else 0}")
    fer = [s for s in ("i0", "j0", "jm0", "rb0", "hc0") if gmap.get(s) == "ferrous_all"]
    check("ferrous_all 合并 5 键", len(fer) == 5, f"{fer}")
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, _, _, _ = run_engine(cfg, cost, prices, tgt_b)

    ship = pd.read_csv(ART / "p13_multimodel_compare.csv")
    print("\n[B/C] 独立 cap 口径重跑 vs 工程师 compare.csv（复利口径）")
    for name in ("v8", "F2", "F3"):
        tgt, detail = build_targets(CACHES[name], prices, gmap, gcap)
        ret_a, eq_a, m_a, m_oos_a = run_engine(cfg, cost, prices, tgt)
        combo = combo_ind(ret_a, ret_b, W_A)
        ic = oos_cs_ic_ind(CACHES[name], realized)
        s_ship = ship[(ship["variant"] == name) & (ship["engine"] == "A-S2")].iloc[0]
        c_ship = ship[(ship["variant"] == name) & (ship["engine"] == "A0.10/B0.90-volN")].iloc[0]
        print(f"    [{name}] ind OOS_Sharpe={m_oos_a.sharpe:.4f} ship={s_ship['oos_sharpe']:.4f} "
              f"| ind OOS_ret={m_oos_a.total_return:.4f} ship={s_ship['oos_ret']:.4f} "
              f"| ind combo={combo['oos_sharpe']:.4f} ship={c_ship['oos_sharpe']:.4f} "
              f"| ind IC={ic['ic_mean']:+.4f} ship={s_ship['oos_cs_ic']:+.4f}")
        check(f"{name} 单引擎 OOS Sharpe 复现", abs(m_oos_a.sharpe - s_ship["oos_sharpe"]) < 1e-9,
              f"Δ={abs(m_oos_a.sharpe - s_ship['oos_sharpe']):.2e}")
        check(f"{name} OOS 复利复现", abs(m_oos_a.total_return - s_ship["oos_ret"]) < 1e-9,
              f"Δ={abs(m_oos_a.total_return - s_ship['oos_ret']):.2e}")
        check(f"{name} 组合 OOS Sharpe 复现", abs(combo["oos_sharpe"] - c_ship["oos_sharpe"]) < 1e-9,
              f"Δ={abs(combo['oos_sharpe'] - c_ship['oos_sharpe']):.2e}")
        check(f"{name} OOS 截面 IC 复现", abs(ic["ic_mean"] - s_ship["oos_cs_ic"]) < 1e-9,
              f"Δ={abs(ic['ic_mean'] - s_ship['oos_cs_ic']):.2e}")

    # Round 1 独立 cap 重跑数字交叉核对（严过关 Round 1 报告 §5.1）
    print("\n[交叉] 与 QA Round 1 独立 cap 重跑数字核对（0.646 / 0.597 / 1.614 / 1.612）")
    r1 = {"v8": {"oos_sharpe": 0.646, "combo": 1.614},
          "F2": {"oos_sharpe": 0.597, "combo": 1.612}}
    for name, exp in r1.items():
        s_ship = ship[(ship["variant"] == name) & (ship["engine"] == "A-S2")].iloc[0]
        c_ship = ship[(ship["variant"] == name) & (ship["engine"] == "A0.10/B0.90-volN")].iloc[0]
        check(f"{name} OOS Sharpe 与 Round1 一致", abs(s_ship["oos_sharpe"] - exp["oos_sharpe"]) < 0.001,
              f"ship={s_ship['oos_sharpe']:.3f} r1={exp['oos_sharpe']:.3f}")
        check(f"{name} 组合与 Round1 一致", abs(c_ship["oos_sharpe"] - exp["combo"]) < 0.001,
              f"ship={c_ship['oos_sharpe']:.3f} r1={exp['combo']:.3f}")

    # D. 机制验证：cap vs 无 cap 下 F2 黑色系（ferrous_all）持仓占比
    print("\n[D] 机制验证：F2 ferrous_all 组持仓占比（cap vs 无 cap，OOS 段）")
    oos_start = pd.Timestamp(OOS_START)
    for name in ("v8", "F2"):
        for tag, gcap_i in (("cap", gcap), ("nocap", None)):
            tgt, detail = build_targets(CACHES[name], prices, gmap, gcap_i)
            detail = detail[detail["ts"] >= oos_start]
            fer_share = (detail["group"] == "ferrous_all").mean()
            n_days = detail["ts"].nunique()
            avg_syms = detail.groupby("ts")["symbol"].nunique().mean()
            print(f"      {name} {tag}: ferrous_all 持仓占比={fer_share*100:.1f}% "
                  f"（OOS {n_days} 天，日均 {avg_syms:.1f} 品种）")

    # E. 终裁规则复现
    print("\n[E] 终裁规则复现（cap 口径）")
    from scripts.p13_multimodel import make_verdict
    ic_tbl = pd.read_csv(ART / "p13_model_ic.csv")
    v = make_verdict(ship, ic_tbl)
    ship_v = json.loads((ART / "p13_verdict.json").read_text(encoding="utf-8"))
    check("终裁 is_pass=false / adopted=null", v["is_pass"] is False and v["adopted_fusion"] is None,
          f"is_pass={v['is_pass']} adopted={v['adopted_fusion']}")
    check("落盘 verdict 与规则复现一致",
          v["is_pass"] == ship_v["is_pass"] and v["adopted_fusion"] == ship_v["adopted_fusion"],
          f"ship_pass={ship_v['is_pass']} ship_adopted={ship_v['adopted_fusion']}")

    print("\n" + "=" * 96)
    n_pass = sum(1 for _, s, _ in _results if s == PASS)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    print(f"QA P13 Round 2 独立验证汇总：PASS={n_pass} FAIL={n_fail}")
    for name, s, detail in _results:
        print(f"  [{s}] {name} {detail}")
    print("=" * 96)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
