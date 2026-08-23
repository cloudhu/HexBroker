"""组合层（§3.5）：记录权益曲线与回撤。"""

from __future__ import annotations

from typing import Any

import pandas as pd


class Portfolio:
    """逐 bar 记录账户权益，提供权益曲线与回撤序列。"""

    def __init__(self, initial_capital: float = 1_000_000.0) -> None:
        self.initial_capital = float(initial_capital)
        self._timestamps: list = []
        self._equity: list[float] = []

    def record(self, timestamp: Any, equity: float) -> None:
        self._timestamps.append(timestamp)
        self._equity.append(float(equity))

    @property
    def equity_curve(self) -> pd.Series:
        return pd.Series(self._equity, index=pd.to_datetime(self._timestamps), name="equity")

    @property
    def drawdown_series(self) -> pd.Series:
        eq = self.equity_curve
        peak = eq.cummax()
        return (eq / peak - 1.0).clip(upper=0.0)

    @property
    def max_drawdown(self) -> float:
        dd = self.drawdown_series
        return float(dd.min()) if len(dd) else 0.0

    @property
    def final_equity(self) -> float:
        return self._equity[-1] if self._equity else self.initial_capital
