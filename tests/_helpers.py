"""测试共享辅助：快速构造「信号+价格」（RL/一致性/进化测试用，避免重复训练）。"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def make_prices(n_bars: int = 300, seed: int = 7, symbols: Optional[list[str]] = None) -> pd.DataFrame:
    from hexbroker.data.sources.synthetic_source import SyntheticSource

    syms = symbols or ["SHFE.cu"]
    bars = SyntheticSource(n_bars=n_bars, seed=seed).fetch_bars(syms, freq="1d")
    return bars.df[["open", "high", "low", "close", "volume", "amount", "open_interest"]]


def make_signals(prices: pd.DataFrame, seed: int = 7, strength: float = 0.15) -> pd.DataFrame:
    """在价格索引上合成有方向性的 p_up 信号（无真实训练，速度快）。"""
    rows = []
    rng = np.random.default_rng(seed)
    for sym in prices.index.get_level_values(0).unique():
        sub = prices.xs(sym, level=0).sort_index()
        ts = sub.index
        n = len(ts)
        phase = np.linspace(0, 6 * np.pi, n)
        trend = np.sin(phase) * strength + 0.02 * np.sin(phase / 3)
        p_up = np.clip(0.5 + trend + rng.normal(0, 0.06, n), 0.05, 0.95)
        vol = np.clip(0.005 + 0.01 * np.abs(trend) + rng.normal(0, 0.002, n), 1e-3, 0.1)
        conf = np.clip(1.0 - vol * 20, 0.0, 1.0)
        for i in range(n):
            rows.append(
                {
                    "symbol": sym,
                    "ts": ts[i],
                    "p_up": float(p_up[i]),
                    "exp_ret": float(trend[i] * 0.1),
                    "vol_hat": float(vol[i]),
                    "conf": float(conf[i]),
                    "is_effective": bool(abs(p_up[i] - 0.5) > 0.05),
                    "model_id": "test-model",
                    "train_end": ts[i],
                    "horizon": 5,
                }
            )
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index(["symbol", "ts"]).sort_index()
    df.index = df.index.set_names(["symbol", "datetime"])
    return df


def fast_cfg(n_bars: int = 300):
    """快速测试配置（小样本、少 epoch、单品种）。"""
    from hexbroker.config import load_config

    cfg = load_config()
    cfg.data.symbols = ["SHFE.cu"]
    cfg.data.source = "synthetic"
    cfg.data.train_len = 150
    cfg.data.test_len = 50
    cfg.data.purge = 5
    cfg.data.embargo = 2
    cfg.data.mode = "rolling"
    cfg.forecast.horizon = 3
    cfg.forecast.epochs = 3
    cfg.forecast.hidden_size = 16
    cfg.forecast.n_layers = 1
    cfg.forecast.n_heads = 2
    cfg.rl.total_timesteps = 1500
    cfg.rl.hidden_size = 16
    cfg.rl.n_hidden = 1
    cfg.evolution.n_trials = 3
    cfg.evolution.examm_n_generations = 3
    cfg.evolution.examm_n_islands = 2
    return cfg
