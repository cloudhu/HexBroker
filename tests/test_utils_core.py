"""代码审核补覆盖：hexbroker/utils 核心工具测试。

覆盖 registry / seed / timeutil / io / fingerprint，冒烟 + 边界 + 确定性。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from hexbroker.utils import io
from hexbroker.utils.fingerprint import model_id
from hexbroker.utils.registry import build, get_registered, list_registered, register
from hexbroker.utils.seed import set_global_seed
from hexbroker.utils.timeutil import (
    can_execute_on_bar,
    make_bar_endindex,
    next_bar_ts,
    normalize_ts,
    prev_bar_ts,
    to_serializable,
)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registry_register_build_roundtrip():
    @register("test_audit_fns_sum")
    def _sum(a, b):
        return a + b

    assert build("test_audit_fns_sum", 2, 3) == 5
    assert get_registered("test_audit_fns_sum") is _sum
    assert "test_audit_fns_sum" in list_registered()


def test_registry_duplicate_raises():
    from hexbroker import HexConfigError

    @register("test_audit_fns_dup")
    def _first():
        return 1

    with pytest.raises(HexConfigError, match="冲突"):

        @register("test_audit_fns_dup")
        def _second():
            return 2


def test_registry_unregistered_raises():
    from hexbroker import HexConfigError

    with pytest.raises(HexConfigError, match="未注册"):
        build("test_audit_fns_nonexistent")


# ---------------------------------------------------------------------------
# seed 确定性
# ---------------------------------------------------------------------------
def test_seed_deterministic():
    set_global_seed(123)
    a = np.random.normal(size=50)
    set_global_seed(123)
    b = np.random.normal(size=50)
    assert np.array_equal(a, b)
    # 不同 seed 序列不同
    set_global_seed(456)
    c = np.random.normal(size=50)
    assert not np.array_equal(a, c)


# ---------------------------------------------------------------------------
# timeutil
# ---------------------------------------------------------------------------
def test_normalize_ts_strips_tz():
    ts = pd.Timestamp("2024-01-02 15:00+08:00")
    out = normalize_ts(ts)
    assert out.tz is None
    assert out == pd.Timestamp("2024-01-02 15:00")


def test_next_prev_bar_ts():
    ts = pd.Timestamp("2024-01-02 15:00")
    assert next_bar_ts(ts, "1d") == pd.Timestamp("2024-01-03 15:00")
    assert next_bar_ts(ts, "60m") == pd.Timestamp("2024-01-02 16:00")
    assert prev_bar_ts(ts, "1d") == pd.Timestamp("2024-01-01 15:00")


def test_can_execute_on_bar_forbids_same_bar():
    sig = pd.Timestamp("2024-01-02 15:00")
    # 同一根 bar 收盘时点（=信号时点）不允许成交
    assert not can_execute_on_bar(sig, sig, "1d")
    # 下一根 bar 才允许
    assert can_execute_on_bar(sig, next_bar_ts(sig, "1d"), "1d")


def test_make_bar_endindex_sorted_and_naive():
    idx = make_bar_endindex(
        [pd.Timestamp("2024-01-03 09:00+08:00"), pd.Timestamp("2024-01-02 09:00")], freq="1d"
    )
    assert idx.name == "datetime"
    assert list(idx) == [
        pd.Timestamp("2024-01-02 09:00"),
        pd.Timestamp("2024-01-03 09:00"),
    ]
    assert idx.tz is None


def test_to_serializable_iso():
    assert to_serializable(pd.Timestamp("2024-01-02 15:00")) == "2024-01-02T15:00:00"


# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------
def test_io_parquet_roundtrip(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]}, index=pd.date_range("2024-01-01", periods=3))
    p = tmp_path / "t.parquet"
    io.write_parquet(df, p)
    back = io.read_parquet(p)
    pd.testing.assert_frame_equal(back, df, check_freq=False)  # parquet 不保留 freq


def test_io_json_roundtrip(tmp_path):
    p = tmp_path / "t.json"
    io.write_json({"k": [1, 2], "s": "x"}, p)
    assert io.read_json(p) == {"k": [1, 2], "s": "x"}


def test_io_ensure_dir(tmp_path):
    d = tmp_path / "a" / "b"
    out = io.ensure_dir(d)
    assert out.exists() and out.is_dir()


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------
def test_model_id_deterministic_and_sensitive():
    kw = dict(config={"horizon": 5, "symbols": ["au0", "ag0"], "seed": 42}, data_range=("2018-01-01", "2024-12-31"), git_sha="abc123")
    id1 = model_id(**kw)
    id2 = model_id(**kw)
    assert id1 == id2
    id3 = model_id(**{**kw, "git_sha": "abc124"})
    assert id1 != id3
