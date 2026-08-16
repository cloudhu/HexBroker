"""统计辅助（§3.6 / §8.5）：过拟合诊断（DSR / PBO）。"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from .metrics import MetricsReport, compute_metrics


def aggregate_metrics(reports: Sequence[MetricsReport]) -> dict:
    """聚合多折/多标的指标，返回均值与标准差。"""
    if not reports:
        return {}
    keys = ["total_return", "annual_return", "sharpe", "sortino", "max_drawdown", "calmar", "win_rate"]
    out = {}
    for k in keys:
        vals = np.array([getattr(r, k) for r in reports])
        out[k] = float(vals.mean())
        out[f"{k}_std"] = float(vals.std())
    return out


def deflated_sharpe_ratio(sharpe: float, n_obs: int, n_strategies: int = 1, skew: float = 0.0, kurt: float = 3.0) -> float:
    """简化版 DSR（Deflated Sharpe Ratio）概率估计。

    返回近似的「策略优于随机」概率（0~1）。越高越不易过拟合。
    """
    if n_obs <= 1:
        return 0.0
    # 经验性调整：用策略数量做惩罚
    adj = np.sqrt(np.log(n_strategies)) if n_strategies > 1 else 0.0
    z = (sharpe - adj) / np.sqrt(1.0 / n_obs)
    return float(1.0 / (1.0 + np.exp(-z)))


def probability_of_backtest_overfitting(sharpe_train: Sequence[float], sharpe_test: Sequence[float]) -> float:
    """PBO：测试集 Sharpe 低于训练集 Sharpe 的比例（越高越疑似过拟合）。"""
    if len(sharpe_train) == 0:
        return 0.0
    return float(np.mean(np.array(sharpe_test) < np.array(sharpe_train)))


def metrics_from_equity(equity: pd.Series, freq: str = "1d") -> MetricsReport:
    return compute_metrics(equity, freq=freq)
