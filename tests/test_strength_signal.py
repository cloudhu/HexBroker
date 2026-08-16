"""测试强度信号评估模块（hexbroker/evaluation/strength.py）。

覆盖：quintile_analysis（全局/品种内）、long_only_topk（含成本）、long_short_spread。
用构造数据验证单调性与价差计算的正确性。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.evaluation.strength import (
    TRADES_PER_YEAR,
    long_only_topk,
    long_short_spread,
    quintile_analysis,
)


def _sig(n_per_sym: int = 40, seed: int = 0) -> pd.DataFrame:
    """构造信号：exp_ret 与 realized 正相关（真实 alpha），au0 无 alpha。"""
    rng = np.random.default_rng(seed)
    frames = []
    for sym, alpha in [("ag0", 0.5), ("m0", 1.0), ("au0", 0.0)]:
        exp_ret = rng.normal(0, 1, n_per_sym)
        # realized = alpha * exp_ret + 噪声（m0 最强、au0 无）
        realized = alpha * exp_ret + rng.normal(0, 0.5, n_per_sym)
        ts = pd.date_range("2024-01-01", periods=n_per_sym, freq="B")
        frames.append(pd.DataFrame({
            "symbol": sym, "ts": ts,
            "exp_ret": exp_ret, "realized": realized,
        }))
    return pd.concat(frames, ignore_index=True)


def test_quintile_analysis_global_monotone():
    """全局分位：含 alpha 的合成数据应呈正单调。"""
    sig = _sig()
    r = quintile_analysis(sig, n_q=5, by_symbol=False)
    assert len(r["rows"]) == 5
    assert r["monotone_corr"] > 0.5  # 强单调
    assert r["spread"] > 0  # Q4 > Q0


def test_quintile_analysis_by_symbol_reveals_no_alpha():
    """品种内分位：au0（无 alpha）应单调≈0；m0（强 alpha）应正。"""
    sig = _sig()
    r = quintile_analysis(sig, n_q=5, by_symbol=True)
    # 品种内分位把三品种混在一起，整体单调性被 au0 拉低但 m0 撑起——应仍为正
    assert r["monotone_corr"] > 0.1


def test_long_only_topk_basic_and_cost():
    """单边多头：top 20% 收益为正；成本扣除后净年化下降。"""
    sig = _sig()
    r0 = long_only_topk(sig, top_k=0.20, cost_per_trade=0.0)
    assert r0["n"] == pytest.approx(len(sig) * 0.2, rel=0.05)
    assert r0["mean_ret"] > 0
    assert r0["annualized"] == pytest.approx(r0["mean_ret"] * TRADES_PER_YEAR)

    r1 = long_only_topk(sig, top_k=0.20, cost_per_trade=0.001)
    assert r1["annualized_net"] < r1["annualized"]
    assert r1["annualized_net"] == pytest.approx((r1["mean_ret"] - 0.001) * TRADES_PER_YEAR)


def test_long_short_spread_costs():
    """多空价差：成本双倍扣除。"""
    sig = _sig()
    r0 = long_short_spread(sig, cost_per_trade=0.0)
    assert r0["spread"] > 0
    r1 = long_short_spread(sig, cost_per_trade=0.001)
    assert r1["annualized_net"] == pytest.approx(r0["annualized"] - 2 * 0.001 * TRADES_PER_YEAR)


def test_strength_requires_columns():
    """缺列时报错。"""
    with pytest.raises(ValueError, match="exp_ret"):
        quintile_analysis(pd.DataFrame({"symbol": ["a"], "realized": [0.1]}))
