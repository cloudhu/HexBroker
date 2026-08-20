"""P4-2 引擎B 双因子升级：基差分位(f1) + 基差动量(f2) IC 验证与合成回测。

背景
----
引擎B 基线（单因子）：basis_ratio 品种内滚动 252 日分位 >= 0.7 做多，
全样本 Sharpe 0.927 / OOS(2024-07-18 后) Sharpe 1.260 / 交易胜率 64.4%。
P4-2 尝试加入基差动量因子 f2 做双因子合成：
  - f1 = basis_ratio 品种内滚动 252 日分位（沿用引擎B）
  - f2 = basis_ratio 的 5 日变化率(pct_change(5)) / 10 日变化率(pct_change(10))
  - 先用品种内时序 RankIC（嵌套 cal_split=0.5，PandaData 2024-07-18 后严格 OOS 复核）
    验证 f2 单独是否 PASS（门槛 |OOS IC|>=0.03 且 IS/OOS 同号）
  - 若 PASS：score = w*f1_rank + (1-w)*f2_rank，w∈{0.5,0.7}，做多 score 分位>=0.7
    → BacktestEngine 完整口径回测，对比单因子基线 OOS Sharpe 1.260
  - 若 NOT_PASS：如实否决，不强推

口径（铁律）：品种内时序 RankIC（禁止截面IC）；参数只用 IS 选定；OOS 只做终裁；
回测滑点1tick + 手续费0.005% + 保证金12%。

用法：
  python scripts/p4_dual_factor.py
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
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p1_factor_ic import IC_THRESHOLD, STRICT_OOS, load_kline, per_symbol_ts_ic
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_basis_panel, load_prices
from scripts.p3_combo_backtest import engine_b_targets, run_engine

BASIS_WIN = 252
BASIS_THR = 0.70
NOTIONAL_FRAC = 0.20
IC_HORIZONS = [5, 10, 20]
MIN_N = 60
F2_CANDIDATES = {"f2_5d": 5, "f2_10d": 10}
OOS_START = pd.Timestamp("2024-07-18")
ART = ROOT / "artifacts"


def build_basis_ic_panel() -> pd.DataFrame:
    """基差面板 + f1_rank/f2 因子 + 前瞻收益 → 长表（symbol/date 列）。"""
    rows = []
    fund = ROOT / "data/raw/fundamental"
    for sym in SYMBOLS18:
        sym_u = sym[:-1].upper()
        f = fund / f"basis_{sym_u}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates("date").set_index("date")
        df["symbol"] = sym
        df["f1_rank"] = df["basis_ratio"].rolling(BASIS_WIN, min_periods=MIN_N).rank(pct=True)
        df["f2_5d"] = df["basis_ratio"].pct_change(5)
        df["f2_10d"] = df["basis_ratio"].pct_change(10)
        k = load_kline(sym[:-1])  # p1.load_kline 内部会补 '0'，这里传去掉尾部 0 的品种名
        for h in IC_HORIZONS:
            k[f"fwd_{h}"] = k["close"].shift(-h) / k["close"] - 1.0
        m = df.join(k[["close"] + [f"fwd_{h}" for h in IC_HORIZONS]], how="inner")
        rows.append(m.reset_index())
    return pd.concat(rows, ignore_index=True)


def ic_block(panel: pd.DataFrame, factor: str, split_dt: pd.Timestamp) -> list[dict]:
    """单个因子：IS/OOS(嵌套) + 严格 OOS IC 与判定（返回全部 h 行）。"""
    is_seg = panel[panel["date"] <= split_dt]
    oos_seg = panel[panel["date"] > split_dt]
    strict_seg = panel[panel["date"] >= STRICT_OOS]
    rows = []
    for h in IC_HORIZONS:
        is_arr = per_symbol_ts_ic(is_seg, factor, h, min_n=MIN_N)
        oos_arr = per_symbol_ts_ic(oos_seg, factor, h, min_n=MIN_N)
        strict_arr = per_symbol_ts_ic(strict_seg, factor, h, min_n=MIN_N) if len(strict_seg) else np.array([])
        if len(is_arr) == 0 or len(oos_arr) == 0:
            continue
        is_ic, oos_ic = float(is_arr.mean()), float(oos_arr.mean())
        same = np.sign(is_ic) == np.sign(oos_ic)
        strong = abs(oos_ic) >= IC_THRESHOLD
        verdict = "PASS" if (strong and same) else ("WEAK" if same else "REVERSED")
        strict_ic = float(strict_arr.mean()) if len(strict_arr) else np.nan
        rows.append({"factor": factor, "h": h, "is_ic": is_ic, "oos_ic": oos_ic,
                     "is_n": len(is_arr), "oos_n": len(oos_arr),
                     "strict_oos_ic": strict_ic, "verdict": verdict})
        print(f"  {factor:8s} h={h:>2}: IS IC={is_ic:+.4f}(n={len(is_arr)}) | "
              f"OOS IC={oos_ic:+.4f}(n={len(oos_arr)}) | "
              f"strict IC={strict_ic:+.4f} → {verdict}")
    return rows


def dual_targets(prices: pd.DataFrame, f2_win: int, w: float, flip: bool) -> pd.DataFrame:
    """双因子合成信号 → target（score = w*f1_rank + (1-w)*f2_rank，>=thr 做多）。"""
    basis = load_basis_panel()
    basis["f1_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(BASIS_WIN, min_periods=MIN_N).rank(pct=True)
    )
    basis["f2_raw"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.pct_change(f2_win)
    )
    basis["f2_rank"] = basis.groupby("symbol")["f2_raw"].transform(
        lambda s: s.rolling(BASIS_WIN, min_periods=MIN_N).rank(pct=True)
    )
    if flip:  # f2 为反向因子时做 rank 反转（参数符号由 IS 决定）
        basis["f2_rank"] = 1.0 - basis["f2_rank"]
    basis["score"] = w * basis["f1_rank"] + (1.0 - w) * basis["f2_rank"]

    sig = basis[["score"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "score"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["score"] >= BASIS_THR,
        (notional / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    return sig[["target"]].sort_index()


def ret_metrics_row(ret: pd.Series, label: str, n_long: int | None = None) -> dict:
    """由日收益序列重建权益 → 指标行（不重复跑引擎）。"""
    eq = (1.0 + ret).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "label": label,
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "win_rate_full": m.win_rate,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "final_equity": eq.iloc[-1],
        "n_long_days": int(n_long) if n_long is not None else np.nan,
    }


def main() -> None:
    print("=" * 96)
    print("P4-2 引擎B 双因子升级：f1=基差分位 + f2=基差动量(5d/10d) — 品种内时序 RankIC")
    print("=" * 96)

    panel = build_basis_ic_panel()
    dates = sorted(panel["date"].unique())
    split_dt = dates[len(dates) // 2]
    print(f"面板: {len(panel)} 行 | {panel['symbol'].nunique()} 品种 | "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}")
    print(f"嵌套切分(cal_split=0.5): {split_dt.date()} | 严格OOS: {STRICT_OOS.date()} | "
          f"门槛 |OOS IC|>={IC_THRESHOLD} 且 IS/OOS 同号\n")

    # ---- 1. f2 IC 验证（含 f1 基线参考） ----
    print("--- 品种内时序 RankIC（嵌套） ---")
    ic_rows = []
    results: dict[str, dict] = {}
    for factor in ["f1_rank", "f2_5d", "f2_10d"]:
        print(f"[因子] {factor}")
        rows = ic_block(panel, factor, split_dt)
        ic_rows.extend(rows)
        results[factor] = max(rows, key=lambda r: abs(r["oos_ic"])) if rows else {}
        print()
    ic_df = pd.DataFrame(ic_rows)
    ART.mkdir(exist_ok=True)
    ic_df.to_csv(ART / "p4_dual_factor_ic.csv", index=False)
    print(f"[OK] IC 结果 → {ART / 'p4_dual_factor_ic.csv'}\n")

    # ---- 2. f2 判定 ----
    f2_best = None
    for cand, win in F2_CANDIDATES.items():
        b = results.get(cand)
        if b and b["verdict"] == "PASS":
            if f2_best is None or abs(b["oos_ic"]) > abs(f2_best["oos_ic"]):
                f2_best = {"cand": cand, "win": win, **b}
    if f2_best is None:
        print("=" * 96)
        print("判定: f2 基差动量 NOT_PASS —— 如实否决，不强推双因子合成")
        print("=" * 96)
        print("  (任一 f2 候选均未达到 |OOS IC|>=0.03 且 IS/OOS 同号的门槛)")
        return

    cand = f2_best["cand"]
    f2_win = f2_best["win"]
    flip = f2_best["is_ic"] < 0
    print("=" * 96)
    print(f"判定: f2 基差动量 PASS —— 选用 {cand}（win={f2_win} 日变化率），"
          f"OOS IC={f2_best['oos_ic']:+.4f}（h={f2_best['h']}）"
          f"{'，按 IS 负号做 rank 反转' if flip else '，正向使用'}")
    print("=" * 96)

    # ---- 3. 合成回测（w∈{0.5,0.7}）vs 单因子基线 ----
    print("\n--- 回测（完整口径：滑点1tick + 手续费0.005% + 保证金12%） ---")
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()

    tgt_base = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_base = run_engine(cfg, cost, prices, tgt_base, "基线 单因子f1(win252/thr0.7)")
    rows = [ret_metrics_row(ret_base, "baseline_f1", n_long=int((tgt_base["target"] > 0).sum()))]

    for w in (0.5, 0.7):
        tgt = dual_targets(prices, f2_win, w, flip)
        label = f"dual_w{w:.1f}({cand},flip={flip})"
        ret = run_engine(cfg, cost, prices, tgt, f"双因子 {label}")
        rows.append(ret_metrics_row(ret, label, n_long=int((tgt["target"] > 0).sum())))

    bt = pd.DataFrame(rows)
    print("\n--- 回测对比表 ---")
    show = bt.copy()
    for c, fmt in [("sharpe_full", "{:.3f}"), ("ann_ret_full", "{:+.1%}"),
                   ("maxdd_full", "{:.1%}"), ("win_rate_full", "{:.1%}"),
                   ("oos_sharpe", "{:.3f}"), ("oos_maxdd", "{:.1%}"),
                   ("oos_ret", "{:+.1%}"), ("final_equity", "{:,.0f}")]:
        show[c] = show[c].map(lambda v: fmt.format(v))
    print(show.to_string(index=False))
    bt.to_csv(ART / "p4_dual_factor_backtest.csv", index=False)
    print(f"\n[OK] 回测对比 → {ART / 'p4_dual_factor_backtest.csv'}")

    # ---- 4. 终裁：OOS Sharpe 是否优于单因子基线 1.260 ----
    base_row = bt[bt["label"] == "baseline_f1"].iloc[0]
    print("\n--- 终裁（OOS 只做终裁） ---")
    print(f"  单因子基线 OOS Sharpe={base_row['oos_sharpe']:.3f} "
          f"OOS MaxDD={base_row['oos_maxdd']*100:.1f}%")
    for _, r in bt[bt["label"] != "baseline_f1"].iterrows():
        delta = r["oos_sharpe"] - base_row["oos_sharpe"]
        verdict = "PASS(优于基线)" if delta > 0 else "NOT_PASS(未优于基线)"
        print(f"  {r['label']:24s} OOS Sharpe={r['oos_sharpe']:.3f} "
              f"(Δ{delta:+.3f}) OOS MaxDD={r['oos_maxdd']*100:.1f}% → {verdict}")


if __name__ == "__main__":
    main()
