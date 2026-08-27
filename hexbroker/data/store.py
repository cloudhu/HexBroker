"""数据湖（§2.2 L1）：``data/raw|interim|processed`` 分层 Parquet，
按 ``symbol/freq/year`` 分区。底层 IO 见 ``hexbroker.utils.io``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from ..utils.io import read_parquet, write_parquet
from .schema import BarFrame


class DataLake:
    """分层本地数据湖。"""

    def __init__(self, root: Optional[str | Path] = None,
                 constants: Optional[dict] = None) -> None:
        self.root = Path(root) if root else Path("data")
        # P0-3：写 manifest 时携带的口径常量（adjust_method/main_rule/RAW_SCALE_FIX 等）
        self.constants = dict(constants or {})
        for layer in ("raw", "interim", "processed"):
            (self.root / layer).mkdir(parents=True, exist_ok=True)

    def _path(self, layer: str, symbol: str, freq: str, year: Optional[int] = None) -> Path:
        if year is None:
            return self.root / layer / symbol / f"{freq}.parquet"
        return self.root / layer / symbol / freq / f"{year}.parquet"

    def save_processed(self, bars: BarFrame, symbol: Optional[str] = None) -> None:
        """保存已处理 BarFrame（按 symbol 拆分分区），并自动写/更新 manifest（P0-3）。

        manifest 为 sidecar JSON（``processed/{symbol}/{freq}/manifest.json``），
        不改 Parquet schema；仅当有新数据写入时更新。
        """
        from .manifest import build_manifest, write_manifest

        for sym in bars.symbols:
            df = bars.by_symbol(sym)
            years = df.index.get_level_values("datetime").year.unique()
            for y in years:
                sub = df[df.index.get_level_values("datetime").year == y]
                write_parquet(sub.reset_index(), self._path("processed", sym, bars.freq, int(y)))
            write_manifest(
                build_manifest("processed", sym, bars.freq, df,
                               source="lake", data_version="v1", constants=self.constants),
                self.root,
            )

    def load_processed(self, symbol: str, freq: str) -> BarFrame:
        """读取某品种已处理数据，拼回 MultiIndex。"""
        base = self.root / "processed" / symbol / freq
        files = sorted(base.glob("*.parquet")) if base.exists() else []
        if not files:
            raise FileNotFoundError(f"数据湖中无 {symbol}/{freq} 的处理数据")
        parts = [read_parquet(f) for f in files]
        df = pd.concat(parts)
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return BarFrame(df=df, freq=freq, source="lake")

    def exists(self, symbol: str, freq: str) -> bool:
        base = self.root / "processed" / symbol / freq
        return base.exists() and any(base.glob("*.parquet"))
