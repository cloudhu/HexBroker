"""P0-3 DataLake manifest 测试（PRD A3.1/A3.5）。

覆盖：内容指纹确定性/敏感性、manifest 写读回环、build_manifest 字段、
存量回填、save_processed 自动写 manifest。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _helpers import make_prices

from hexbroker.config import load_config
from hexbroker.data.manifest import (
    DataManifest,
    backfill_manifests,
    build_manifest,
    content_fingerprint,
    read_manifest,
    write_manifest,
)
from hexbroker.data.schema import as_barframe
from hexbroker.data.store import DataLake
from hexbroker.utils.io import write_parquet


def _mindex_df(n=4, symbol="X"):
    idx = pd.MultiIndex.from_product(
        [[symbol], pd.to_datetime([f"2020-01-0{i+1}" for i in range(n)])],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0], "volume": [10, 20, 30, 40]}, index=idx)


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------
def test_content_fingerprint_deterministic():
    df = _mindex_df()
    assert content_fingerprint(df) == content_fingerprint(df.copy())


def test_content_fingerprint_column_order_insensitive():
    df1 = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})
    df2 = pd.DataFrame({"b": [3.0, 4.0], "a": [1.0, 2.0]})
    assert content_fingerprint(df1) == content_fingerprint(df2)


def test_content_fingerprint_sensitive_to_content():
    df1 = pd.DataFrame({"a": [1.0, 2.0]})
    df2 = pd.DataFrame({"a": [1.0, 3.0]})
    assert content_fingerprint(df1) != content_fingerprint(df2)


def test_content_fingerprint_length():
    assert len(content_fingerprint(_mindex_df())) == 12


# ---------------------------------------------------------------------------
# manifest 写读
# ---------------------------------------------------------------------------
def test_write_read_roundtrip(tmp_path):
    m = DataManifest(layer="processed", symbol="SHFE.cu", freq="1d",
                     data_version="v1", fetched_at="2026-01-01T00:00:00Z", source="lake")
    p = write_manifest(m, tmp_path)
    assert p.exists()
    assert p.name == "manifest.json"
    assert p.parent == tmp_path / "processed" / "SHFE.cu" / "1d"
    m2 = read_manifest(tmp_path, "processed", "SHFE.cu", "1d")
    assert m2 is not None
    assert m2.layer == "processed" and m2.symbol == "SHFE.cu"
    assert m2.data_version == "v1" and m2.source == "lake"


def test_read_manifest_missing_returns_none(tmp_path):
    assert read_manifest(tmp_path, "raw", "X", "1d") is None


def test_build_manifest_fields():
    df = _mindex_df()
    m = build_manifest("processed", "X", "1d", df, source="test", constants={"adjust_method": "backward"})
    assert m.n_rows == 4
    assert m.content_fingerprint
    assert len(m.content_fingerprint) == 12
    assert m.date_range[0] == "2020-01-01"
    assert m.date_range[1] == "2020-01-04"
    assert m.constants == {"adjust_method": "backward"}


# ---------------------------------------------------------------------------
# 存量回填
# ---------------------------------------------------------------------------
def test_backfill_manifests(tmp_path):
    df = _mindex_df()
    write_parquet(df.reset_index(), tmp_path / "processed" / "X" / "1d" / "2020.parquet")
    n = backfill_manifests(tmp_path, load_config())
    assert n >= 1
    m = read_manifest(tmp_path, "processed", "X", "1d")
    assert m is not None
    assert m.content_fingerprint == content_fingerprint(df)
    assert m.constants.get("adjust_method") == "backward"


def test_backfill_manifests_flat_layout(tmp_path):
    """无年份分区布局：{layer}/{symbol}/{freq}.parquet。"""
    df = pd.DataFrame({"close": [1.0, 2.0]})
    write_parquet(df, tmp_path / "raw" / "X" / "1d.parquet")
    n = backfill_manifests(tmp_path, load_config())
    assert n >= 1
    assert read_manifest(tmp_path, "raw", "X", "1d") is not None


def test_backfill_skip_existing_preserves_production_manifest(tmp_path):
    """P1-b：skip_existing=True 跳过已有 manifest 的分区，生产自动化维护的
    manifest（data_version=v1）不被改写；仅补全缺失分区。"""
    cfg = load_config()
    # 分区 A：模拟盘中自动化已写 v1 manifest
    dir_a = tmp_path / "processed" / "A" / "1d"
    dir_a.mkdir(parents=True)
    write_parquet(_mindex_df().reset_index(), dir_a / "2026.parquet")
    write_manifest(
        build_manifest("processed", "A", "1d", _mindex_df(),
                       source="lake", data_version="v1"),
        tmp_path,
    )
    before = (dir_a / "manifest.json").read_text(encoding="utf-8")

    # 分区 B：从未生成 manifest
    dir_b = tmp_path / "processed" / "B" / "1d"
    dir_b.mkdir(parents=True)
    write_parquet(_mindex_df().reset_index(), dir_b / "2026.parquet")

    n = backfill_manifests(tmp_path, cfg, skip_existing=True)
    assert n == 1  # 仅 B 被回填
    # A 的 manifest 逐字节未动（仍为 v1，非 backfill-*）
    assert (dir_a / "manifest.json").read_text(encoding="utf-8") == before
    assert read_manifest(tmp_path, "processed", "A", "1d").data_version == "v1"
    # B 已补全且标记 backfill-*
    assert read_manifest(tmp_path, "processed", "B", "1d").data_version.startswith("backfill-")

    # 再次增量运行：幂等，零回填
    assert backfill_manifests(tmp_path, cfg, skip_existing=True) == 0


def test_backfill_without_skip_existing_overwrites(tmp_path):
    """默认 skip_existing=False 保持既有行为：已有 manifest 也会重写。"""
    cfg = load_config()
    dir_a = tmp_path / "processed" / "A" / "1d"
    dir_a.mkdir(parents=True)
    write_parquet(_mindex_df().reset_index(), dir_a / "2026.parquet")
    write_manifest(
        build_manifest("processed", "A", "1d", _mindex_df(),
                       source="lake", data_version="v1"),
        tmp_path,
    )
    backfill_manifests(tmp_path, cfg)
    assert read_manifest(tmp_path, "processed", "A", "1d").data_version.startswith("backfill-")


# ---------------------------------------------------------------------------
# DataLake.save_processed 自动写 manifest
# ---------------------------------------------------------------------------
def test_save_processed_writes_manifest(tmp_path):
    prices = make_prices(n_bars=30, seed=1)
    prices = prices.assign(adj_close=prices["close"], raw_close=prices["close"])
    bf = as_barframe(prices, freq="1d")

    lake = DataLake(root=str(tmp_path / "lake"), constants={"adjust_method": "backward"})
    lake.save_processed(bf)

    m = read_manifest(tmp_path / "lake", "processed", "SHFE.cu", "1d")
    assert m is not None
    assert m.n_rows == 30
    assert m.constants.get("adjust_method") == "backward"
    # Parquet 本体仍可正常读取（schema 未变）
    bf2 = lake.load_processed("SHFE.cu", "1d")
    assert len(bf2.df) == 30


def test_save_processed_load_unchanged_without_constants(tmp_path):
    """默认 constants={} 时 DataLake 行为与改动前一致。"""
    prices = make_prices(n_bars=20, seed=2)
    prices = prices.assign(adj_close=prices["close"], raw_close=prices["close"])
    bf = as_barframe(prices, freq="1d")
    lake = DataLake(root=str(tmp_path / "lake"))
    lake.save_processed(bf)
    bf2 = lake.load_processed("SHFE.cu", "1d")
    assert len(bf2.df) == 20
