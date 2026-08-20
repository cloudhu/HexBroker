"""QA P9 独立复核（fresh-eyes，严过关）：组合网格 + 黑色系敞口 + group_cap。

fresh-eyes 独立复核原则：
- 不调用 p9_combo_validation / p5_engineA_cross_section 的任何重估函数
  （engine_a_targets_cs / engine_a_selection / _capped_selection / combo_stats_row /
  run_engine_row / ferrous_daily_share 均不用）；只用核心框架
  BacktestEngine / CostModel / compute_metrics 与数据加载函数
  （load_prices / load_basis_panel / CONTRACTS18 / INITIAL_CAPITAL），
  目标构建与 cap 逻辑全部自写。
- 口径与工程师完全一致：OOS 2024-07-18 后；按日截面 rank top30% + min=3（S2）；
  滑点1tick+费0.005%+保证金12%+CONTRACTS18；复利口径 = OOS 权益段 total_return。
- 目标：独立复现
  1. 引擎 A S2 OOS Sharpe 0.626 / OOS 复利 +8.68%
  2. 组合 A10/B90 OOS 1.612、A15/B85 OOS 1.602（复利口径）
  3. group_cap=0.5 → 引擎 A OOS 0.646/+9.01%、组合 A15/B85 OOS 1.605、
     黑色系 >50% 天数 16→0
  4. 黑色系敞口统计：full mean 32.8% / p90 60% / >50% 天数 67(12.2%)；
     OOS mean 26.5% / >50% 天数 16(10.0%)
"""
from __future__ import annotations

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
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_basis_panel, load_prices

ART = ROOT / "artifacts"
OOS_START = pd.Timestamp("2024-07-18")
TOP_K = 0.30
MIN_SYMS = 3
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
CAP = 0.5

V8 = ART / "signals_cache18_grouped_v8.parquet"

MULT = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
NOTIONAL = INITIAL_CAPITAL * NOTIONAL_FRAC

# 黑色系合并组映射（与 p9 P9_GROUPS 口径一致：ferrous_raw + ferrous_steel）
P9_GROUP_MAP: dict[str, str] = {
    "i0": "ferrous", "j0": "ferrous", "jm0": "ferrous",
    "rb0": "ferrous", "hc0": "ferrous",
    "cu0": "industrial", "al0": "industrial", "zn0": "industrial", "ni0": "industrial",
    "au0": "precious", "ag0": "precious",
    "y0": "agri_oil", "p0": "agri_oil",
    "m0": "agri_protein",
    "sr0": "agri_soft", "cf0": "agri_soft",
    "ta0": "chem_energy", "sc0": "chem_energy",
}
FERROUS_SYMS = ["i0", "j0", "jm0", "rb0", "hc0"]


