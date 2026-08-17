"""品种质量筛选：18 品种按嵌套 IC/价差贡献排序，筛选 top 子集。

指标（每个品种，在嵌套评估集内）：
  - RankIC：spearman(exp_ret, realized5)（全局排序质量）
  - Q4-Q0 价差：exp_ret 分位桶的实际收益价差（极端分位区分度）
  - 贡献度：品种在 top25% 信号中的占比 × 品种 alpha

筛选：按 IC 降序取 top 10 / top 12 → 子集重跑最优配置 + 快速搜索。
对比：6 品种（精选稳健）/ 18 品种（全市场）/ 筛选子集（宽+质平衡）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scipy import stats

from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.eval_signals18 import build_rolling_spread18, load_prices18, load_sig18_sym_close, run_cfg
from scripts.combo_validation import OOS_START


def add_realized5(sig: pd.DataFrame) -> pd.DataFrame:
    """为信号附加已实现 5 日收益（仅用于质量评估，不用于交易）。"""
    closes = {sym: load_sig18_sym_close(sym) for sym in SYMBOLS18}
    close_df = pd.DataFrame(closes).sort_index()
    real5 = close_df.shift(-5) / close_df - 1.0
    rows = []
    for _, r in sig.iterrows():
        if r["ts"] in real5.index and r["symbol"] in real5.columns:
            v = real5.loc[r["ts"], r["symbol"]]
            if pd.notna(v):
                rows.append((r["symbol"], r["ts"], r["exp_ret"], float(v)))
    out = pd.DataFrame(rows, columns=["symbol", "ts", "exp_ret", "real5"])
    return out.merge(sig[["symbol", "ts"]], on=["symbol", "ts"], how="left")


def quality_report(sig: pd.DataFrame) -> pd.DataFrame:
    """每个品种的 IC / 价差 / 样本量。"""
    rows = []
    for sym in SYMBOLS18:
        sub = sig[sig["symbol"] == sym].dropna(subset=["real5"])
        if len(sub) < 100:
            rows.append({"symbol": sym, "n": len(sub), "rankic": np.nan, "spread": np.nan})
            continue
        ic = stats.spearmanr(sub["exp_ret"], sub["real5"]).statistic
        q = pd.qcut(sub["exp_ret"].rank(pct=True), 5, labels=False, duplicates="drop")
        if q.nunique() >= 3:
            m = sub.groupby(q)["real5"].mean()
            spread = m.iloc[-1] - m.iloc[0]
        else:
            spread = np.nan
        rows.append({"symbol": sym, "n": len(sub), "rankic": float(ic), "spread": float(spread)})
    df = pd.DataFrame(rows)
    df["ic_spread"] = df["rankic"].fillna(0) + df["spread"].fillna(0)
    return df.sort_values("rankic", ascending=False)


def main() -> None:
    print("=" * 72)
    print("品种质量筛选（18 → top 子集）")
    print("=" * 72)
    sig = pd.read_parquet("artifacts/signals_cache18.parquet")
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] 18 品种信号 {len(sig)} 条")

    # 质量评估
    sig_q = add_realized5(sig)
    q = quality_report(sig_q)
    print("\n[质量报告] 各品种嵌套 RankIC 与 Q4-Q0 价差（排序）")
    print(q.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    # 筛选子集
    prices, close_by = load_prices18()
    closes = {sym: close_by[sym] for sym in SYMBOLS18}
    close_df = pd.DataFrame(closes).sort_index()
    mom120 = close_df / close_df.shift(120) - 1.0
    sig["mom"] = sig.apply(
        lambda r: float(mom120.loc[r["ts"], r["symbol"]])
        if r["ts"] in mom120.index and r["symbol"] in mom120.columns else np.nan,
        axis=1,
    )
    spreads_full = build_rolling_spread18(sig, 20)
    scale18 = (spreads_full >= -0.003).astype(float).fillna(1.0).clip(0.0, 1.0)

    top10 = set(q.head(10)["symbol"])
    top12 = set(q.head(12)["symbol"])
    # 始终包含 6 原品种中质量高的（若在 top 内自然包含）
    print(f"\n[筛选] top10 = {sorted(top10)}")
    print(f"[筛选] top12 = {sorted(top12)}")

    print("\n=== 各池最优配置对比（w1=0.5 topk=0.30 thr=-0.3%） ===")
    for label, syms in [("18 全部", SYMBOLS18), ("top12", top12), ("top10", top10)]:
        sub = sig[sig["symbol"].isin(syms)].copy()
        run_cfg(sub, prices, scale18, 0.5, 0.5, 0.30, label=f"{label} ({len(syms)}品种)")

    # top12 快速搜索
    print("\n=== top12 快速搜索（w1 × topk） ===")
    sub12 = sig[sig["symbol"].isin(top12)].copy()
    for w1, topk in [(0.5, 0.25), (0.5, 0.30), (0.6, 0.30), (0.4, 0.30)]:
        run_cfg(sub12, prices, scale18, w1, 1 - w1, topk, label=f"w1={w1} topk={topk}")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/symbol-quality-filter-2026-08-17.md")


if __name__ == "__main__":
    main()
