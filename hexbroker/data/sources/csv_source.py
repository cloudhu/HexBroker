"""CSV 离线数据源（默认离线开发源，§T01）。

读取 ``data/sample/{symbol}.csv``（或配置指定的本地目录）。CSV 至少包含
``date, open, high, low, close, volume, amount, open_interest`` 列。
缺失值策略：前向填充 ≤1 根（契约 §8.4），超限时 ``validate_bars`` 报错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame, validate_bars

DEFAULT_SAMPLE_DIR = Path("data/sample")


class CsvSource(DataSource):
    """本地 CSV 数据源。"""

    name = "csv"

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = Path(root) if root else DEFAULT_SAMPLE_DIR

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        frames = []
        for sym in symbols:
            path = self.root / f"{sym}.csv"
            if not path.exists():
                raise HexDataError(f"CSV 源缺少文件: {path}")
            raw = pd.read_csv(path, parse_dates=["date"])
            raw = raw.sort_values("date").reset_index(drop=True)
            df = self._build_frame(raw, sym, freq)
            frames.append(df)
        out = pd.concat(frames)
        out = self._clip_range(out, start, end)
        return BarFrame(df=out, freq=freq, source="csv").validate()

    @staticmethod
    def _build_frame(raw: pd.DataFrame, symbol: str, freq: str) -> pd.DataFrame:
        required = ["date", "open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in raw.columns]
        if missing:
            raise HexDataError(f"CSV 缺少列: {missing}")
        df = raw.copy()
        df["symbol"] = symbol
        df = df.rename(columns={"date": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        for col in ["amount", "open_interest"]:
            if col not in df.columns:
                df[col] = 0.0
        # 未复权 = 原始；样例即主力连续，adj_close 初值等同 close
        df["raw_close"] = df["close"]
        df["adj_close"] = df["close"]
        df["limit_up"] = False
        df["limit_down"] = False
        df["is_rollover"] = False
        df = df[
            [
                "symbol",
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "open_interest",
                "adj_close",
                "raw_close",
                "limit_up",
                "limit_down",
                "is_rollover",
            ]
        ]
        df = df.set_index(["symbol", "datetime"])
        return df

    def health_check(self) -> bool:
        return self.root.exists()
