"""QA：数据源对比脚本核心逻辑测试（任务 #4）。

覆盖 compare_data_sources 的：
- load_baseline 窗口过滤（基准按 --start/--end 裁剪）
- compare_one 判定逻辑（对齐率/缺失/误差、共同交易日过少兜底）
"""

from __future__ import annotations

import pandas as pd
import pytest

import scripts.compare_data_sources as cds


@pytest.fixture()
def sample_base(tmp_path) -> pd.DataFrame:
    """构造 12 根基准 bar（compare_one 要求共同交易日 >=10）。"""
    n = 12
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    vals = list(range(1, n + 1))  # 与 idx 长度一致，避免 Series 索引错位
    return pd.DataFrame(
        {
            "open": vals,
            "high": [v + 0.1 for v in vals],
            "low": [v - 0.1 for v in vals],
            "close": vals,
            "volume": [v * 100.0 for v in vals],
        },
        index=idx,
    )


def test_load_baseline_window_clip(tmp_path):
    """load_baseline 应按窗口裁剪（回归：2024 窗口曾因未裁剪误报 1460 缺失）。"""
    # 写入一个分片 parquet
    df = pd.DataFrame(
        {
            "symbol": ["au0"] * 3,
            "datetime": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-06-03"]),
            "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0], "volume": [1.0, 1.0, 1.0],
        }
    )
    d = tmp_path / "au0" / "1d"
    d.mkdir(parents=True)
    df.to_parquet(d / "2024.parquet")

    # monkeypatch PROCESSED_DIR 指向临时目录
    cds.PROCESSED_DIR = tmp_path
    try:
        out = cds.load_baseline("au0", "2024-01-01", "2024-03-01")
        assert len(out) == 2  # 只保留 1-2 月两根
        assert list(out.index) == [
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
        ]
    finally:
        cds.PROCESSED_DIR = __import__("pathlib").Path("data") / "raw" / "processed"


def test_compare_one_perfect_alignment(sample_base):
    """完全对齐：close 对齐 100%、缺失 0、vol_corr 1.0、判定应通过。"""
    src = sample_base.copy()
    r = cds.compare_one("au0", sample_base, src, "test-src")
    assert r["error"] == ""
    assert r["n_common"] == 12 and r["n_missing"] == 0
    assert r["close_aligned_pct"] == 1.0
    assert r["close_rel_err_max"] == 0.0
    assert r["volume_corr"] == pytest.approx(1.0)


def test_compare_one_tiny_drift_and_missing(sample_base):
    """微小漂移 + 缺 1 根：对齐率 <100%，缺失数正确。"""
    src = sample_base.copy()
    # close 全部 +0.5% 漂移
    src["close"] = src["close"] * 1.005
    # 缺最后一天
    src = src.iloc[:11]
    r = cds.compare_one("au0", sample_base, src, "test-src")
    assert r["n_missing"] == 1
    assert r["close_aligned_pct"] == 0.0  # 0.5% > 0.1% 阈值
    assert r["close_rel_err_mean"] == pytest.approx(0.005)


def test_compare_one_few_common_days(sample_base):
    """共同交易日过少 → 明确 error 标记。"""
    src = sample_base.iloc[[0]].copy()  # 只有 1 根
    r = cds.compare_one("au0", sample_base, src, "test-src")
    assert "过少" in r["error"]


def test_compare_one_empty_source(sample_base):
    """源无数据 → 返回 error 标记。"""
    r = cds.compare_one("au0", sample_base, None, "test-src")
    assert r["error"] == "源无数据"
