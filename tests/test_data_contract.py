"""T01 数据契约测试：``validate_bars`` 红线。"""

import numpy as np
import pandas as pd
import pytest

from hexbroker.data.schema import BarFrame, validate_bars
from hexbroker import HexDataError


def _make_df(n=100, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
            "amount": rng.integers(1e6, 5e6, n).astype(float),
            "open_interest": rng.integers(1000, 9000, n).astype(float),
            "adj_close": close,
            "raw_close": close,
            "limit_up": False,
            "limit_down": False,
            "is_rollover": False,
        },
        index=pd.MultiIndex.from_arrays(
            [["SHFE.cu"] * n, dates], names=["symbol", "datetime"]
        ),
    )
    return df


def test_valid_bars_pass():
    df = _make_df()
    assert validate_bars(df) is True
    bf = BarFrame(df=df, freq="1d")
    assert bf.symbols == ["SHFE.cu"]


def test_high_less_than_low_raises():
    df = _make_df()
    df = df.copy()
    df.loc[df.index[5], "high"] = df.iloc[5]["low"] - 1.0
    with pytest.raises(HexDataError):
        validate_bars(df)


def test_duplicate_index_raises():
    df = _make_df()
    # 强制制造重复索引（通过重建索引）
    idx = df.index.tolist()
    idx[10] = idx[9]
    df2 = df.copy()
    df2.index = pd.MultiIndex.from_tuples(idx, names=["symbol", "datetime"])
    with pytest.raises(HexDataError):
        validate_bars(df2)


def test_nonpositive_price_raises():
    df = _make_df()
    df = df.copy()
    df.loc[df.index[3], "close"] = -5.0
    with pytest.raises(HexDataError):
        validate_bars(df)
