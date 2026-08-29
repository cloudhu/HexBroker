"""年度口径一致性检测测试（P0-10 固化件）。"""
from __future__ import annotations

import pandas as pd
import pytest

from hexbroker.data.caliber import (
    BoundaryGap,
    boundary_fake_gaps,
    nominal_suspect_years,
)


def _s(values, dates):
    return pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)


YEARS_22_24 = {
    2022: _s([5500.0, 5554.96], ["2022-12-28", "2022-12-30"]),
    2023: _s([4063.0, 4002.0], ["2023-01-03", "2023-12-29"]),
    2024: _s([5451.02, 5500.0], ["2024-01-02", "2024-01-03"]),
}


class TestBoundaryFakeGaps:
    def test_rb0_like_breaks_detected(self):
        """rb0 真实断裂形态：2023 名义价夹在两个后复权年之间。"""
        gaps = boundary_fake_gaps(YEARS_22_24)
        assert [g.from_year for g in gaps] == [2022, 2023]
        assert [g.to_year for g in gaps] == [2023, 2024]
        assert gaps[0].jump == pytest.approx(4063.0 / 5554.96 - 1.0)
        assert gaps[1].jump == pytest.approx(5451.02 / 4002.0 - 1.0)

    def test_clean_series_no_gaps(self):
        clean = {
            2022: _s([100.0, 102.0], ["2022-12-28", "2022-12-30"]),
            2023: _s([103.0, 104.0], ["2023-01-03", "2023-12-29"]),
        }
        assert boundary_fake_gaps(clean) == []

    def test_missing_year_skipped(self):
        """中间年度缺失（2020 隔离）：2019->2021 跨界拼接不判罪。"""
        sparse = {
            2019: _s([100.0, 102.0], ["2019-12-28", "2019-12-31"]),
            2021: _s([140.0, 141.0], ["2021-01-04", "2021-01-05"]),
        }
        assert boundary_fake_gaps(sparse) == []

    def test_rollover_flag_recorded_not_exempted(self):
        """换月标记仅记录不豁免：真后复权在换月日也应连续。"""
        roll = {
            2022: _s([False], ["2022-12-30"]),
            2023: _s([True], ["2023-01-03"]),
        }
        broke = {
            2022: _s([100.0], ["2022-12-30"]),
            2023: _s([130.0], ["2023-01-03"]),
        }
        gaps = boundary_fake_gaps(broke, roll_by_year=roll)
        assert len(gaps) == 1
        assert isinstance(gaps[0], BoundaryGap)
        assert gaps[0].rollover_prev is False and gaps[0].rollover_next is True

    def test_empty_years_ignored(self):
        with_empty = dict(YEARS_22_24)
        with_empty[2025] = pd.Series(dtype=float)
        assert len(boundary_fake_gaps(with_empty)) == 2

    def test_zero_prev_guarded(self):
        zero = {
            2022: _s([0.0], ["2022-12-30"]),
            2023: _s([100.0], ["2023-01-03"]),
        }
        assert boundary_fake_gaps(zero) == []

    def test_custom_threshold(self):
        mild = {
            2022: _s([100.0], ["2022-12-30"]),
            2023: _s([113.0], ["2023-01-03"]),  # +13%
        }
        assert boundary_fake_gaps(mild) != []
        assert boundary_fake_gaps(mild, max_jump=0.15) == []


class TestNominalSuspectYears:
    def test_flat_one_year_detected(self):
        adj = {
            2022: _s([1353.0, 1360.0], ["2022-12-28", "2022-12-30"]),
            2023: _s([4063.0, 4002.0], ["2023-01-03", "2023-12-29"]),
        }
        nom = _s([1000.0, 1005.0, 4063.0, 4002.0],
                 ["2022-12-28", "2022-12-30", "2023-01-03", "2023-12-29"])
        assert nominal_suspect_years(adj, nom) == [2023]

    def test_small_spread_product_not_forced(self):
        """k≈1 但漂移超容差（ag/au/m 形态）：不判嫌疑年。"""
        adj = {2018: _s([100.1, 99.9], ["2018-06-01", "2018-06-02"])}
        nom = _s([100.0, 100.0], ["2018-06-01", "2018-06-02"])
        assert nominal_suspect_years(adj, nom) == []

    def test_empty_year_skipped(self):
        adj = {2023: pd.Series(dtype=float)}
        nom = _s([1.0], ["2023-01-03"])
        assert nominal_suspect_years(adj, nom) == []

    def test_no_overlap_year_skipped(self):
        adj = {2023: _s([100.0], ["2023-01-03"])}
        nom = _s([100.0], ["2020-01-03"])
        assert nominal_suspect_years(adj, nom) == []

    def test_duplicated_nominal_index_deduped(self):
        """名义价索引重复：去重后正常计算（k≡1 → 嫌疑年），不崩溃。"""
        adj = {2023: _s([100.0, 101.0], ["2023-01-03", "2023-01-04"])}
        nom = pd.concat([
            _s([100.0, 101.0], ["2023-01-03", "2023-01-04"]),
            _s([100.0, 101.0], ["2023-01-03", "2023-01-04"]),
        ]).sort_index()
        assert nominal_suspect_years(adj, nom) == [2023]
