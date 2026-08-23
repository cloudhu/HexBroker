"""AkShare 免费日线备份源（可选；接口不稳，仅备份）。

依赖缺失时优雅降级。
"""

from __future__ import annotations


import pandas as pd

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame


class AkshareSource(DataSource):
    """AkShare 免费期货日线源。"""

    name = "akshare"

    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
    ) -> BarFrame:
        try:
            import akshare as ak  # type: ignore
        except ImportError as e:
            raise HexConfigError(
                "未安装 akshare，无法使用 AkShare 数据源。请 pip install akshare，或使用 --source csv。"
            ) from e
        frames = []
        for sym in symbols:
            # sym 形如 SHFE.cu → 商品代码 cu
            code = sym.split(".")[-1]
            df = ak.futures_main_sina(symbol=code) if hasattr(ak, "futures_main_sina") else None
            if df is None:
                raise HexDataError(f"AkShare 未取得 {sym} 数据（接口可能变更）")
            df = df.rename(columns={"date": "datetime", "open": "open", "high": "high",
                                    "low": "low", "close": "close", "volume": "volume"})
            df["symbol"] = sym
            df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
            df["amount"] = 0.0
            df["open_interest"] = 0.0
            df["raw_close"] = df["close"]
            df["adj_close"] = df["close"]
            df["limit_up"] = False
            df["limit_down"] = False
            df["is_rollover"] = False
            df = df.set_index(["symbol", "datetime"])
            frames.append(df)
        out = pd.concat(frames)
        out = self._clip_range(out, start, end)
        return BarFrame(df=out, freq=freq, source="akshare").validate()

    def health_check(self) -> bool:
        try:
            import akshare  # noqa: F401

            return True
        except ImportError:
            return False
