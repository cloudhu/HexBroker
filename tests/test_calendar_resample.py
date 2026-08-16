"""T01 日历 + 重采样测试：60m 不跨夜盘边界聚合。"""

import numpy as np
import pandas as pd

from hexbroker.data.resample import resample_to_freq
from hexbroker.data.schema import BarFrame


def _intraday_df():
    """构造夜盘(21:00-22:59) + 次日日盘(09:00-10:59) 的 1m 数据（不含夜盘结束边界 bar）。"""
    night = pd.date_range("2024-01-02 21:00", periods=120, freq="1min")  # 21:00..22:59
    day = pd.date_range("2024-01-03 09:00", periods=120, freq="1min")    # 09:00..10:59
    idx = night.append(day)
    n = len(idx)
    rng = np.random.default_rng(0)
    close = 5000 * np.exp(np.cumsum(rng.normal(0, 0.0002, n)))
    df = pd.DataFrame(
        {
            "open": close, "high": close * 1.0005, "low": close * 0.9995,
            "close": close, "volume": np.full(n, 100.0),
            "amount": close * 100, "open_interest": np.full(n, 500.0),
            "raw_close": close, "limit_up": False, "limit_down": False,
            "is_rollover": False,
        },
        index=pd.MultiIndex.from_arrays(
            [["SHFE.cu"] * n, idx], names=["symbol", "datetime"]
        ),
    )
    return df


def test_resample_60m_no_cross_night():
    df = _intraday_df()
    bf = BarFrame(df=df, freq="1m")
    out = resample_to_freq(bf, "60m")
    ts = out.by_symbol("SHFE.cu").index.get_level_values("datetime")  # DatetimeIndex

    # 期望 4 根 60m bar：夜盘 22:00, 23:00；日盘 10:00, 11:00
    expected = [
        pd.Timestamp("2024-01-02 22:00"),
        pd.Timestamp("2024-01-02 23:00"),
        pd.Timestamp("2024-01-03 10:00"),
        pd.Timestamp("2024-01-03 11:00"),
    ]
    assert list(ts) == expected, f"得到 {list(ts)}"


def test_resample_no_midnight_bars():
    df = _intraday_df()
    bf = BarFrame(df=df, freq="1m")
    out = resample_to_freq(bf, "60m")
    ts = out.by_symbol("SHFE.cu").index.get_level_values("datetime")
    # 不应出现跨午夜的 bar（如 00:00 / 01:00）
    for t in ts:
        assert not (0 <= t.hour < 9), f"出现跨夜盘边界的 bar: {t}"


def test_session_key_midnight_belongs_to_prev_night():
    """跨零点 bar（00:00-02:30）归属前一日夜盘，offset 从 21:00 起算（不为负）。"""
    from hexbroker.data.resample import _session_key

    # 夜盘 21:00 起点
    assert _session_key(pd.Timestamp("2024-01-02 21:30")) == ("2024-01-02-night", 30)
    assert _session_key(pd.Timestamp("2024-01-02 23:30")) == ("2024-01-02-night", 150)
    # 跨零点：归属前一日 night，offset 继续累加
    assert _session_key(pd.Timestamp("2024-01-03 00:30")) == ("2024-01-02-night", 210)
    assert _session_key(pd.Timestamp("2024-01-03 02:00")) == ("2024-01-02-night", 300)
    assert _session_key(pd.Timestamp("2024-01-03 02:30")) == ("2024-01-02-night", 330)
    # 日盘
    assert _session_key(pd.Timestamp("2024-01-03 09:00")) == ("2024-01-03-day", 0)
    assert _session_key(pd.Timestamp("2024-01-03 14:30")) == ("2024-01-03-day", 330)


def test_resample_cross_midnight_night_session():
    """夜盘 21:00 至次日 02:30（au/ag）的 60m 桶：跨零点时段正确分桶。"""
    idx = pd.date_range("2024-01-02 21:00", "2024-01-03 02:30", freq="1min")  # 330 根
    n = len(idx)
    close = np.full(n, 5000.0)
    df = pd.DataFrame(
        {
            "open": close, "high": close * 1.0005, "low": close * 0.9995,
            "close": close, "volume": np.full(n, 100.0),
            "amount": close * 100, "open_interest": np.full(n, 500.0),
            "raw_close": close, "limit_up": False, "limit_down": False,
            "is_rollover": False,
        },
        index=pd.MultiIndex.from_arrays([["SHFE.au"] * n, idx], names=["symbol", "datetime"]),
    )
    out = resample_to_freq(BarFrame(df=df, freq="1m"), "60m")
    ts = out.by_symbol("SHFE.au").index.get_level_values("datetime")
    # 6 个 60m 桶：22:00 / 23:00 / 次日 00:00 / 01:00 / 02:00 / 03:00
    expected = [
        pd.Timestamp("2024-01-02 22:00"),
        pd.Timestamp("2024-01-02 23:00"),
        pd.Timestamp("2024-01-03 00:00"),
        pd.Timestamp("2024-01-03 01:00"),
        pd.Timestamp("2024-01-03 02:00"),
        pd.Timestamp("2024-01-03 03:00"),
    ]
    assert list(ts) == expected, f"得到 {list(ts)}"
