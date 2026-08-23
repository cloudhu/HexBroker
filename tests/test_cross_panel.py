"""L5 回归：close 宽表 pivot 前按短名去重，杜绝不同合约静默平均。"""

from __future__ import annotations

import pandas as pd
import pytest

from hexbroker.feature.cross import _panel_close_wide


class _BF:
    def __init__(self, df):
        self.df = df


def test_panel_close_wide_dedups_colliding_short_names():
    idx = pd.date_range("2020-01-01", periods=2, freq="D")
    rows = []
    # au0 与 au2506 均归一为 'au'；ag0 → 'ag'
    closes = {"au0": 100.0, "au2506": 200.0, "ag0": 50.0}
    for sym in ["au0", "au2506", "ag0"]:
        c = closes[sym]
        for t in idx:
            rows.append((sym, t, c))
    df = pd.DataFrame(rows, columns=["symbol", "datetime", "close"]).set_index(
        ["symbol", "datetime"]
    )
    panel = _panel_close_wide(_BF(df))
    # 列应为去重后的短名，不应出现原始长名
    assert set(panel.columns) == {"au", "ag"}
    assert "au2506" not in panel.columns
    # 'au' 列应为代表合约 au0（连续主力，全名以 0 结尾）的收盘，而非两者均值 150
    assert panel["au"].iloc[0] == pytest.approx(100.0)
    assert panel["ag"].iloc[0] == pytest.approx(50.0)