# ---------------------------------------------------------------------------
# 1. 独立引擎 A 目标构建（自写）
# ---------------------------------------------------------------------------
def _load_signals(cache_path: Path, prices: pd.DataFrame) -> pd.DataFrame:
    sig = pd.read_parquet(cache_path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(MULT)
    sig["group"] = sig["symbol"].map(lambda s: P9_GROUP_MAP.get(s, "other"))
    return sig


def build_engine_a_targets(prices: pd.DataFrame, cap: float | None = None) -> pd.DataFrame:
    """自写引擎 A S2 targets；cap=None 等价生产（top_k 截面 + min=3）。"""
    sig = _load_signals(V8, prices)
    if cap is None:
        cond = (sig["rank_pct"] >= 1.0 - TOP_K) & sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)
        sig["selected"] = cond
    else:
        sig["selected"] = _apply_group_cap(sig, cap)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = NOTIONAL / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(sig["selected"], raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 2. 独立 group_cap 实现（自写，不调用 p5._capped_selection）
# ---------------------------------------------------------------------------
def _apply_group_cap(sig: pd.DataFrame, cap: float) -> pd.Series:
    """对每日候选施加单组敞口上限 cap。

    思路（与工程师文档化口径一致，实现独立）：
      每日按 exp_ret 降序扫描候选：
       - 优先保留"组占比 <= cap"的品种（满 target_count 停止）；
       - 若某组已被选到上限，其后该组品种跳过（按 exp_ret 剔除超出部分）；
       - 稀疏兜底：若某日最后只剩 1 个候选（任意组），允许保留避免空仓退化。
    target_count = 当日 top_k 截面应选数量（与无 cap 等量，名义可比）。
    """
    selected = pd.Series(False, index=sig.index)
    elig = sig[sig["_px"].notna() & (sig["_day_cnt"] >= MIN_SYMS)]
    for _ts, g in elig.groupby("ts"):
        g = g.sort_values("exp_ret", ascending=False)
        target_count = int((g["rank_pct"] >= 1.0 - TOP_K).sum())
        if target_count <= 0:
            continue
        counts: dict[str, int] = {}
        chosen: list[str] = []
        for sym in g["symbol"]:
            grp = P9_GROUP_MAP.get(sym, "other")
            n_grp = counts.get(grp, 0)
            # 允许该只的条件：组占比不超过 cap（新增后），或当前是整日第 1 只（稀疏兜底）
            ok = (n_grp + 1) / (len(chosen) + 1) <= cap or len(chosen) == 0
            if ok:
                chosen.append(sym)
                counts[grp] = n_grp + 1
                if len(chosen) >= target_count:
                    break
        selected.loc[g.index[g["symbol"].isin(chosen)]] = True
    return selected


# ---------------------------------------------------------------------------
# 3. 独立引擎 B 目标构建（自写）
# ---------------------------------------------------------------------------
def build_engine_b_targets(prices: pd.DataFrame) -> pd.DataFrame:
    basis = load_basis_panel()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(BASIS_WIN, min_periods=60).rank(pct=True)
    )
    sig = basis[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= BASIS_THR,
        (NOTIONAL / (sig["_px"] * sym_level.map(MULT))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 4. 独立回测 + 组合指标（复利口径）
# ---------------------------------------------------------------------------
def run_backtest(cfg, cost, prices, targets) -> tuple[pd.Series, object, object]:
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return ret, m, m_oos


def combo_stats(ret_a: pd.Series, ret_b: pd.Series, w_a: float) -> dict:
    idx = ret_a.index.intersection(ret_b.index)
    comb = w_a * ret_a.loc[idx] + (1.0 - w_a) * ret_b.loc[idx]
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    oos_eq = eq[pd.to_datetime(eq.index) >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "sharpe_full": float(m.sharpe),
        "maxdd_full": float(m.max_drawdown),
        "oos_sharpe": float(m_oos.sharpe) if m_oos else np.nan,
        "oos_ret": float(m_oos.total_return) if m_oos else np.nan,
        "oos_maxdd": float(m_oos.max_drawdown) if m_oos else np.nan,
    }


# ---------------------------------------------------------------------------
# 5. 独立黑色系敞口统计
# ---------------------------------------------------------------------------
def ferrous_exposure(sig: pd.DataFrame) -> pd.DataFrame:
    s = sig[sig["selected"]]
    daily = s.groupby("ts").agg(
        n_selected=("symbol", "size"),
        n_ferrous=("group", lambda x: int((x == "ferrous").sum())),
    )
    daily["ferrous_share"] = daily["n_ferrous"] / daily["n_selected"]
    daily = daily.reset_index()
    daily["ts"] = pd.to_datetime(daily["ts"])
    return daily


def exposure_summary(daily: pd.DataFrame, tag: str) -> list[dict]:
    out = []
    for name, lo, hi in [("full", pd.Timestamp("2000-01-01"), None),
                         ("is", pd.Timestamp("2000-01-01"), OOS_START),
                         ("oos", OOS_START, None)]:
        d = daily[daily["ts"] >= lo]
        if hi is not None:
            d = d[d["ts"] < hi]
        if len(d) == 0:
            continue
        sh = d["ferrous_share"]
        n_over = int((sh > 0.5).sum())
        out.append({
            "tag": tag, "segment": name, "n_days": len(d),
            "mean_share": float(sh.mean()), "p90_share": float(sh.quantile(0.90)),
            "max_share": float(sh.max()),
            "days_over_50pct": n_over,
            "pct_days_over_50pct": float(n_over / len(d) * 100),
        })
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 100)
    print("QA P9 独立复核（fresh-eyes）：组合网格 + 黑色系敞口 + group_cap")
    print("口径：OOS 2024-07-18 后；截面 rank top30% + S2 min=3；复利口径；目标构建全部自写")
    print("=" * 100)

    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"[env] prices={len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"v8 缓存存在={V8.exists()}")

    # ---- 引擎 B ----
    tgt_b = build_engine_b_targets(prices)
    ret_b, m_b, m_b_oos = run_backtest(cfg, cost, prices, tgt_b)
    print(f"\n[引擎 B] 全样本 Sharpe={m_b.sharpe:.3f} | OOS Sharpe={m_b_oos.sharpe:.3f} "
          f"OOS 复利={m_b_oos.total_return*100:+.2f}%")

    # ---- 引擎 A 基线（cap=None）----
    tgt_a = build_engine_a_targets(prices, cap=None)
    ret_a, m_a, m_a_oos = run_backtest(cfg, cost, prices, tgt_a)
    print(f"\n[引擎 A v8 S2 基线] 全样本 Sharpe={m_a.sharpe:.3f} 年化={m_a.annual_return*100:+.1f}% "
          f"MaxDD={m_a.max_drawdown*100:.1f}%")
    print(f"  OOS Sharpe={m_a_oos.sharpe:.3f} OOS 复利={m_a_oos.total_return*100:+.2f}% "
          f"OOS MaxDD={m_a_oos.max_drawdown*100:.1f}%  (工程师声明 0.626 / +8.68%)")

    # ---- 组合 A10/B90 与 A15/B85（vol=N）----
    print("\n[组合 vol=N（复利口径）]")
    combos = {}
    for w_a in (0.10, 0.15):
        c = combo_stats(ret_a, ret_b, w_a)
        combos[w_a] = c
        print(f"  A{w_a:.2f}/B{1-w_a:.2f}: OOS Sharpe={c['oos_sharpe']:.3f} "
              f"OOS 复利={c['oos_ret']*100:+.2f}% OOS MaxDD={c['oos_maxdd']*100:.1f}% "
              f"| 全样本 Sharpe={c['sharpe_full']:.3f} MaxDD={c['maxdd_full']*100:.1f}%")
    print("  （工程师声明 A10/B90 1.612 / +28.70%；A15/B85 1.602 / +27.54%）")

    # ---- 黑色系敞口（基线，独立统计）----
    print("\n[黑色系敞口统计（基线 cap=None，独立实现）]")
    sig_base = _load_signals(V8, prices)
    sig_base["selected"] = (sig_base["rank_pct"] >= 1.0 - TOP_K) & \
        sig_base["_px"].notna() & (sig_base["_day_cnt"] >= MIN_SYMS)
    daily_base = ferrous_exposure(sig_base)
    for r in exposure_summary(daily_base, "baseline"):
        print(f"  [{r['segment']:<4}] mean={r['mean_share']*100:5.1f}% p90={r['p90_share']*100:5.1f}% "
              f"max={r['max_share']*100:5.1f}% | >50% 天数={r['days_over_50pct']:>3} "
              f"({r['pct_days_over_50pct']:5.1f}%)")
    print("  （工程师声明 full mean 32.8% / p90 60% / >50% 67 天(12.2%)；OOS mean 26.5% / >50% 16 天(10.0%)）")

    # ---- group_cap=0.5（独立实现）----
    print(f"\n[group_cap={CAP}（独立实现）]")
    tgt_a_cap = build_engine_a_targets(prices, cap=CAP)
    ret_a_cap, m_a_cap, m_a_cap_oos = run_backtest(cfg, cost, prices, tgt_a_cap)
    print(f"  引擎 A cap0.5: 全样本 Sharpe={m_a_cap.sharpe:.3f} | "
          f"OOS Sharpe={m_a_cap_oos.sharpe:.3f} OOS 复利={m_a_cap_oos.total_return*100:+.2f}% "
          f"OOS MaxDD={m_a_cap_oos.max_drawdown*100:.1f}%")
    print(f"  （工程师声明 0.646 / +9.01%）")

    sig_cap = _load_signals(V8, prices)
    sig_cap["selected"] = _apply_group_cap(sig_cap, CAP)
    daily_cap = ferrous_exposure(sig_cap)
    for r in exposure_summary(daily_cap, "cap0.5"):
        print(f"  [cap0.5 {r['segment']:<4}] mean={r['mean_share']*100:5.1f}% "
              f"p90={r['p90_share']*100:5.1f}% max={r['max_share']*100:5.1f}% | "
              f">50% 天数={r['days_over_50pct']:>3} ({r['pct_days_over_50pct']:5.1f}%)")
    print("  （工程师声明 cap 后 >50% 天数 16→0）")

    # 组合 A15/B85 + cap
    for w_a in (0.15,):
        c_base = combo_stats(ret_a, ret_b, w_a)
        c_cap = combo_stats(ret_a_cap, ret_b, w_a)
        print(f"\n  组合 A{w_a:.2f}/B{1-w_a:.2f} cap0.5 vs 基线: "
              f"OOS Sharpe {c_base['oos_sharpe']:.3f} → {c_cap['oos_sharpe']:.3f} "
              f"(Δ{c_cap['oos_sharpe']-c_base['oos_sharpe']:+.3f}) | "
              f"OOS 复利 {c_base['oos_ret']*100:+.2f}% → {c_cap['oos_ret']*100:+.2f}%")
    print("  （工程师声明组合 A15/B85 OOS 1.602 → 1.605）")

    # ---- 汇总表 ----
    rows = [
        {"item": "engineA_S2_oos_sharpe", "engineer": 0.626, "qa": float(m_a_oos.sharpe)},
        {"item": "engineA_S2_oos_compound", "engineer": 0.0868, "qa": float(m_a_oos.total_return)},
        {"item": "combo_A10B90_oos_sharpe", "engineer": 1.612, "qa": combos[0.10]["oos_sharpe"]},
        {"item": "combo_A10B90_oos_compound", "engineer": 0.2870, "qa": combos[0.10]["oos_ret"]},
        {"item": "combo_A15B85_oos_sharpe", "engineer": 1.602, "qa": combos[0.15]["oos_sharpe"]},
        {"item": "combo_A15B85_oos_compound", "engineer": 0.2754, "qa": combos[0.15]["oos_ret"]},
        {"item": "capA_oos_sharpe", "engineer": 0.646, "qa": float(m_a_cap_oos.sharpe)},
        {"item": "capA_oos_compound", "engineer": 0.0901, "qa": float(m_a_cap_oos.total_return)},
        {"item": "cap_combo_A15B85_oos_sharpe", "engineer": 1.605, "qa": combo_stats(ret_a_cap, ret_b, 0.15)["oos_sharpe"]},
    ]
    tbl = pd.DataFrame(rows)
    tbl["delta"] = (tbl["qa"] - tbl["engineer"]).round(4)
    tbl["match"] = tbl["delta"].abs() < 0.005
    print("\n" + "=" * 100)
    print("QA 独立复算 vs 工程师声明（abs delta < 0.005 判定一致）")
    print(tbl.round(4).to_string(index=False))
    tbl.to_csv(ART / "qa_p9_independent_verify.csv", index=False)
    print(f"[OK] → {ART / 'qa_p9_independent_verify.csv'}")

    # 敞口汇总
    expo_rows = exposure_summary(daily_base, "baseline") + exposure_summary(daily_cap, "cap0.5")
    pd.DataFrame(expo_rows).to_csv(ART / "qa_p9_ferrous_exposure.csv", index=False)
    print(f"[OK] → {ART / 'qa_p9_ferrous_exposure.csv'}")

    print("\n[DONE] QA P9 独立复核完成")


if __name__ == "__main__":
    main()
