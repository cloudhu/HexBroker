"""后复权续接器单测（P0-5）。

验证策略：构造**已知真值**的序列，断言续接结果与解析解一致，而非只看"能跑通"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker import HexDataError
from hexbroker.data.graft import (
    DEFAULT_ALIGN_TOL,
    GraftResult,
    graft_adjusted,
    verify_alignment,
)

D = pd.Timestamp


def _idx(start: str, n: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="D")


def _s(idx, values) -> pd.Series:
    return pd.Series(list(values), index=idx, dtype=float)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
class TestInputValidation:
    def test_empty_adj_raises(self):
        with pytest.raises(HexDataError, match="为空"):
            graft_adjusted(_s(_idx("2026-08-01", 0), []), _s(_idx("2026-08-01", 3), [1, 2, 3]))

    def test_non_positive_price_raises(self):
        idx = _idx("2026-08-01", 3)
        with pytest.raises(HexDataError, match="非正价格"):
            graft_adjusted(_s(idx, [1.0, 2.0, 3.0]), _s(idx, [1.0, 0.0, 3.0]))

    def test_provisional_flag_set_when_grafted(self):
        """有续接段 → provisional=True。下游据此在主源恢复后重建窗口。"""
        idx = _idx("2026-08-01", 10)
        raw = _s(idx, [100.0 + i for i in range(10)])
        adj = _s(idx[:5], [2.0 * v for v in raw.iloc[:5]])
        res = graft_adjusted(adj, raw)
        assert res.provisional is True
        assert res.new_dates == list(idx[5:])

    def test_provisional_false_when_nothing_to_graft(self):
        """备源没有更新的数据 → 无需外推 → 不是临时值。"""
        idx = _idx("2026-08-01", 10)
        raw = _s(idx, [100.0 + i for i in range(10)])
        adj = _s(idx, [2.0 * v for v in raw])
        res = graft_adjusted(adj, raw)
        assert res.provisional is False
        assert res.new_dates == []

    def test_undetectable_break_after_anchor_yields_no_warning(self):
        """锚点之后的口径跳变**原理上不可检出** —— 固化此事实。

        cu0 2026-08-21 实测：重叠区 08-14~08-20 比值恒定（cv=0），
        跳变发生在锚点之后，结果 -21.35 bp 但零告警。
        这正说明 provisional 标记不可省（不能靠 warnings 兜底）。
        """
        idx = _idx("2026-08-14", 10)
        base = [107690.0, 109540.0, 107930.0, 106850.0, 107200.0,
                107520.0, 107910.0, 107980.0, 108750.0, 108300.0]
        raw = _s(idx, base)
        # 主源真值：前 5 天比值 1.472692，第 6 天起跳到 1.475842
        adj_vals = [1.472692 * v for v in base[:5]] + [1.475842 * v for v in base[5:]]
        adj_all = _s(idx, adj_vals)

        res = graft_adjusted(adj_all.iloc[:5], raw)
        # 重叠区（前 5 天）完全对齐 → 无告警
        assert res.alignment["aligned"] is True
        assert res.warnings == []
        # 但锚点之后的跳变照样造成恒定偏移
        err_bp = (res.series.loc[idx[5:]] / adj_all.loc[idx[5:]] - 1) * 1e4
        assert abs(float(err_bp.iloc[0])) == pytest.approx(21.35, abs=0.5)
        # 唯一的安全网就是 provisional 标记
        assert res.provisional is True

    def test_no_overlap_raises(self):
        """无锚点禁止外推 —— 这是硬约束，不是警告。"""
        a = _s(_idx("2026-08-01", 3), [10.0, 11.0, 12.0])
        r = _s(_idx("2026-09-01", 3), [10.0, 11.0, 12.0])
        with pytest.raises(HexDataError, match="无重叠日期"):
            graft_adjusted(a, r)


# ---------------------------------------------------------------------------
# 情形 1：无换月
# ---------------------------------------------------------------------------
class TestNoRollover:
    def test_ratio_preserved(self):
        """主源 = 3 × 备源，锚点后应严格保持该比例。"""
        idx = _idx("2026-08-01", 15)
        raw = _s(idx, [100.0 + i for i in range(15)])
        adj = _s(idx[:10], [3.0 * v for v in raw.iloc[:10]])

        res = graft_adjusted(adj, raw)
        assert isinstance(res, GraftResult)
        assert res.ok, f"不应有告警: {res.warnings}"
        assert res.anchor_date == idx[9]
        assert res.anchor_ratio == pytest.approx(3.0)
        assert len(res.segments) == 1

        # 新增段逐位等于 3 × raw
        for d in res.new_dates:
            assert res.series.loc[d] == pytest.approx(3.0 * raw.loc[d])

    def test_history_untouched(self):
        idx = _idx("2026-08-01", 12)
        raw = _s(idx, [50.0] * 12)
        adj = _s(idx[:8], [2.5 * v for v in raw.iloc[:8]])
        res = graft_adjusted(adj, raw)
        for d in idx[:8]:
            assert res.series.loc[d] == pytest.approx(adj.loc[d])

    def test_nothing_to_graft(self):
        idx = _idx("2026-08-01", 5)
        raw = _s(idx, [10.0] * 5)
        adj = _s(idx, [20.0] * 5)
        res = graft_adjusted(adj, raw)
        assert res.new_dates == []
        assert len(res.series) == 5

    def test_alignment_verified_on_overlap(self):
        idx = _idx("2026-08-01", 12)
        raw = _s(idx, [100.0 + i for i in range(12)])
        adj = _s(idx[:8], [4.0 * v for v in raw.iloc[:8]])
        res = graft_adjusted(adj, raw)
        assert res.alignment["aligned"] is True
        assert res.alignment["ratio_cv"] < DEFAULT_ALIGN_TOL
        assert res.alignment["ratio_mean"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# 情形 2：有换月
# ---------------------------------------------------------------------------
class TestRollover:
    def _setup(self):
        idx = _idx("2026-08-01", 12)
        # 备源主力连续：第 7 天（idx[6]）切合约，价格翻倍（纯价差，无真实波动）
        raw_vals = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0]
        raw = _s(idx, raw_vals)
        adj = _s(idx[:6], [3.0 * v for v in raw_vals[:6]])  # 主源到 idx[5]
        return idx, raw, adj

    def test_no_spread_skips_adjustment(self):
        """未提供精确价差 → **跳过**调整（不套用近似），并告警。

        依据：2026-08-28 对 18 个品种的真实数据消融显示，近似价差
        ``raw[R]/raw[R-1]`` 在 18/18 品种上不优于不调整，且在 2/18 品种上
        显著劣化（cu0 +36.06bp、ni0 +20.12bp）。故此处必须"宁可不调"。
        """
        idx, raw, adj = self._setup()
        res = graft_adjusted(adj, raw, rollover_dates=[idx[6]])
        # 跳过后 k 保持 3.0 → adj[idx[6]] = 3.0 * 20 = 60（而非 30）
        assert res.series.loc[idx[6]] == pytest.approx(60.0)
        assert any("跳过" in w for w in res.warnings)
        assert any("近似价差为净损害" in w for w in res.warnings)
        assert not res.ok

    def test_two_segments_still_recorded(self):
        """跳过调整也要记录分段，便于下游定位风险段。"""
        idx, raw, adj = self._setup()
        res = graft_adjusted(adj, raw, rollover_dates=[idx[6]])
        assert len(res.segments) == 2
        assert res.segments[0][1] == pytest.approx(3.0)
        assert res.segments[1][1] == pytest.approx(3.0)  # 跳过 → k 不变

    def test_explicit_spread_no_skip_warning(self):
        """给了精确价差就不应再有"跳过"告警。"""
        idx, raw, adj = self._setup()
        res = graft_adjusted(adj, raw, rollover_dates=[idx[6]],
                             rollover_spreads={idx[6]: 2.0})
        assert not any("跳过" in w for w in res.warnings)
        # 精确价差 2.0 → adj[idx[6]] = (3.0/2.0) * 20 = 30，换月日真实收益 0
        assert res.series.loc[idx[6]] == pytest.approx(30.0)
        assert res.ok

    def test_explicit_spread_changes_result(self):
        """精确价差 ≠ 观测跳空时，结果应随之不同（证明价差确实参与计算）。"""
        idx, raw, adj = self._setup()
        res = graft_adjusted(adj, raw, rollover_dates=[idx[6]],
                             rollover_spreads={idx[6]: 4.0})
        # k = 3.0 / 4.0 = 0.75；adj[idx[6]] = 0.75 * 20 = 15
        assert res.series.loc[idx[6]] == pytest.approx(15.0)

    def test_non_positive_spread_raises(self):
        idx, raw, adj = self._setup()
        with pytest.raises(HexDataError, match="价差非正"):
            graft_adjusted(adj, raw, rollover_dates=[idx[6]],
                           rollover_spreads={idx[6]: 0.0})


# ---------------------------------------------------------------------------
# max_graft_days 截断
# ---------------------------------------------------------------------------
class TestMaxGraftDays:
    def test_truncate_and_warn(self):
        idx = _idx("2026-08-01", 40)
        raw = _s(idx, [100.0 + i for i in range(40)])
        adj = _s(idx[:5], [2.0 * v for v in raw.iloc[:5]])
        res = graft_adjusted(adj, raw, max_graft_days=10)
        assert len(res.new_dates) == 10
        assert any("超过上限" in w for w in res.warnings)

    def test_no_truncate_when_within(self):
        idx = _idx("2026-08-01", 8)
        raw = _s(idx, [100.0 + i for i in range(8)])
        adj = _s(idx[:5], [2.0 * v for v in raw.iloc[:5]])
        res = graft_adjusted(adj, raw, max_graft_days=10)
        assert len(res.new_dates) == 3
        assert not any("超过上限" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# 重叠区对齐校验（约束④的正确实现）
# ---------------------------------------------------------------------------
class TestVerifyAlignment:
    def test_constant_ratio_aligned(self):
        idx = _idx("2026-08-01", 10)
        raw = _s(idx, [10.0 + i for i in range(10)])
        adj = _s(idx, [7.0 * v for v in raw])
        d = verify_alignment(adj, raw)
        assert d["aligned"] is True
        assert d["ratio_mean"] == pytest.approx(7.0)
        assert d["breaks"] == []

    def test_ratio_break_detected(self):
        """检出未登记的换月 —— 这是本校验的核心价值。"""
        idx = _idx("2026-08-01", 6)
        raw = _s(idx, [10.0, 10.0, 10.0, 20.0, 20.0, 20.0])
        adj = _s(idx, [30.0, 30.0, 30.0, 30.0, 30.0, 30.0])
        d = verify_alignment(adj, raw)
        assert d["aligned"] is False
        assert len(d["breaks"]) >= 1
        assert d["breaks"][0] == idx[3]

    def test_insufficient_overlap(self):
        a = _s(_idx("2026-08-01", 1), [10.0])
        r = _s(_idx("2026-08-01", 1), [5.0])
        d = verify_alignment(a, r)
        assert d["n"] == 1
        assert d["aligned"] is False
        assert "不足" in d["note"]

    def test_lookback_limits_window(self):
        idx = _idx("2026-08-01", 100)
        raw = _s(idx, np.linspace(10, 110, 100))
        adj = _s(idx, np.linspace(10, 110, 100) * 2.0)
        assert verify_alignment(adj, raw, lookback=20)["n"] == 20
        assert verify_alignment(adj, raw, lookback=None)["n"] == 100

    def test_graft_warns_on_unregistered_rollover(self):
        """重叠区有未登记换月时，续接必须告警而非静默。"""
        idx = _idx("2026-08-01", 12)
        raw = _s(idx, [10.0] * 6 + [20.0] * 6)
        adj = _s(idx[:8], [30.0] * 8)  # 主源口径：换月处不跳
        res = graft_adjusted(adj, raw)
        assert any("比值突变" in w for w in res.warnings)
        assert not res.ok


# ---------------------------------------------------------------------------
# 端到端：贴近真实场景
# ---------------------------------------------------------------------------
class TestEndToEnd:
    def test_realistic_backup_extension(self):
        """主源停在 08-25，备源有到 08-28 的名义价，续接后收益率应与备源一致。"""
        idx = _idx("2026-08-20", 9)  # 08-20 .. 08-28
        raw_vals = [3000.0, 3010.0, 3025.0, 3015.0, 3030.0, 3040.0, 3055.0, 3060.0, 3075.0]
        raw = _s(idx, raw_vals)
        # 主源后复权 = 1.2345 × 名义价（常数比例因子），只到 08-25
        k = 1.2345
        adj = _s(idx[:6], [k * v for v in raw_vals[:6]])

        res = graft_adjusted(adj, raw)
        assert res.ok
        # 续接段的日收益率必须与备源一致（这才是"不换口径"的实质检验）
        for i in range(6, 9):
            r_graft = res.series.loc[idx[i]] / res.series.loc[idx[i - 1]] - 1
            r_raw = raw.loc[idx[i]] / raw.loc[idx[i - 1]] - 1
            assert r_graft == pytest.approx(r_raw, abs=1e-12)


# ---------------------------------------------------------------------------
# 真实事故模式固化（2026-08-28 cu0 实测，见 dev_probe_37_graft_truth.py）
# ---------------------------------------------------------------------------
class TestRealWorldRolloverMisdate:
    """锁定「dominant_id 标注日 ≠ 比值实际跳变日」这一实证模式。

    真实数据（cu0, 2026-08-14~08-27，pandadata close_pcr vs 新浪主力连续）：
        比值 08-14~08-20 恒为 1.472692，08-21 起恒为 1.475842（+0.2139%）。
        pandadata ``dominant_id`` 却在 08-24 才从 CU2609 切到 CU2610。

    含义：按 dominant_id 施加换月调整 = 在**错误的日期**施加**错误的幅度**。
    实测后果：cu0 误差由 -21.35bp 放大到 -57.41bp。
    """

    def _cu0_like(self):
        idx = _idx("2026-08-14", 10)  # 08-14..08-23（10 个日历日，含周末）
        # 备源名义价（新浪主力连续）
        raw = _s(idx, [107690.0, 109540.0, 107930.0, 106850.0, 107200.0,
                       107520.0, 107910.0, 107980.0, 108750.0, 108300.0])
        # 主源后复权：比值在 idx[5]（真实切换）跳变，而非 dominant_id 标的 idx[6]
        k1, k2 = 1.472692, 1.475842
        adj = _s(idx, [k1, k1, k1, k1, k1, k2, k2, k2, k2, k2]
                 * np.array([107690.0, 109540.0, 107930.0, 106850.0, 107200.0,
                             107520.0, 107910.0, 107980.0, 108750.0, 108300.0]))
        return idx, raw, adj, k1, k2

    def test_misdated_rollover_is_harmful(self):
        """按 dominant_id（晚一天）调整，误差应大于完全不调整。"""
        idx, raw, adj, _k1, _k2 = self._cu0_like()
        truth_tail = adj.iloc[5:]  # 主源真值（模拟"后来主源恢复拿到的值"）

        hist = adj.iloc[:5]
        # A：无视换月
        a = graft_adjusted(hist, raw)
        # B：按 dominant_id 传换月日（无精确价差 → 现在会跳过，等价于 A）
        b = graft_adjusted(hist, raw, rollover_dates=[idx[6]])

        err_a = ((a.series.reindex(truth_tail.index) - truth_tail)
                 / truth_tail * 1e4).abs().max()
        err_b = ((b.series.reindex(truth_tail.index) - truth_tail)
                 / truth_tail * 1e4).abs().max()

        # 跳过调整后 B 不应再劣于 A（这正是本次修复的目标）
        assert err_b == pytest.approx(err_a)
        # 且 B 必须给出"跳过"告警，不得静默
        assert any("跳过" in w for w in b.warnings)

    def test_unregistered_break_is_flagged_in_overlap(self):
        """真实切换发生在**重叠区**时，必须被 overlaps 校验检出。"""
        idx, raw, adj, _k1, _k2 = self._cu0_like()
        d = verify_alignment(adj, raw)
        assert d["aligned"] is False
        assert idx[5] in d["breaks"]
