"""P2-4 回归测试：tqsdk 源持仓量口径（``close_oi`` vs ``open_oi``）。

**根因**：tqsdk 日 K 的 ``open_oi`` 语义是「K 线**起始**时刻的持仓量」，即
**上一交易日收盘 OI**；``close_oi`` 才是「K 线**结束**时刻的持仓量」，即
**当日收盘 OI**（与 pandadata 权威源同口径）。旧实现
``"open_interest": float(r.get("open_oi", 0) or 0)`` 误用 ``open_oi``，
导致主湖 ``open_interest`` 相对权威源**系统性滞后一个交易日**
（实测湖 OI(t) == 权威 OI(t-1)，逐位精确，波及 16/18 品种）。

本文件把该断根钉死：主口径必须取 ``close_oi``，仅在缺失/NaN/0 时降级回退
``open_oi``（兼容旧版 tqsdk 无该字段，语义为上一交易日收盘 OI）。

全部用最小 DataFrame 直接调 ``TqsdkSource._klines_to_frame``，**不触网**。
"""
from __future__ import annotations

import pandas as pd

from hexbroker.data.sources.tqsdk_source import TqsdkSource

# tqsdk 官方示例值（api.py 第 694-695 行）：open_oi=27354 / close_oi=27355。
# 两者**必须取不同值**，否则用例无法区分主口径与回退口径，断根会失效。
OI_OPEN = 27354
OI_CLOSE = 27355


def _ts(day: str) -> int:
    """'YYYY-MM-DD' → tqsdk ``datetime`` 列口径的纳秒时间戳（UTC）。"""
    return int(pd.Timestamp(f"{day} 15:00:00", tz="Asia/Shanghai").value)


def _klines(rows: list[dict]) -> pd.DataFrame:
    """把行字典列表拼成 tqsdk ``get_kline_serial`` 形态的 DataFrame。"""
    return pd.DataFrame(rows)


