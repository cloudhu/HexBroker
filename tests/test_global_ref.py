"""L4 回归：外盘对齐必须用 asof（容忍时分/时区错位），非精确 reindex。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hexbroker.feature.global_ref import align_global_to_inner


def test_align_global_to_inner_asof_tolerates_timestamp_mismatch():
    # 外盘：日级收盘（00:00）
    g_idx = pd.date_range("2021-01-04", periods=3, freq="D")
    global_close = pd.Series([10.0, 20.0, 30.0], index=g_idx)
    # 内盘：交易时段（09:00），与外盘时间戳不精确匹配
    inner_idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2021-01-04 09:00:00"),
            pd.Timestamp("2021-01-05 09:00:00"),
            pd.Timestamp("2021-01-06 09:00:00"),
        ]
    )
    # 旧实现：shifted.reindex(inner_idx).ffill() → 全 NaN（无精确匹配）→ 外盘特征整列失效
    old = global_close.shift(1).reindex(inner_idx).ffill()
    assert old.isna().all()
    # 新实现：asof 对齐 → t 日取外盘 t-1 收盘（shift(1) 后）
    res = align_global_to_inner(global_close, inner_idx)
    assert not res.isna().all()
    # shifted = [NaN, 10, 20]；asof 到各内盘交易日 => [NaN, 10, 20]
    assert np.isnan(res.iloc[0])
    assert res.iloc[1] == pytest.approx(10.0)
    assert res.iloc[2] == pytest.approx(20.0)
