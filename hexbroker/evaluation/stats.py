"""统计辅助（§3.6 / §8.5）：过拟合诊断（DSR / PBO）。"""

from __future__ import annotations

import math
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
    """Deflated Sharpe Ratio（Bailey & López de Prado, 2015）概率估计。

    返回策略 Sharpe **优于**「n_strategies 个随机策略中最优者」的概率（0~1）：
    越高越不易过拟合。修正旧实现的缺陷——旧版用 ``sqrt(1/n_obs)`` 作标准误、
    忽略 skew/kurt，导致大样本下恒≈1、闸门 ``dsr>0`` 永真。

    标准误纳入偏度/峰度修正：``Var(SR) = (1 - γ3·SR + (γ4-1)/4·SR²) / n``，
    并以正态 CDF（``math.erf``，无第三方依赖）给出概率。
    """
    if n_obs <= 1:
        return 0.0
    skew = 0.0 if skew is None else float(skew)
    kurt = 3.0 if kurt is None else float(kurt)
    var_sr = (1.0 - skew * float(sharpe) + (kurt - 1.0) / 4.0 * float(sharpe) ** 2) / n_obs
    if var_sr <= 0:
        return 0.0
    se = math.sqrt(var_sr)
    # n_strategies 个随机策略中最优 Sharpe 的期望（近似）：sqrt((1-γ)·2·ln(M))
    gamma_euler = 0.5772156649015329
    expected_max = math.sqrt((1.0 - gamma_euler) * 2.0 * math.log(max(n_strategies, 1)))
    z = (float(sharpe) - expected_max) / se
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def probability_of_backtest_overfitting(sharpe_train: Sequence[float], sharpe_test: Sequence[float]) -> float:
    """简化过拟合比值：测试集 Sharpe 低于训练集 Sharpe 的折数比例（越高越疑似过拟合）。

    注意：这是**逐折简单比值**，并非完整 CSCV（Bailey & López de Prado 的
    Combinatorial Symmetric Cross-Validation）。正式的 CSCV PBO 已在
    ``hexbroker.pipeline._pbo`` 中实现并用于闸门 2 诊断。
    """
    if len(sharpe_train) == 0:
        return 0.0
    return float(np.mean(np.array(sharpe_test) < np.array(sharpe_train)))


def metrics_from_equity(equity: pd.Series, freq: str = "1d") -> MetricsReport:
    return compute_metrics(equity, freq=freq)