def _base_row(day: str, **over) -> dict:
    """构造一根合法日 K（缺字段的行由上层 ``**over`` 覆盖/剔除后传入）。"""
    row = {
        "datetime": _ts(day),
        "open": 3000.0,
        "high": 3010.0,
        "low": 2990.0,
        "close": 3005.0,
        "volume": 100.0,
        "open_oi": OI_OPEN,
        "close_oi": OI_CLOSE,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# 断根用例：三种输入形态
# ---------------------------------------------------------------------------
def test_open_interest_uses_close_oi_not_open_oi():
    """【断根】close_oi 与 open_oi 同时存在且取值不同 → 必须等于 close_oi。

    这是本次修复的核心断言。两者取值刻意不同（27355 vs 27354），
    若实现回退到 open_oi，本用例立即失败。
    """
    df = TqsdkSource._klines_to_frame(_klines([_base_row("2026-09-01")]), "ag0")

    assert len(df) == 1
    assert df["open_interest"].iloc[0] == float(OI_CLOSE)
    assert df["open_interest"].iloc[0] != float(OI_OPEN)


def test_open_interest_falls_back_to_open_oi_when_close_oi_absent():
    """仅提供 open_oi（模拟旧版 tqsdk 无 close_oi 字段）→ 降级回退，不报错。

    回退值语义为**上一交易日收盘 OI**，属降级口径，仅用于保持列非空。
    """
    row = _base_row("2026-09-01")
    del row["close_oi"]
    df = TqsdkSource._klines_to_frame(_klines([row]), "ag0")

    assert len(df) == 1
    assert df["open_interest"].iloc[0] == float(OI_OPEN)


def test_open_interest_falls_back_when_close_oi_is_zero_or_nan():
    """close_oi 为 0 / NaN / None 时按「缺失」处理 → 回退 open_oi，不抛异常。"""
    for bad in (0, 0.0, float("nan"), None, pd.NA):
        df = TqsdkSource._klines_to_frame(
            _klines([_base_row("2026-09-01", close_oi=bad)]), "ag0"
        )
        assert df["open_interest"].iloc[0] == float(OI_OPEN), f"close_oi={bad!r} 未回退"


def test_open_interest_zero_when_both_missing():
    """两者皆缺失 → 结果为 0，不抛异常。"""
    row = _base_row("2026-09-01")
    del row["close_oi"]
    del row["open_oi"]
    df = TqsdkSource._klines_to_frame(_klines([row]), "ag0")

    assert len(df) == 1
    assert df["open_interest"].iloc[0] == 0.0


def test_open_interest_zero_when_both_zero():
    """两者皆为 0 → 结果为 0，不抛异常。"""
    df = TqsdkSource._klines_to_frame(
        _klines([_base_row("2026-09-01", open_oi=0, close_oi=0)]), "ag0"
    )

    assert df["open_interest"].iloc[0] == 0.0


# ---------------------------------------------------------------------------
# 序列级：杜绝「滞后一行」
# ---------------------------------------------------------------------------
def test_open_interest_series_matches_close_oi_day_by_day():
    """多日序列：open_interest 必须逐日等于当日 close_oi（不得整体错位一行）。

    权威 OI 取递增序列，close_oi 与 open_oi 错开一日构造，
    若实现误用 open_oi，结果会整体右移一位而被本用例捕获。
    """
    authoritative = [100.0 + i * 10 for i in range(5)]  # 权威：100,110,120,130,140
    rows = []
    for i, oi in enumerate(authoritative):
        day = f"2026-09-0{i + 1}"
        rows.append(
            _base_row(
                day,
                close_oi=oi,                                   # 当日收盘 OI
                open_oi=authoritative[i - 1] if i else 90.0,   # 起始 OI = 前一日收盘
            )
        )

    df = TqsdkSource._klines_to_frame(_klines(rows), "cu0")

    assert list(df["open_interest"]) == authoritative
    # 显式排除「滞后一行」：结果不得等于 open_oi 序列
    assert list(df["open_interest"]) != [90.0] + authoritative[:-1]


def test_open_interest_fallback_per_row_is_independent():
    """逐行独立判定：close_oi 只在**部分行**缺失时，仅这些行回退，其余保持主口径。"""
    rows = [
        _base_row("2026-09-01", open_oi=100.0, close_oi=110.0),
        _base_row("2026-09-02", open_oi=110.0, close_oi=0),      # 缺失 → 回退 110
        _base_row("2026-09-03", open_oi=120.0, close_oi=130.0),
    ]
    df = TqsdkSource._klines_to_frame(_klines(rows), "ni0")

    assert list(df["open_interest"]) == [110.0, 110.0, 130.0]


# ---------------------------------------------------------------------------
# 副作用守卫：本次修复不得波及其他列
# ---------------------------------------------------------------------------
def test_other_columns_unchanged_by_oi_fix():
    """volume / amount / adj_close / raw_close / OHLC 一律不受 OI 口径改动影响。"""
    df = TqsdkSource._klines_to_frame(_klines([_base_row("2026-09-01")]), "rb0")

    assert df["open"].iloc[0] == 3000.0
    assert df["high"].iloc[0] == 3010.0
    assert df["low"].iloc[0] == 2990.0
    assert df["close"].iloc[0] == 3005.0
    assert df["volume"].iloc[0] == 100.0
    assert df["amount"].iloc[0] == 0.0        # tqsdk 免费接口无成交额
    assert df["adj_close"].iloc[0] == 3005.0  # 禁止前复权：初值等同 close
    assert df["raw_close"].iloc[0] == 3005.0


def test_frame_shape_and_index_contract():
    """契约不变：MultiIndex(symbol, datetime) 且按时间升序。"""
    rows = [_base_row("2026-09-02"), _base_row("2026-09-01")]
    df = TqsdkSource._klines_to_frame(_klines(rows), "ag0")

    assert list(df.index.names) == ["symbol", "datetime"]
    assert df.index.get_level_values("symbol").unique().tolist() == ["ag0"]
    assert list(df.index.get_level_values("datetime")) == sorted(
        df.index.get_level_values("datetime")
    )
