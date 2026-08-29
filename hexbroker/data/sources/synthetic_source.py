"""合成数据源（供 ``--demo`` 端到端 smoke，无需外部文件）。

生成具有可学习信号的确定性合成期货日线：
- 价格由几何随机游走 + 可学习方向的局部漂移（regime）驱动；
- 注入若干「换月跳空」用于检验 ``ContractStitcher`` 后向复权；
- 固定种子保证可复现（§8.2）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ... import HexEmptyDataError
from ...constants import LIMIT_DOWN, LIMIT_UP
from ..base import DataSource
from ..schema import BarFrame


class SyntheticSource(DataSource):
    """确定性合成数据源，用于离线 smoke 与单测。"""

    name = "synthetic"

    #: 离线确定性源：不做新鲜度判定，仅做空结果门禁
    check_freshness = False

    def __init__(
        self,
        n_bars: int = 800,
        seed: int = 42,
        vol: float = 0.012,
        drift: float = 0.0002,
        rollover_every: int = 240,
        regime_strength: float = 0.004,
    ) -> None:
        self.n_bars = n_bars
        self.seed = seed
        self.vol = vol
        self.drift = drift
        self.rollover_every = rollover_every
        self.regime_strength = regime_strength

    def fetch_bars(
        self,
        symbols: list[str],
        start: str = "2018-01-01",
        end: str = "2024-12-31",
        freq: str = "1d",
    ) -> BarFrame:
        frames = []
        for i, sym in enumerate(symbols):
            rng = np.random.default_rng(self.seed + i * 1000 + 7)
            prices = self._gen_prices(rng)
            df = self._to_frame(prices, sym, rng)
            frames.append(df)
        if not frames:
            raise HexEmptyDataError(
                f"Synthetic 源未生成任何品种数据（请求 {symbols}）", source="synthetic"
            )
        out = pd.concat(frames)
        # 收口：裁剪 + 空结果门禁 + 契约校验。
        # 合成源是离线确定性源，默认关闭新鲜度判定（见类属性 check_freshness）。
        return self._finalize(out, start, end, freq, symbols=symbols, source="synthetic")

    def _gen_prices(self, rng: np.random.Generator) -> np.ndarray:
        """几何随机游走 + 分段 regime（可学习方向） + 换月跳空。"""
        n = self.n_bars
        returns = rng.normal(self.drift, self.vol, size=n)
        # 分段 regime：每 ~120 根切换一次方向（可学习）
        regime = np.zeros(n)
        seg = 120
        sign = 1.0
        for s in range(0, n, seg):
            sign = -sign if rng.random() > 0.4 else sign
            regime[s : s + seg] = sign * self.regime_strength
        returns = returns + regime
        # 换月跳空：在 rollover 边界处叠加一次性跳空（复权需消除）
        for r in range(self.rollover_every, n, self.rollover_every):
            returns[r] += rng.choice([-1, 1]) * self.vol * 6.0
        close = 1000.0 * np.exp(np.cumsum(returns))
        return close

    def _to_frame(self, close: np.ndarray, sym: str, rng: np.random.Generator) -> pd.DataFrame:
        n = len(close)
        dates = pd.date_range("2018-01-01", periods=n, freq="D")
        # OHLC 围绕 close 构造
        intraday = rng.normal(0, self.vol * 0.6, size=n)
        open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + intraday * 0.3)
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, self.vol * 0.5, n)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, self.vol * 0.5, n)))
        volume = rng.integers(5000, 50000, size=n).astype(float)
        amount = volume * close
        oi = rng.integers(10000, 100000, size=n).astype(float)

        df = pd.DataFrame(
            {
                "symbol": sym,
                "datetime": dates,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "open_interest": oi,
            }
        )
        df["raw_close"] = df["close"]
        df["adj_close"] = df["close"]
        # 换月标记
        df[LIMIT_UP] = False
        df[LIMIT_DOWN] = False
        is_roll = np.zeros(n, dtype=bool)
        for r in range(self.rollover_every, n, self.rollover_every):
            is_roll[r] = True
        df["is_rollover"] = is_roll
        return df.set_index(["symbol", "datetime"])
