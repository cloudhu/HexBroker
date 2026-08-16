"""T01 换月复权测试：后向复权后换月处收益无跳空。"""

import numpy as np

from hexbroker.data.contract import ContractStitcher
from hexbroker.data.schema import BarFrame


def test_backward_adjust_removes_rollover_jump():
    rng = np.random.default_rng(7)
    n = 600
    # 基础价格随机游走
    base = 1000 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, n)))
    raw_close = base.copy()
    is_roll = np.zeros(n, dtype=bool)
    # 在若干位置注入换月跳空（新合约相对旧合约 +6% 跳变）
    for r in range(120, n, 120):
        raw_close[r:] = raw_close[r:] * 1.06
        is_roll[r] = True

    dates = __import__("pandas").date_range("2020-01-01", periods=n, freq="D")
    df = __import__("pandas").DataFrame(
        {
            "open": raw_close,
            "high": raw_close * 1.005,
            "low": raw_close * 0.995,
            "close": raw_close,
            "volume": rng.integers(1000, 5000, n).astype(float),
            "amount": raw_close * 1000,
            "open_interest": rng.integers(1000, 9000, n).astype(float),
            "adj_close": raw_close,
            "raw_close": raw_close,
            "limit_up": False,
            "limit_down": False,
            "is_rollover": is_roll,
        },
        index=__import__("pandas").MultiIndex.from_arrays(
            [["SHFE.cu"] * n, dates], names=["symbol", "datetime"]
        ),
    )
    bf = BarFrame(df=df, freq="1d")
    stitcher = ContractStitcher(main_rule="open_interest", adjust_method="backward")
    out = stitcher.stitch(bf)

    adj = out.by_symbol("SHFE.cu")["adj_close"].to_numpy()
    # 复权后的「正常」收益标准差
    adj_ret = np.diff(adj) / adj[:-1]
    normal_std = np.std(adj_ret[~is_roll[1:]])
    # 换月处的复权收益跳变应可忽略
    jump_at_roll = np.abs(adj_ret[is_roll[1:]])
    assert np.all(jump_at_roll < 3 * normal_std), "换月处复权收益跳空超 3σ"

    # 原始 raw_close 跳变应被消除（adj 连续）
    assert jump_at_roll.max() < 0.01 * np.mean(adj)

    # 换月日期可打印
    rolls = stitcher.rollover_dates(out)
    assert "SHFE.cu" in rolls
    assert len(rolls["SHFE.cu"]) >= 3


def test_no_rollover_is_identity():
    rng = np.random.default_rng(3)
    n = 200
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    dates = __import__("pandas").date_range("2020-01-01", periods=n, freq="D")
    df = __import__("pandas").DataFrame(
        {
            "open": close, "high": close * 1.005, "low": close * 0.995,
            "close": close, "volume": np.full(n, 1000.0),
            "amount": close * 1000, "open_interest": np.full(n, 1000.0),
            "adj_close": close, "raw_close": close,
            "limit_up": False, "limit_down": False, "is_rollover": False,
        },
        index=__import__("pandas").MultiIndex.from_arrays(
            [["SHFE.cu"] * n, dates], names=["symbol", "datetime"]
        ),
    )
    bf = BarFrame(df=df, freq="1d")
    out = ContractStitcher().stitch(bf)
    adj = out.by_symbol("SHFE.cu")["adj_close"].to_numpy()
    assert np.allclose(adj, close)
