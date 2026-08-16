"""外盘参照数据加载与时差安全对齐（生产/研究共用）。

外盘收盘（尤其美盘）晚于内盘收盘，t 日内盘只能看到外盘 t-1 收盘，
故必须 ``shift(1)`` 后 asof 对齐到内盘交易日，保证 t 日特征仅依赖外盘 t-1 及以前信息。

数据资产位于 ``data/raw/global/{code}.parquet``（datetime 索引 + close 列）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# 项目根目录（本文件位于 hexbroker/feature/ 下，向上两级）
_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_DATA_DIR = _ROOT / "data" / "raw" / "global"


def load_global_close(code: str) -> pd.Series:
    """加载外盘收盘序列（datetime 索引的 close 列，升序）。"""
    fp = GLOBAL_DATA_DIR / f"{code}.parquet"
    if not fp.exists():
        raise FileNotFoundError(
            f"外盘数据缺失：{fp}。请先运行数据脚本生成 {code}.parquet"
        )
    df = pd.read_parquet(fp)
    if "close" not in df.columns:
        raise ValueError(f"外盘数据 {fp} 缺少 close 列")
    return df["close"].astype(float).sort_index()


def align_global_to_inner(global_close: pd.Series, inner_index: pd.Index) -> pd.Series:
    """时差安全对齐：外盘 shift(1) 后 asof 到内盘交易日。

    对每个内盘交易日 t，取外盘 index <= t 的最近值（已 shift(1)），
    保证特征只用外盘 t-1 及以前收盘。返回与 inner_index 等长的 Series。
    """
    shifted = global_close.shift(1)
    return shifted.reindex(inner_index).ffill()
