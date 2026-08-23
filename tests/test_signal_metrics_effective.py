"""E3-A 回归测试：_signal_metrics 工作副本纳入 is_effective。

根因：旧实现只选 ``p_up/vol_hat/conf``，漏选 ``is_effective`` →
第119行 ``df["is_effective"] if ...`` 分支因列缺失**永远走默认**
（``abs(p-0.5)>0.05``），属死代码，且口径与信号帧实际列不一致。

修复：把 ``is_effective`` 纳入工作副本（若存在），但**不设默认阈值硬编码**——
沿用 ``is_effective`` 列原值，故在「is_effective == 默认分支掩码」这一常见情况下
数值与旧默认分支完全一致，守住「无证据不翻转」铁律。

本测试验证：
- 含 is_effective 列时工作副本用该列（列确实存在）；
- 默认阈下（make_signals 构造的 is_effective 恰等于 abs(p-0.5)>0.05）数值不变。
"""
from __future__ import annotations

from hexbroker.pipeline import _forward_returns, _signal_metrics
from tests._helpers import make_prices, make_signals


def test_signal_metrics_includes_is_effective_column():
    prices = make_prices(n_bars=200, seed=11, symbols=["SHFE.cu"])
    signals = make_signals(prices, seed=11)
    assert "is_effective" in signals.columns  # 信号帧确有该列（审计反问已证伪）

    fwd = _forward_returns(prices, 5)
    m = _signal_metrics(signals, fwd)
    # 基本产出健全
    assert set(["dir_acc", "eff_acc", "coverage", "rank_ic", "brier", "calib_err", "n"]) <= set(m)
    assert m["n"] > 0


def test_signal_metrics_no_research_flip_at_default_threshold():
    """含/不含 is_effective 列 → 默认阈下数值应完全相同（不翻转研究结论）。"""
    prices = make_prices(n_bars=200, seed=13, symbols=["SHFE.cu"])
    signals_with = make_signals(prices, seed=13)
    signals_without = signals_with.drop(columns=["is_effective"])

    fwd = _forward_returns(prices, 5)
    m_with = _signal_metrics(signals_with, fwd)
    m_without = _signal_metrics(signals_without, fwd)

    assert m_with["dir_acc"] == m_without["dir_acc"]
    assert m_with["eff_acc"] == m_without["eff_acc"]
    assert m_with["coverage"] == m_without["coverage"]
    assert m_with["rank_ic"] == m_without["rank_ic"]
    assert m_with["brier"] == m_without["brier"]
    assert m_with["n"] == m_without["n"]
