"""D2：`DataLake.save_processed` 年度分区**缩水门禁**。

背景（2026-08-23 生产事故）：``save_processed`` 按年整区覆盖写，新浪旧端点
（数据冻结于 2024-07-17）的陈数据经此把 ag0/au0/m0 2024 分区从 243/243/242
行打回 131/131/130 行（−46%）。本文件用 ``tmp_path`` 复现并锁死门禁行为。

铁律：全部走 ``tmp_path``，绝不触碰生产湖（``data/raw/processed``）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from hexbroker.data.schema import BarFrame
from hexbroker.data.store import (
    DEFAULT_SHRINK_TOLERANCE,
    DataLake,
    PartitionShrinkError,
)
from hexbroker.utils.io import read_parquet, write_parquet

SYM = "ag0"
FREQ = "1d"
YEAR = 2024


# ---- 夹具 ----------------------------------------------------------------

@pytest.fixture
def lake(tmp_path) -> DataLake:
    return DataLake(root=str(tmp_path))


def _bars(dates, symbol: str = SYM, freq: str = FREQ) -> BarFrame:
    """按给定日期序列构造最小合法 BarFrame（MultiIndex(symbol, datetime)）。"""
    n = len(dates)
    df = pd.DataFrame(
        {
            "symbol": [symbol] * n,
            "datetime": pd.to_datetime(pd.Index(dates)),
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [10.0] * n,
            "amount": [1000.0] * n,
            "open_interest": [1.0] * n,
            "adj_close": [100.5 + i for i in range(n)],
            "raw_close": [100.5 + i for i in range(n)],
        }
    )
    return BarFrame(
        df=df.set_index(["symbol", "datetime"]), freq=freq, source="pytest"
    )


def _dates(n: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def _disk_rows(lake: DataLake, symbol: str = SYM, year: int = YEAR) -> int:
    return len(read_parquet(lake._path("processed", symbol, FREQ, year)))


def _seed_partition(lake: DataLake, n: int, year: int = YEAR):
    """预先在磁盘落一个 n 行的年度分区（模拟陈数据写入前的健康分区）。"""
    path = lake._path("processed", SYM, FREQ, year)
    write_parquet(_bars(_dates(n)).df.reset_index(), path)
    return path


# ---- 1) 正常写入不触发 ---------------------------------------------------

def test_row_count_growth_does_not_trigger(lake):
    """行数不减少（243 → 261）→ 放行。"""
    _seed_partition(lake, 243)
    lake.save_processed(_bars(_dates(261)))
    assert _disk_rows(lake) == 261


def test_identical_row_count_does_not_trigger(lake):
    """行数持平（243 → 243）→ 放行。"""
    _seed_partition(lake, 243)
    lake.save_processed(_bars(_dates(243)))
    assert _disk_rows(lake) == 243


# ---- 2) 模拟本次事故：先检查后写盘 ---------------------------------------

def test_incident_repro_261_to_5_blocked_and_partition_untouched(lake):
    """261 行分区被 5 行覆盖 → 抛错，且**磁盘仍是 261 行**。

    这条断言是门禁的核心价值：证明检查发生在写盘**之前**，没有先污染再报错。
    """
    _seed_partition(lake, 261)
    assert _disk_rows(lake) == 261

    with pytest.raises(PartitionShrinkError) as ei:
        lake.save_processed(_bars(_dates(5)))

    # 异常携带完整现场
    err = ei.value
    assert (err.symbol, err.freq, err.year) == (SYM, FREQ, YEAR)
    assert (err.n_before, err.n_after) == (261, 5)
    assert err.tolerance == DEFAULT_SHRINK_TOLERANCE
    assert "allow_shrink=True" in str(err)

    # 分区未被污染
    assert _disk_rows(lake) == 261


# ---- 3) allow_shrink=True 显式放行 ---------------------------------------

def test_allow_shrink_true_writes_anyway(lake):
    _seed_partition(lake, 261)
    lake.save_processed(_bars(_dates(5)), allow_shrink=True)
    assert _disk_rows(lake) == 5


# ---- 4) 首次写入不检查 ---------------------------------------------------

def test_first_write_skips_check(lake):
    """目标分区文件不存在 → 跳过检查，直接写。"""
    assert not lake._path("processed", SYM, FREQ, YEAR).exists()
    lake.save_processed(_bars(_dates(5)))  # 不抛
    assert _disk_rows(lake) == 5


def test_unrelated_year_write_not_blocked(lake):
    """另一年度有 261 行不影响本年首次写入（门禁按年度分区独立判定）。"""
    _seed_partition(lake, 261, year=2023)
    other = _bars(_dates(5, start="2024-01-01"))
    lake.save_processed(other)
    assert _disk_rows(lake, year=2024) == 5


# ---- 5) 2% 边界内外 ------------------------------------------------------

def test_shrink_1pct_within_tolerance_allowed(lake):
    """1% 缩水（200 → 199）在容忍度内 → 放行。"""
    _seed_partition(lake, 200)
    lake.save_processed(_bars(_dates(199)))
    assert _disk_rows(lake) == 199


def test_shrink_5pct_beyond_tolerance_blocked(lake):
    """5% 缩水（200 → 190）超容忍度 → 拦下，分区保持 200 行。"""
    _seed_partition(lake, 200)
    with pytest.raises(PartitionShrinkError) as ei:
        lake.save_processed(_bars(_dates(190)))
    assert (ei.value.n_before, ei.value.n_after) == (200, 190)
    assert _disk_rows(lake) == 200


def test_shrink_exactly_at_boundary_allowed(lake):
    """恰好落在阈值上（200 → 196 = 2.0%）→ 放行（判定是严格小于）。"""
    _seed_partition(lake, 200)
    lake.save_processed(_bars(_dates(196)))
    assert _disk_rows(lake) == 196


def test_shrink_one_row_past_boundary_blocked(lake):
    """比阈值再少一行（200 → 195 = 2.5%）→ 拦下。"""
    _seed_partition(lake, 200)
    with pytest.raises(PartitionShrinkError):
        lake.save_processed(_bars(_dates(195)))
    assert _disk_rows(lake) == 200


# ---- 6) 边界：空分区不误判 -----------------------------------------------

def test_empty_existing_partition_not_blocked(lake):
    """``n_before == 0`` 的空分区：不除零、不误判，直接放行。"""
    path = lake._path("processed", SYM, FREQ, YEAR)
    write_parquet(_bars(_dates(0)).df.reset_index(), path)  # 0 行分区
    lake.save_processed(_bars(_dates(5)))  # 不抛
    assert _disk_rows(lake) == 5


# ---- 7) 门禁按品种独立 ---------------------------------------------------

def test_other_symbol_partition_does_not_block(lake):
    """同年度不同品种的分区互不干扰（门禁按 symbol 分区判定）。"""
    _seed_partition(lake, 261)  # ag0/2024
    lake.save_processed(_bars(_dates(5), symbol="au0"))  # 不抛
    assert _disk_rows(lake) == 261
    assert _disk_rows(lake, symbol="au0") == 5


# ---- 8) 常量与日志 -------------------------------------------------------

def test_default_tolerance_is_2pct():
    assert DEFAULT_SHRINK_TOLERANCE == 0.02


def test_write_logs_row_counts(lake, caplog):
    """每次写入落一行 info，含 n_before / n_after / allow_shrink。"""
    import logging

    _seed_partition(lake, 243)
    with caplog.at_level(logging.INFO, logger="hexbroker.data.store"):
        lake.save_processed(_bars(_dates(243)), allow_shrink=True)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    hit = [m for m in msgs if "n_before=243" in m and "n_after=243" in m]
    assert hit, f"未找到缩水门禁日志，实际：{msgs}"
    assert "allow_shrink=True" in hit[0]


def test_first_write_logs_none_before(lake, caplog):
    """首次写入时 n_before 为 None（日志如实反映"无前值"）。"""
    import logging

    with caplog.at_level(logging.INFO, logger="hexbroker.data.store"):
        lake.save_processed(_bars(_dates(3)))
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("n_before=None" in m and "n_after=3" in m for m in msgs)
