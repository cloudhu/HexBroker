"""SENTINEL L3：SentinelTradingEnv——组合级 RL 交易环境（2026-08-17）。

设计（编码探索经验）：
- 三品种组合决策（截面分配）；
- 动作 = 连续多头仓位 [0,1]×3（**无做空**——看空反指经验）；
- 状态 = 强度信号（exp_ret z-score×3）+ 市场状态（MA 趋势×3 + 波动率×3）+ 持仓×3；
- 奖励 = 实现收益 − 交易成本 − λ_turn×换手惩罚（w_* 权重可配）。
与 BacktestEngine 共用 CostModel（per-symbol 合约参数）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class SentinelTradingEnv:
    """组合级单边多头 RL 环境（SENTINEL L3）。"""

    metadata: dict = {"render.modes": []}

    def __init__(
        self,
        signals: pd.DataFrame,
        prices: pd.DataFrame,
        cfg: Any,
        ma_window: int = 20,
        vol_window: int = 20,
    ) -> None:
        """signals: 含 symbol/ts/exp_ret（及可选 is_effective）的评估集信号；prices: MultiIndex+close。"""
        self.signals = signals
        self.prices = prices
        self.cfg = cfg
        self.ma_window = ma_window
        self.vol_window = vol_window
        rlc = getattr(cfg, "rl", None)
        btc = getattr(cfg, "backtest", None)

        self.symbols = list(signals["symbol"].unique()) if "symbol" in signals.columns else list(signals.index.get_level_values(0).unique())
        # 奖励权重（奖励工程 2026-08-17：pnl 以 bp 计，w_pnl/w_turn/w_trend 可配/可进化）
        self.w_pnl = float(getattr(rlc, "w_pnl", 1.0))
        self.w_turn = float(getattr(rlc, "w_turn", 0.5))
        self.w_trend = float(getattr(rlc, "w_trend", 2.0))
        self.initial_capital = float(getattr(btc, "initial_capital", 1_000_000.0))
        self.notional_frac = float(getattr(getattr(cfg, "risk", None), "max_position_pct", 0.30))

        # 按 ts 排序的信号（长表 → 每交易日快照）
        df = signals.copy()
        if "symbol" not in df.columns:
            df = df.reset_index()
        self.dates = sorted(df["ts"].unique())
        self.date_index = {d: i for i, d in enumerate(self.dates)}

        # 品种状态序列（趋势/波动，严格因果用全历史 ≤ts）
        self._close_by_sym: dict[str, pd.Series] = {}
        for sym in self.symbols:
            sub = prices.xs(sym, level=0)["close"].astype(float).sort_index()
            self._close_by_sym[sym] = sub

        # 预计算每交易日快照
        self._snapshots: list[dict[str, np.ndarray]] = []
        for d in self.dates:
            day = df[df["ts"] == d].set_index("symbol")
            exp_ret = np.array([float(day.loc[s, "exp_ret"]) if s in day.index else 0.0 for s in self.symbols])
            # 状态：趋势（close>=MA → 1）与波动（20日 std）
            trend = np.array([self._trend(s, d) for s in self.symbols])
            vol = np.array([self._vol(s, d) for s in self.symbols])
            self._snapshots.append({"exp_ret": exp_ret, "trend": trend, "vol": vol})

        # 5 日前向收益（评估奖励用：当前 ts 的 exp_ret 对应未来 5 日）
        self._realized: dict[tuple[str, Any], float] = {}
        for sym in self.symbols:
            closes = self._close_by_sym[sym]
            fwd = closes.shift(-5) / closes - 1.0
            for d in self.dates:
                if d in fwd.index:
                    self._realized[(sym, d)] = float(fwd.loc[d])

        self.obs_dim = 3 * len(self.symbols) + len(self.symbols)  # exp_ret + trend + vol + pos
        # 离散动作：每品种 3 档仓位 {0, 0.5, 1.0} → 组合动作数 3^n
        self._pos_levels = np.array([0.0, 0.5, 1.0])
        self._pos_map = _build_pos_map(len(self.symbols), self._pos_levels)  # (n_actions, n_sym)
        self.n_actions = len(self._pos_map)
        self.action_space = _Discrete(self.n_actions)
        self.observation_space = _Box(-np.inf, np.inf, (self.obs_dim,))

        self._step = 0
        self._pos = np.zeros(len(self.symbols))
        self._cash = self.initial_capital

    # ---- 状态 ----
    def _trend(self, sym: str, ts) -> float:
        st = self._close_by_sym[sym].loc[:ts]
        if len(st) < self.ma_window:
            return 0.0
        ma = st.rolling(self.ma_window, min_periods=self.ma_window).mean().iloc[-1]
        return 1.0 if st.iloc[-1] >= ma else 0.0

    def _vol(self, sym: str, ts) -> float:
        st = self._close_by_sym[sym].loc[:ts]
        if len(st) < self.vol_window:
            return 0.0
        return float(st.pct_change().rolling(self.vol_window, min_periods=self.vol_window).std().iloc[-1] or 0.0)

    def _obs(self, i: int) -> np.ndarray:
        snap = self._snapshots[i]
        return np.concatenate([snap["exp_ret"], snap["trend"], snap["vol"], self._pos]).astype(float)

    # ---- 环境接口 ----
    def reset(self) -> np.ndarray:
        self._step = 0
        self._pos = np.zeros(len(self.symbols))
        self._cash = self.initial_capital
        return self._obs(0)

    def step(self, action):
        """action = 离散组合索引 → 多头仓位档位（无做空）。返回 (obs, reward, done, info)。"""
        act_idx = int(np.asarray(action).reshape(-1)[0])
        act_idx = int(np.clip(act_idx, 0, self.n_actions - 1))
        act = self._pos_map[act_idx]  # (n_sym,) 仓位档位
        if self._step >= len(self.dates) - 1:
            done = True
            return self._obs(self._step), 0.0, done, {"episode": True}
        d = self.dates[self._step]
        # 实现收益（当前持仓在 5 日 horizon 的实现）
        pnl = 0.0
        for j, sym in enumerate(self.symbols):
            r = self._realized.get((sym, d), 0.0)
            pnl += self._pos[j] * self.notional_frac * r
        # 奖励工程：pnl 以基点计（×10000，避免与换手惩罚尺度失衡）；趋势惩罚编码
        # "下跌趋势不做多"经验：trend=0 时持仓 → 惩罚
        snap = self._snapshots[self._step]
        pnl_bp = pnl * 10000.0
        trend_pen = float(np.sum(act * (1.0 - snap["trend"])))  # 下跌趋势中的多头仓位和
        turn = float(np.abs(act - self._pos).sum())
        reward = self.w_pnl * pnl_bp - self.w_turn * turn - self.w_trend * trend_pen
        self._pos = act
        self._step += 1
        info = {"step": self._step, "pnl_bp": pnl_bp, "turn": turn, "trend_pen": trend_pen}
        return self._obs(self._step), float(reward), False, info

    def close(self):  # noqa: D401
        return None


class _Box:
    """极简 Box 空间。"""

    def __init__(self, low, high, shape):
        self.low = low
        self.high = high
        self.shape = shape


class _Discrete:
    """极简 Discrete 空间。"""

    def __init__(self, n: int):
        self.n = n


def _build_pos_map(n_sym: int, levels: np.ndarray) -> np.ndarray:
    """枚举 3^n 组合仓位矩阵 (n_actions, n_sym)。"""
    import itertools

    combos = list(itertools.product(levels, repeat=n_sym))
    return np.array(combos, dtype=float)


def check_env(env: SentinelTradingEnv) -> bool:
    obs = env.reset()
    assert obs.shape == (env.obs_dim,), f"obs shape {obs.shape} != {env.obs_dim}"
    act = np.zeros(env.n_actions)
    for _ in range(3):
        obs, r, done, _ = env.step(act)
    return True
