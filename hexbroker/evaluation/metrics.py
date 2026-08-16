"""绩效指标（§3.6 / §8.5）。

由权益曲线计算年化收益、Sharpe、Sortino、最大回撤、Calmar、胜率、换手率等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# 不同频率的年度化因子
_ANNUALIZE = {"1d": 252, "60m": 252 * 4, "30m": 252 * 8, "5m": 252 * 48, "1m": 252 * 240}


@dataclass
class MetricsReport:
    """绩效指标报告。"""

    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    volatility: float = 0.0
    win_rate: float = 0.0
    turnover: float = 0.0
    final_equity: float = 0.0
    n_bars: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "calmar": self.calmar,
            "volatility": self.volatility,
            "win_rate": self.win_rate,
            "turnover": self.turnover,
            "final_equity": self.final_equity,
            "n_bars": self.n_bars,
        }


def compute_metrics(
    equity: pd.Series,
    positions: Optional[pd.DataFrame] = None,
    freq: str = "1d",
) -> MetricsReport:
    """由权益曲线（datetime 索引）计算指标。"""
    eq = equity.astype(float)
    if len(eq) < 2:
        return MetricsReport(final_equity=float(eq.iloc[-1]) if len(eq) else 0.0)
    ann = _ANNUALIZE.get(freq, 252)
    rets = eq.pct_change().dropna()
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    n = len(rets)
    mean_r = rets.mean()
    std_r = rets.std(ddof=1) if n > 1 else 0.0
    # 权益归零/亏损超过本金时，(1+total_return)<=0，分数次幂会得复数 → 钳制
    base = 1.0 + total_return
    if base > 0:
        annual_return = float(base ** (ann / max(n, 1)) - 1.0)
    elif total_return < 0:
        annual_return = -1.0
    else:
        annual_return = 0.0
    sharpe = float(mean_r / std_r * np.sqrt(ann)) if std_r > 1e-12 else 0.0
    downside = rets[rets < 0]
    dsd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = float(mean_r / dsd * np.sqrt(ann)) if dsd > 1e-12 else 0.0
    peak = eq.cummax()
    dd = (eq / peak - 1.0).clip(upper=0.0)
    max_dd = float(dd.min())
    calmar = float(annual_return / abs(max_dd)) if abs(max_dd) > 1e-9 else 0.0
    vol = float(std_r * np.sqrt(ann))
    win_rate = float((rets > 0).mean()) if n > 0 else 0.0
    turnover = 0.0
    if positions is not None and len(positions) > 1:
        turnover = float(positions.diff().abs().sum().sum())
    return MetricsReport(
        total_return=total_return,
        annual_return=annual_return,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        calmar=calmar,
        volatility=vol,
        win_rate=win_rate,
        turnover=turnover,
        final_equity=float(eq.iloc[-1]),
        n_bars=int(n),
    )
