"""强度信号评估（信号形态矫正的核心产出，2026-08-16）。

诊断结论（raw-alpha-diagnosis + signal-form-correction）：模型真实 alpha 在
**exp_ret 强度排序**而非 p_up>0.5 二元方向；看空反指、方向判断≈随机。
本模块提供基于 exp_ret 的强度信号评估原语：
- ``quintile_analysis``：exp_ret 分位收益表 + 单调性（含品种内标准化选项）
- ``long_only_topk``：单边多头（exp_ret 前 K% 做多）收益与年化
- ``long_short_spread``：Q4-Q0 多空价差与年化

所有函数输入均为含 symbol/ts/exp_ret/realized 的 DataFrame（嵌套口径评估集）。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# 5 日 horizon 每年约 50 次换手（252/5）
TRADES_PER_YEAR = 252 // 5


def _require_cols(sig: pd.DataFrame) -> None:
    for c in ("symbol", "exp_ret", "realized"):
        if c not in sig.columns:
            raise ValueError(f"强度信号评估需要列 '{c}'，实际有 {list(sig.columns)}")


def quintile_analysis(
    sig: pd.DataFrame,
    n_q: int = 5,
    by_symbol: bool = False,
) -> dict:
    """exp_ret 分位收益分析。

    - ``by_symbol=False``：全局分位（跨品种，捕获截面 alpha）。
    - ``by_symbol=True``：品种内分位（捕获时序 alpha；诊断显示品种内排序≈0）。
    返回分位收益表 + 分位-收益单调相关系数。
    """
    _require_cols(sig)
    df = sig.copy()
    if by_symbol:
        df["er_q"] = df.groupby("symbol")["exp_ret"].transform(
            lambda s: pd.qcut(s.rank(method="first"), n_q, labels=False)
        )
    else:
        df["er_q"] = pd.qcut(df["exp_ret"], n_q, labels=False, duplicates="drop")

    rows: list[dict] = []
    for q, sub in df.groupby("er_q", observed=True):
        rows.append({
            "q": int(q),
            "n": int(len(sub)),
            "mean_realized": float(sub["realized"].mean()),
        })
    rows.sort(key=lambda r: r["q"])
    if len(rows) < 2:
        return {"rows": rows, "monotone_corr": float("nan"), "spread": 0.0}
    q_vals = np.array([r["mean_realized"] for r in rows])
    mono = float(np.corrcoef(np.arange(len(rows)), q_vals)[0, 1])
    spread = float(rows[-1]["mean_realized"] - rows[0]["mean_realized"])
    return {
        "rows": rows,
        "monotone_corr": mono,
        "spread": spread,  # Q(n-1)-Q0 价差（每 5 日）
        "spread_annualized": spread * TRADES_PER_YEAR,
    }


def long_only_topk(
    sig: pd.DataFrame,
    top_k: float = 0.20,
    cost_per_trade: float = 0.0,
    by_symbol: bool = False,
) -> dict:
    """单边多头：exp_ret 前 top_k 比例做多，其余空仓（不做空）。

    cost_per_trade 为单次往返成本（小数），用于年化净收益估算。
    """
    _require_cols(sig)
    df = sig.copy()
    if by_symbol:
        df["rank_pct"] = df.groupby("symbol")["exp_ret"].transform(
            lambda s: s.rank(pct=True)
        )
    else:
        df["rank_pct"] = df["exp_ret"].rank(pct=True)
    mask = df["rank_pct"] >= 1.0 - top_k
    sel = df[mask]
    if len(sel) == 0:
        return {"n": 0, "mean_ret": 0.0, "annualized": 0.0, "annualized_net": 0.0}
    mean_ret = float(sel["realized"].mean())
    return {
        "n": int(len(sel)),
        "mean_ret": mean_ret,  # 每 5 日
        "annualized": mean_ret * TRADES_PER_YEAR,
        "annualized_net": (mean_ret - cost_per_trade) * TRADES_PER_YEAR,
    }


def long_short_spread(
    sig: pd.DataFrame,
    cost_per_trade: float = 0.0,
) -> dict:
    """Q4-Q0 多空价差（高分位做多、低分位做空）。成本按双倍计（开平两腿）。"""
    _require_cols(sig)
    q = quintile_analysis(sig, n_q=5, by_symbol=False)
    if len(q["rows"]) < 2:
        return {"spread": 0.0, "annualized": 0.0, "annualized_net": 0.0}
    spread = q["spread"]
    return {
        "spread": spread,
        "annualized": spread * TRADES_PER_YEAR,
        "annualized_net": (spread - 2 * cost_per_trade) * TRADES_PER_YEAR,
    }
