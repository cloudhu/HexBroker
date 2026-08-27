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


def fast_execution_cfg(n_bars: int = 300, *, next_bar: bool = False,
                       volume_cap=None, mode: str = "partial"):
    """快速撮合口径测试配置（P0-1：next_bar_execution / volume_cap 开关）。"""
    cfg = fast_cfg(n_bars=n_bars)
    cfg.backtest.next_bar_execution = bool(next_bar)
    cfg.backtest.volume_cap = volume_cap
    cfg.backtest.volume_cap_mode = mode
    return cfg


# ---------------------------------------------------------------------------
# P0-2 缺陷样本夹具生成器（注入已知前视/递归缺陷，供静态分析检出率测试）
# ---------------------------------------------------------------------------
def make_leaky_features() -> list[str]:
    """生成含已知前视缺陷的源码片段列表（每段注入一种危险原语）。

    对应 lookahead 检测的 6 类模式：shift_negative / rolling_center / ewm /
    asof_without_reindex / iloc_forward / np_roll。
    """
    return [
        # 1. shift(-n)：同 bar 使用未来 n 根收益（未来函数）
        'def leaky_momentum(df):\n'
        '    df["f_mom"] = df["close"].shift(-5) / df["close"] - 1.0\n'
        '    return df\n',
        # 2. rolling(center=True)：窗口中心化引入未来
        'def leaky_center_ma(df):\n'
        '    df["f_ma"] = df["close"].rolling(20, center=True).mean()\n'
        '    return df\n',
        # 3. ewm 时序错位（时间衰减基准与信号时点不一致）
        'def leaky_ewm(df):\n'
        '    df["f_ewm"] = df["close"].ewm(span=12, adjust=False).mean()\n'
        '    return df\n',
        # 4. asof 前缺 reindex（跨时区对齐未锁列）
        'def leaky_asof(df, ref):\n'
        '    df["f_global"] = df["ts"].asof(ref["ts"])\n'
        '    return df\n',
        # 5. iloc 直接索引未来行
        'def leaky_iloc(df):\n'
        '    i = len(df) - 1\n'
        '    df["f_next"] = df["close"].iloc[i + 1]\n'
        '    return df\n',
        # 6. np.roll 未来搬移
        'def leaky_roll(df):\n'
        '    import numpy as np\n'
        '    df["f_rolled"] = np.roll(df["close"].to_numpy(), -1)\n'
        '    return df\n',
    ]


def make_recursive_features() -> list[str]:
    """生成含 A→B→A 递归依赖的源码片段（P0-2 recursive 检出夹具）。"""
    return [
        'def compute_a(df):\n'
        '    df["f_a"] = compute_b(df)["f_b"] + 1\n'
        '    return df\n'
        '\n'
        'def compute_b(df):\n'
        '    df["f_b"] = compute_a(df)["f_a"] * 2\n'
        '    return df\n',
    ]

