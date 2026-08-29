"""取数结果门禁单测（P0-0 / P0-7）：空结果与陈旧数据必须显式失败。

覆盖 2026-08-28 停摆事故的故障模式：数据源停更 → 请求新日期窗口 → 裁剪成 0 行
（或剩几根旧 bar）→ 旧版 ``validate_bars`` 全部校验项对空/陈旧帧放行 → 静默"刷新成功"。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from hexbroker import HexEmptyDataError, HexStaleDataError
from hexbroker.data.freshness import (
    DEFAULT_MAX_STALE_DAYS,
    assert_fresh,
    assert_nonempty,
    check_fetch_result,
    latest_bar_date,
)
from hexbroker.data.schema import BarFrame

REQUIRED = ["open", "high", "low", "close", "volume", "amount",
            "open_interest", "adj_close", "raw_close"]


def _frame(dates: list[str], sym: str = "rb0") -> pd.DataFrame:
    """构造符合 BarFrame 契约的最小 DataFrame。"""
    n = len(dates)
    df = pd.DataFrame(
        {
            "open": [10.0 + i for i in range(n)],
            "high": [11.0 + i for i in range(n)],
            "low": [9.0 + i for i in range(n)],
            "close": [10.5 + i for i in range(n)],
            "volume": [100.0] * n,
            "amount": [0.0] * n,
            "open_interest": [1000.0] * n,
            "adj_close": [10.5 + i for i in range(n)],
            "raw_close": [10.5 + i for i in range(n)],
        },
        index=pd.MultiIndex.from_product(
            [[sym], pd.to_datetime(dates)], names=["symbol", "datetime"]
        ),
    )
    return df


# ---------------------------------------------------------------------------
# latest_bar_date
# ---------------------------------------------------------------------------
class TestLatestBarDate:
    def test_none_for_empty(self):
        assert latest_bar_date(_frame([])) is None

    def test_multiindex(self):
        assert latest_bar_date(_frame(["2026-08-26", "2026-08-28"])) == date(2026, 8, 28)

    def test_datetime_column(self):
        df = pd.DataFrame(
            {"datetime": pd.to_datetime(["2026-08-27", "2026-08-28"]), "close": [1.0, 2.0]}
        )
        assert latest_bar_date(df) == date(2026, 8, 28)


# ---------------------------------------------------------------------------
# assert_nonempty
# ---------------------------------------------------------------------------
class TestAssertNonempty:
    def test_pass_on_data(self):
        assert_nonempty(_frame(["2026-08-28"]), source="unit")

    def test_raise_on_empty(self):
        with pytest.raises(HexEmptyDataError) as ei:
            assert_nonempty(_frame([]), source="sina", symbols=["rb0"])
        assert "0 行" in str(ei.value)
        assert ei.value.source == "sina"

    def test_raise_on_none(self):
        with pytest.raises(HexEmptyDataError):
            assert_nonempty(None, source="unit")


# ---------------------------------------------------------------------------
# assert_fresh
# ---------------------------------------------------------------------------
class TestAssertFresh:
    def test_fresh_passes(self):
        df = _frame(["2026-08-27", "2026-08-28"])
        assert assert_fresh(df, "2026-08-28", today=date(2026, 8, 29)) == date(2026, 8, 28)

    def test_stale_raises(self):
        """停摆事故核心：最新 bar 远早于请求结束日，必须抛错而非静默成功。"""
        df = _frame(["2026-06-20", "2026-06-29"])
        with pytest.raises(HexStaleDataError) as ei:
            assert_fresh(df, "2026-08-28", source="sina", today=date(2026, 8, 29))
        assert ei.value.source == "sina"
        assert ei.value.latest == "2026-06-29"
        assert ei.value.expected == "2026-08-28"

    def test_weekend_tolerance(self):
        """周末/短假：end 为周六，最新 bar 为周五，容忍窗口内应放行。"""
        df = _frame(["2026-08-27", "2026-08-28"])
        assert assert_fresh(df, "2026-08-31", today=date(2026, 8, 31)) == date(2026, 8, 28)

    def test_historical_backfill_exempt(self):
        """历史回填不应被新鲜度判定误杀。"""
        df = _frame(["2020-03-02", "2020-03-06"])
        # 精确断言：返回实际最新日，且不抛错
        assert assert_fresh(df, "2020-03-06", today=date(2026, 8, 29)) == date(2020, 3, 6)

    def test_custom_tolerance(self):
        df = _frame(["2026-08-20"])
        # 默认 5 天容差下 08-20 vs end 08-28 应报警
        with pytest.raises(HexStaleDataError):
            assert_fresh(df, "2026-08-28", today=date(2026, 8, 29))
        # 放宽到 15 天则放行
        assert assert_fresh(df, "2026-08-28", max_stale_days=15, today=date(2026, 8, 29)) == date(2026, 8, 20)


# ---------------------------------------------------------------------------
# check_fetch_result
# ---------------------------------------------------------------------------
class TestCheckFetchResult:
    def test_empty_wins_over_stale(self):
        """空结果优先报 HexEmptyDataError，不混淆为陈旧。"""
        with pytest.raises(HexEmptyDataError):
            check_fetch_result(_frame([]), "2026-08-28", today=date(2026, 8, 29))

    def test_skip_freshness(self):
        df = _frame(["2026-06-29"])
        assert check_fetch_result(
            df, "2026-08-28", check_freshness=False, today=date(2026, 8, 29)
        ) == date(2026, 6, 29)


# ---------------------------------------------------------------------------
# validate_bars / BarFrame 空帧门禁
# ---------------------------------------------------------------------------
class TestEmptyFrameRejected:
    def test_validate_bars_rejects_empty_by_default(self):
        empty = _frame([])[REQUIRED]
        with pytest.raises(HexEmptyDataError):
            BarFrame(df=empty, freq="1d", source="unit").validate()

    def test_allow_empty_opt_in(self):
        empty = _frame([])[REQUIRED]
        bf = BarFrame(df=empty, freq="1d", source="unit").validate(allow_empty=True)
        assert bf.length == 0

    def test_nonempty_still_validates(self):
        df = _frame(["2026-08-27", "2026-08-28"])[REQUIRED]
        assert BarFrame(df=df, freq="1d", source="unit").validate().length == 2


def test_default_tolerance_value():
    """默认容差覆盖周末 + 3 天连休。"""
    assert DEFAULT_MAX_STALE_DAYS == 5
