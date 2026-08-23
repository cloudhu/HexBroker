"""基线策略（§3.6 / §8.5）：买入持有、双均线、MACD、信号阈值。

每个基线产出目标仓位（合约数，带符号）DataFrame，供回测引擎统一评估与对比。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..forecast.threshold import SignalThreshold


def _target_df(symbol: str, index: pd.Index, targets: np.ndarray) -> pd.DataFrame:
    df = pd.DataFrame({"target": targets.astype(float)}, index=index)
    df.index = pd.MultiIndex.from_arrays([np.repeat(symbol, len(index)), index], names=["symbol", "datetime"])
    return df


def buy_hold(prices: pd.DataFrame, contract_scale: float = 1.0) -> pd.DataFrame:
    """买入持有：始终满仓多头。"""
    out = []
    for sym in prices.index.get_level_values(0).unique():
        sub = prices.xs(sym, level=0)["close"].sort_index()
        out.append(_target_df(sym, sub.index, np.full(len(sub), contract_scale)))
    return pd.concat(out).sort_index()


def dual_ma(prices: pd.DataFrame, fast: int = 5, slow: int = 20, contract_scale: float = 1.0) -> pd.DataFrame:
    """双均线交叉：快线上穿慢线做多，下穿做空。"""
    out = []
    for sym in prices.index.get_level_values(0).unique():
        sub = prices.xs(sym, level=0)["close"].sort_index()
        ma_f = sub.rolling(fast, min_periods=fast).mean()
        ma_s = sub.rolling(slow, min_periods=slow).mean()
        tgt = np.where(ma_f > ma_s, contract_scale, -contract_scale)
        tgt = np.where(ma_s.isna().values, 0.0, tgt)
        out.append(_target_df(sym, sub.index, tgt))
    return pd.concat(out).sort_index()


def macd(prices: pd.DataFrame, contract_scale: float = 1.0) -> pd.DataFrame:
    """MACD：DIF 上穿 DEA 做多，下穿做空。"""
    out = []
    for sym in prices.index.get_level_values(0).unique():
        sub = prices.xs(sym, level=0)["close"].sort_index()
        ema12 = sub.ewm(span=12, adjust=False).mean()
        ema26 = sub.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        tgt = np.where(dif > dea, contract_scale, -contract_scale)
        tgt = np.where(dif.isna().values, 0.0, tgt)
        out.append(_target_df(sym, sub.index, tgt))
    return pd.concat(out).sort_index()


def signal_threshold(
    signals: pd.DataFrame, long_thr: float = 0.55, short_thr: float = 0.45, contract_scale: float = 1.0
) -> pd.DataFrame:
    """信号阈值：依据 p_up 给出 ±1 方向（需 SignalStore 风格的 MultiIndex 信号帧）。"""
    thr = SignalThreshold(long_thr=long_thr, short_thr=short_thr)
    out = []
    for sym in signals.index.get_level_values(0).unique():
        sub = signals.xs(sym, level=0).sort_index()
        p_up = sub["p_up"].values
        dirs = thr.direction.__class__  # noqa
        tgt = np.where(p_up >= long_thr, contract_scale, np.where(p_up <= short_thr, -contract_scale, 0.0))
        out.append(_target_df(sym, sub.index, tgt))
    return pd.concat(out).sort_index()


_BASELINES = {
    "buy_hold": buy_hold,
    "dual_ma": dual_ma,
    "macd": macd,
    "signal_threshold": signal_threshold,
}


class BaselineStrategy:
    """基线策略注册封装。"""

    def __init__(self, name: str) -> None:
        if name not in _BASELINES:
            raise ValueError(f"未知基线策略：{name}（可选 {list(_BASELINES)}）")
        self.name = name

    def generate(self, prices: pd.DataFrame, signals: Optional[pd.DataFrame] = None, **kwargs) -> pd.DataFrame:
        if self.name == "signal_threshold":
            if signals is None:
                raise ValueError("signal_threshold 需要 signals 参数")
            return signal_threshold(signals, **kwargs)
        return _BASELINES[self.name](prices, **kwargs)


def run_baselines(prices: pd.DataFrame, signals: Optional[pd.DataFrame] = None) -> dict[str, pd.DataFrame]:
    """运行全部 4 个基线，返回 {name: targets_df}。"""
    result = {}
    for name in _BASELINES:
        if name == "signal_threshold" and signals is None:
            continue
        result[name] = BaselineStrategy(name).generate(prices, signals)
    return result
