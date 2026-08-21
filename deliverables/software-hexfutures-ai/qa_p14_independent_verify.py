"""QA P14 独立复核（fresh-eyes，不调用 p14 编排函数）。

复核范围（P14 引擎 A 任务对齐修复：分类目标 + 校准器修复）：
  1. 缓存完整性：v8 / v10_cls / v10_cal 行数=8624、18 品种、ts 范围一致
  2. 校准生效独立验证：v10_cal vs v8 的 exp_ret 逐字节一致率 / p_up 不一致率 /
     mean|Δp_up|（应 ~100% / ~0.37-0.42）
  3. OOS 截面 IC 独立复现：自写每日截面 Spearman（p_up vs 5 日 fwd 收益），
     不调用 p14 的 cs_ic_on_score（期望 OOS IC ≈ +0.1395 / ICIR ≈ 0.363 / pos≈64%）
  4. 整数手 floor 排除高价合约独立验证：NOTIONAL=200k，逐合约 OOS 手数 =
     floor(200000/(px*mult)) → au0/cu0/i0/j0/sc0 是否结构性 0 手；
     per-symbol OOS IC 是否集中在被排除合约（p_up: cu0/au0/sc0; v8: rb0/jm0/m0）
  5. 等权 top30% OOS 1 日持有 Sharpe 独立复现（p_up 信号层 ≈2.98 gross）
  6. "选中但不可交易"持仓 1 日收益对比（可交易 vs 不可交易）

口径：OOS 2024-07-18 后；复利口径；同 v8 口径仅标签/选择变量不同。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import NOTIONAL_FRAC, OOS_START, TOP_K

ART = ROOT / "artifacts"
OOS = pd.Timestamp(OOS_START)
HORIZON = 5  # v8 口径

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """自写 Spearman：秩相关（不依赖 scipy）。"""
    if len(a) < 3:
        return np.nan
    ra = pd.Series(a).rank(method="average").to_numpy()
    rb = pd.Series(b).rank(method="average").to_numpy()
    if ra.std(ddof=0) == 0 or rb.std(ddof=0) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def load(name: str) -> pd.DataFrame:
    df = pd.read_parquet(ART / f"signals_cache18_grouped_{name}.parquet")
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def realized_returns(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.Series:
    """5 日已实现收益（独立实现，与 p8_3 同语义）。"""
    parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-horizon) / close - 1.0
        r.name = "realized"
        parts.append(r)
    return pd.concat(parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"]
    )


def daily_cs_ic(score: pd.Series, realized: pd.Series, oos_start: pd.Timestamp) -> dict:
    """自写每日截面 Spearman IC（跨品种），返回 full/is/oos 统计。"""
    d = pd.DataFrame({"score": score, "realized": realized})
    d = d.dropna(subset=["realized"])
    ics = []
    for ts, g in d.groupby(level="ts"):
        if len(g) < 3:
            continue
        ic = spearman(g["score"].to_numpy(), g["realized"].to_numpy())
        if not np.isnan(ic):
            ics.append((ts, ic))
    s = pd.Series({ts: ic for ts, ic in ics}).sort_index()
    s.name = "ic"

    def summ(sub: pd.Series) -> dict:
        if len(sub) == 0:
            return {"n_days": 0, "ic_mean": np.nan, "icir": np.nan, "pos_ratio": np.nan}
        sd = float(sub.std(ddof=1)) if len(sub) > 1 else 0.0
        return {
            "n_days": int(len(sub)),
            "ic_mean": float(sub.mean()),
            "icir": float(sub.mean() / sd) if sd > 0 else np.nan,
            "pos_ratio": float((sub > 0).mean()),
        }

    return {
        "full": summ(s),
        "is": summ(s[s.index < pd.Timestamp("2022-04-21")]),
        "oos": summ(s[s.index >= oos_start]),
    }


def per_symbol_oos_ic(score: pd.Series, realized: pd.Series, oos_start: pd.Timestamp) -> pd.Series:
    """per-symbol OOS 时序 IC（品种内 Spearman，OOS 段）。"""
    d = pd.DataFrame({"score": score, "realized": realized})
    d = d[d.index.get_level_values("ts") >= oos_start].dropna(subset=["realized"])
    out = {}
    for sym, g in d.groupby(level="symbol"):
        if len(g) < 20:
            continue
        ic = spearman(g["score"].to_numpy(), g["realized"].to_numpy())
        out[sym] = ic
    return pd.Series(out).sort_values(ascending=False)


def main() -> None:
    print("=" * 100)
    print("QA P14 独立复核（fresh-eyes）")
    print("=" * 100)

    prices = load_prices()
    realized5 = realized_returns(prices)
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    print(f"prices {len(prices)} 行 | realized5 {len(realized5)} 行 | "
          f"OOS={OOS.date()} | notional/手={notional:,.0f} | top_k={TOP_K}")

    # ---------------------------------------------------------------
    # 1. 缓存完整性
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[1] 缓存完整性（v8 / v10_cls / v10_cal）")
    print("=" * 100)
    caches = {n: load(n) for n in ("v8", "v10_cls", "v10_cal")}
    for n, df in caches.items():
        print(f"  {n:<8} rows={len(df):>5} symbols={df['symbol'].nunique():>2} "
              f"ts {df['ts'].min().date()} -> {df['ts'].max().date()} "
              f"cols={list(df.columns)}")

    # ---------------------------------------------------------------
    # 2. 校准生效独立验证（v10_cal vs v8）
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[2] 校准生效独立验证（v10_cal vs v8）")
    print("=" * 100)
    v8, v10_cal = caches["v8"], caches["v10_cal"]
    m = v8.merge(v10_cal, on=["symbol", "ts"], suffixes=("_v8", "_cal"), how="inner")
    exp_exact = float((m["exp_ret_v8"] == m["exp_ret_cal"]).mean())
    exp_close = float(np.isclose(m["exp_ret_v8"], m["exp_ret_cal"], rtol=1e-9, atol=1e-12).mean())
    exp_maxdiff = float(np.abs(m["exp_ret_v8"] - m["exp_ret_cal"]).max())
    pup_diff = float((np.abs(m["p_up_v8"] - m["p_up_cal"]) > 1e-9).mean())
    pup_mad = float(np.abs(m["p_up_v8"] - m["p_up_cal"]).mean())
    pup_mad_max = float(np.abs(m["p_up_v8"] - m["p_up_cal"]).max())
    print(f"  n_align={len(m)}")
    print(f"  exp_ret 逐字节一致率（==）      = {exp_exact*100:.4f}%")
    print(f"  exp_ret isclose(1e-9/1e-12)    = {exp_close*100:.4f}%  max|Δexp_ret|={exp_maxdiff:.3e}")
    print(f"  p_up 不一致率(>1e-9)           = {pup_diff*100:.2f}%")
    print(f"  mean|Δp_up|                    = {pup_mad:.4f}  max|Δp_up|={pup_mad_max:.4f}")
    per_sym = m.groupby("symbol").apply(
        lambda g: pd.Series({
            "n": len(g),
            "exp_exact": float((g["exp_ret_v8"] == g["exp_ret_cal"]).mean()),
            "pup_diff": float((np.abs(g["p_up_v8"] - g["p_up_cal"]) > 1e-9).mean()),
            "mad": float(np.abs(g["p_up_v8"] - g["p_up_cal"]).mean()),
        }), include_groups=False
    )
    print("\n  per-symbol 汇总（独立计算）：")
    print(per_sym.round(4).to_string())
    # v8 p_up 是否确实为原始 MC 概率（1/30 倍数）→ 印证校准从未发生
    pv8 = v8["p_up"].to_numpy()
    grid = np.round(np.arange(0, 31) / 30.0, 6)
    on_grid = float(np.isin(np.round(pv8, 6), grid).mean())
    print(f"\n  [旁证] v8 p_up 落在 k/30 网格比例 = {on_grid*100:.1f}% "
          f"（高→v8 p_up 是 MC 原始概率，未校准）")
    pcal = v10_cal["p_up"].to_numpy()
    on_grid_cal = float(np.isin(np.round(pcal, 6), grid).mean())
    print(f"  [旁证] v10_cal p_up 落在 k/30 网格比例 = {on_grid_cal*100:.1f}% "
          f"（低→已 Platt 校准，连续值）")

    # ---------------------------------------------------------------
    # 3. OOS 截面 IC 独立复现（p_up）
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[3] OOS 截面 IC 独立复现（v10_cal p_up / v8 exp_ret / v10_cls exp_ret）")
    print("=" * 100)
    for name, col in (("v8", "exp_ret"), ("v10_cls", "exp_ret"),
                      ("v10_cal", "exp_ret"), ("v10_cal", "p_up")):
        sig = caches[name].set_index(["symbol", "ts"]).sort_index()
        sc = sig[col]
        summ = daily_cs_ic(sc, realized5.reindex(sc.index), OOS)
        print(f"  {name:<8} col={col:<8} "
              f"full IC={summ['full']['ic_mean']:+.4f} (n={summ['full']['n_days']}) | "
              f"IS  IC={summ['is']['ic_mean']:+.4f} (n={summ['is']['n_days']}) | "
              f"OOS IC={summ['oos']['ic_mean']:+.4f} ICIR={summ['oos']['icir']:+.3f} "
              f"pos={summ['oos']['pos_ratio']*100:.1f}% n={summ['oos']['n_days']}")

    # ---------------------------------------------------------------
    # 4. 整数手 floor 排除 + per-symbol OOS IC
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[4] 整数手 floor 排除高价合约 + per-symbol OOS IC")
    print("=" * 100)
    mult = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    oos_price = prices[prices.index.get_level_values("datetime") >= OOS]
    lot_rows = []
    for sym in SYMBOLS18:
        ser = oos_price.xs(sym, level=0)["close"]
        lots = (notional / (ser * mult[sym])).astype(int)
        lot_rows.append({
            "sym": sym, "mult": mult[sym],
            "px_oos_med": float(ser.median()),
            "notional_per_lot_med": float((ser * mult[sym]).median()),
            "lots_min": int(lots.min()), "lots_max": int(lots.max()),
            "zero_frac": float((lots == 0).mean()),
            "struct_zero": bool((lots == 0).all()),
        })
    lot_tbl = pd.DataFrame(lot_rows).set_index("sym")
    print("  OOS 逐合约 floor 手数（notional=200k）：")
    print(lot_tbl.round(2).to_string())
    zero_syms = lot_tbl.index[lot_tbl["struct_zero"]].tolist()
    print(f"\n  → 结构性 0 手（OOS 全程 0 手）合约: {sorted(zero_syms)}")

    print("\n  per-symbol OOS 时序 IC（品种内 Spearman，OOS 段, n>=20）：")
    for name, col in (("v8", "exp_ret"), ("v10_cal", "p_up"), ("v10_cls", "exp_ret")):
        sig = caches[name].set_index(["symbol", "ts"]).sort_index()
        sc = sig[col]
        ps = per_symbol_oos_ic(sc, realized5.reindex(sc.index), OOS)
        top = ps.head(6)
        print(f"  --- {name} col={col} ---")
        print(f"      top6 IC: " + ", ".join(f"{k} {v:+.3f}" for k, v in top.items()))
        print(f"      全部: " + ", ".join(f"{k} {v:+.3f}" for k, v in ps.sort_index().items()))
        # 被排除合约的 IC 均值 vs 可交易合约
        z = ps.reindex([s for s in zero_syms if s in ps.index]).dropna()
        tr = ps.drop(index=[s for s in zero_syms if s in ps.index])
        print(f"      被排除(0手)合约 IC 均值={z.mean():+.4f} (n={len(z)}) | "
              f"可交易合约 IC 均值={tr.mean():+.4f} (n={len(tr)})")

    # ---------------------------------------------------------------
    # 5. 等权 top30% OOS 1 日持有 Sharpe（信号层）
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[5] 等权 top30% OOS 1 日持有（信号层，自写）")
    print("=" * 100)
    # 1 日 fwd 收益（每品种 close.shift(-1)/close-1）
    ret1_parts = []
    for sym in prices.index.get_level_values(0).unique():
        close = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        r = close.shift(-1) / close - 1.0
        r.name = "ret1"
        ret1_parts.append(r)
    ret1 = pd.concat(ret1_parts, keys=prices.index.get_level_values(0).unique()).rename_axis(
        ["symbol", "datetime"]
    )

    def ew_top30_oos(name: str, col: str, cost_per_side: float = 0.0) -> dict:
        sig = caches[name].set_index(["symbol", "ts"]).sort_index()
        sc = sig[col]
        d = pd.DataFrame({"score": sc, "ret1": ret1.reindex(sc.index)})
        d = d[d.index.get_level_values("ts") >= OOS].dropna(subset=["score", "ret1"])
        daily = []
        for ts, g in d.groupby(level="ts"):
            if len(g) < 3:
                continue
            g = g.sort_values("score", ascending=False)
            n_sel = max(1, int(np.ceil(len(g) * TOP_K)))
            sel = g.iloc[:n_sel]
            gross = float(sel["ret1"].mean())
            # 近似成本：每选中合约每边 1tick+0.005%（买入+卖出=2边）；按 price 折算 tick
            c = 0.0
            if cost_per_side > 0:
                px = prices.xs(sel.index.get_level_values(0), level=0)  # 占位，下面单独算
            daily.append((ts, gross, float(sel["ret1"].mean() - g["ret1"].mean())))
        s = pd.DataFrame(daily, columns=["ts", "ret", "edge"]).set_index("ts")
        sharpe = float(s["ret"].mean() / s["ret"].std(ddof=1) * np.sqrt(252)) if len(s) > 1 else np.nan
        total = float((1.0 + s["ret"]).prod() - 1.0)
        return {"n_days": len(s), "sharpe": sharpe, "total_ret": total,
                "mean_daily": float(s["ret"].mean() * 100), "edge_pp": float(s["edge"].mean() * 100),
                "std_daily": float(s["ret"].std(ddof=1) * 100)}

    for name, col in (("v8", "exp_ret"), ("v10_cal", "p_up"), ("v10_cls", "exp_ret")):
        r = ew_top30_oos(name, col)
        print(f"  {name:<8} col={col:<8} OOS 1日等权top30%: Sharpe={r['sharpe']:+.2f} "
              f"复利={r['total_ret']*100:+.1f}% 日均={r['mean_daily']:+.3f}% "
              f"edge={r['edge_pp']:+.3f}pp/日 n={r['n_days']}")

    # 带成本版本（1tick 滑点 + 0.005% 费，双边，1 日持有 → 每合约每天 2 边成本）
    def ew_top30_cost(name: str, col: str) -> dict:
        sig = caches[name].set_index(["symbol", "ts"]).sort_index()
        sc = sig[col]
        d = pd.DataFrame({"score": sc, "ret1": ret1.reindex(sc.index)})
        d = d[d.index.get_level_values("ts") >= OOS].dropna(subset=["score", "ret1"])
        daily = []
        for ts, g in d.groupby(level="ts"):
            if len(g) < 3:
                continue
            g = g.sort_values("score", ascending=False)
            n_sel = max(1, int(np.ceil(len(g) * TOP_K)))
            sel = g.iloc[:n_sel]
            rets = []
            for (sym, _), row in sel.iterrows():
                pxv = prices.xs(sym, level=0)["close"].get(ts)
                if pxv is None or pd.isna(pxv) or pxv <= 0:
                    continue
                tick = CONTRACTS18[sym]["min_tick"]
                cost_rt = (tick / pxv + 0.00005) * 2.0  # 双边 1tick + 0.005%费
                rets.append(row["ret1"] - cost_rt)
            if rets:
                daily.append((ts, float(np.mean(rets))))
        s = pd.Series({ts: r for ts, r in daily}).sort_index()
        sharpe = float(s.mean() / s.std(ddof=1) * np.sqrt(252)) if len(s) > 1 else np.nan
        total = float((1.0 + s).prod() - 1.0)
        return {"sharpe": sharpe, "total_ret": total, "n": len(s)}

    print("\n  带成本（双边 1tick+0.005%/合约/日）1 日等权 top30%：")
    for name, col in (("v8", "exp_ret"), ("v10_cal", "p_up")):
        r = ew_top30_cost(name, col)
        print(f"  {name:<8} col={col:<8} Sharpe={r['sharpe']:+.2f} 复利={r['total_ret']*100:+.1f}% n={r['n']}")

    # ---------------------------------------------------------------
    # 6. 选中但不可交易 vs 可交易（1 日收益，引擎 A p_up 选择口径）
    # ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("[6] 引擎 A（p_up 选择）选中持仓：可交易 vs 不可交易（0手）1 日收益")
    print("=" * 100)
    # 复刻引擎 A 每日截面 rank + top30% + group_cap 前的基础选择（无 cap，仅观察选中集合）
    # 用生产 group_map/cap 需读 base.yaml；此处按「每日 top30% 选中」近似（cap 影响小）
    sig = caches["v10_cal"].copy()
    sig["rank_pct"] = sig.groupby("ts")["p_up"].rank(pct=True, ascending=True)
    sig["sel"] = sig["rank_pct"] >= 1.0 - TOP_K
    d = sig.set_index(["symbol", "ts"]).sort_index()
    d["ret1"] = ret1.reindex(d.index)
    d = d[d.index.get_level_values("ts") >= OOS].dropna(subset=["ret1"])
    # 逐行算手数（floor(notional/(px*mult))）
    lot_vals = []
    for (sym, ts), _row in d.iterrows():
        pxv = prices.xs(sym, level=0)["close"].get(ts)
        if pxv is None or pd.isna(pxv):
            lot_vals.append(np.nan)
        else:
            lot_vals.append(int(notional / (pxv * mult[sym])))
    d["lots"] = lot_vals
    sel_d = d[d["sel"] & d["lots"].notna()].copy()
    sel_d["tradeable"] = sel_d["lots"] > 0
    grp = sel_d.groupby("tradeable")["ret1"]
    print(f"  选中且价格可对齐样本 n={len(sel_d)}")
    for tb, g in grp:
        print(f"  tradeable={tb}: n={len(g):>4} 1日收益均值={g.mean()*100:+.3f}%/日 "
              f"中位={g.median()*100:+.3f}%")

    print("\n[DONE] QA P14 独立复核脚本结束")


if __name__ == "__main__":
    main()
