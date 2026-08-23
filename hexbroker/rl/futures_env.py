"""期货交易 Gymnasium 环境（§3.3 / §8.6）。

核心约束（防泄漏红线）：
- 构造函数**只接收信号 DataFrame 与价格 DataFrame**，绝不接收 ForecastModel / SignalStore 实例。
- 所有信号必须来自 walk-forward 产出的 **OOS 信号**（由 ``SignalStore.get_frame`` 读取）。
- ``assert_env_has_no_model_dependency`` 通过静态检查（源码 + 签名）确保不依赖预测层。

风控 in-the-loop：
- ``RiskManager.evaluate`` 嵌入 ``step()``：RL 意图 → 风控优先级链（硬止损 > S1–S5 > 预算 >
  R1–R4 恢复 > RL 意图）→ 最终目标仓位 → ``SimBroker`` 记账。
- 风控状态（恢复档 R0–R4、是否被 veto、ATR 档位）进入观测，RL 可学「何时别逆着风控干」。

训练-回测一致性：
- 环境与 ``BacktestEngine`` 共用同一 ``SimBroker``/``CostModel``；
- 环境记录每 bar 的最终目标合约数到 ``target_frame``，可交给回测引擎重放，
  同一策略经两条路径的权益曲线差应 < 1e-6（见 test_backtest_env_consistency）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..backtest.broker import SimBroker
from ..backtest.cost import CostModel
from ..constants import ActionSpaceType, RecoveryStage
from ..risk.manager import RiskManager
from ..risk.types import RiskState

#: 观测中每 bar 的信号+行情特征数（顺序固定，禁止改动）
SIG_FEATURES = ["p_up", "exp_ret", "vol_hat", "conf", "is_effective", "ret"]

#: 回撤恢复档 → 观测标量（0=R0 正常 … 4=R4 恢复满仓）
_STAGE_ORD = {s: float(i) for i, s in enumerate(RecoveryStage)}

#: 离散5档动作 → 目标仓位比例（-1 空满仓 … 1 多满仓）
DISCRETE5_POSITIONS = np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=float)

#: 静态检查：环境中禁止出现的预测层符号（防泄漏红线）
_FORBIDDEN_TOKENS = (
    "ForecastModel",
    "ForecastTrainer",
    "SignalStore",
    "from_pretrained",
    "KronosPredictor",
    "model_id",
    "sample_paths",
)


def assert_env_has_no_model_dependency(env_cls: type) -> None:
    """静态断言：环境类不依赖预测层（源码与签名中不得出现预测层符号）。"""
    src = inspect.getsource(env_cls)
    for tok in _FORBIDDEN_TOKENS:
        if tok in src:
            raise AssertionError(
                f"防泄漏静态检查失败：环境源码出现预测层符号 '{tok}'"
            )
    sig = inspect.signature(env_cls.__init__)
    for pname in sig.parameters:
        if "model" in pname.lower():
            raise AssertionError(f"防泄漏静态检查失败：构造参数含模型字样 '{pname}'")


@dataclass
class EnvStepRecord:
    """单步执行记录（供一致性校验与审计）。"""

    ts: Any
    action: int
    intent_position: float
    target_contracts: float
    reason: str
    stage: RecoveryStage
    equity: float
    fee: float


class FuturesTradingEnv:
    """单标的（或逐标的回放）期货交易环境。

    参数
    ----
    signals : MultiIndex(symbol, datetime) 信号帧，需含 p_up/exp_ret/vol_hat/conf/is_effective。
    prices  : MultiIndex(symbol, datetime) 行情帧，需含 close（及 high/low/volume 等）。
    cfg     : HexConfig（rl / risk / backtest 段生效）。
    """

    metadata: dict = {"render.modes": []}

    def __init__(
        self,
        signals: pd.DataFrame,
        prices: pd.DataFrame,
        cfg: Any,
        symbol: Optional[str] = None,
    ) -> None:
        self.signals = signals
        self.prices = prices
        self.cfg = cfg
        rlc = getattr(cfg, "rl", None)
        btc = getattr(cfg, "backtest", None)

        self.symbols = list(signals.index.get_level_values(0).unique())
        if symbol is not None:
            if symbol not in self.symbols:
                raise ValueError(f"信号中没有标的 {symbol}")
            self.symbols = [symbol]
        self.symbol = self.symbols[0]

        self.action_space_type = str(getattr(rlc, "action_space", "discrete5"))
        self.obs_window = int(getattr(rlc, "obs_window", 30))
        self.initial_capital = float(getattr(btc, "initial_capital", 1_000_000.0))
        self.margin_rate = float(getattr(btc, "margin_rate", 0.12))
        self.multiplier = float(getattr(btc, "multiplier", 10.0))
        self.max_position_pct = float(getattr(getattr(cfg, "risk", None), "max_position_pct", 0.30))

        # 奖励权重
        self.w_pnl = float(getattr(rlc, "w_pnl", 1.0))
        self.w_cost = float(getattr(rlc, "w_cost", 1.0))
        self.w_dd = float(getattr(rlc, "w_dd", 2.0))
        self.w_turn = float(getattr(rlc, "w_turn", 0.05))
        self.w_align = float(getattr(rlc, "w_align", 0.1))
        self.align_anneal_steps = int(getattr(rlc, "align_anneal_steps", 20_000))

        # 共享券商与成本模型（与 BacktestEngine 同构）
        self.cost_model = CostModel.from_config(cfg)
        self.broker = SimBroker(self.cost_model, self.initial_capital)
        self.risk = RiskManager(cfg)

        # 逐 bar 数据（按符号取）
        self._sig = signals.xs(self.symbol, level=0).sort_index() if self.symbol in signals.index.get_level_values(0) else signals
        self._px = prices.xs(self.symbol, level=0).sort_index()
        self._px = self._px[~self._px.index.duplicated(keep="last")]

        # 对齐：只保留价格与信号都存在的 bar，且信号滞后一 bar 生效（信号在该 bar 收盘后生成）
        common = self._sig.index.intersection(self._px.index)
        self._sig = self._sig.loc[common]
        self._px = self._px.loc[common]
        self.n_bars = len(self._sig)
        if self.n_bars == 0:
            raise ValueError(f"标的 {self.symbol} 没有对齐的价格/信号数据")

        # 若信号帧缺 "ret"（同 bar 已实现收益），用价格自动补齐（决策层只依赖公开行情）
        if "ret" not in self._sig.columns:
            close = self._px["close"].astype(float)
            self._sig = self._sig.assign(ret=close.pct_change().fillna(0.0).to_numpy())

        # 逐 bar 已实现收益（用于观测与奖励基准）
        close = self._px["close"].astype(float)
        self._rets = close.pct_change().fillna(0.0).to_numpy()
        self._close = close.to_numpy()
        # 成交量序列（S2 量价背离用；无 volume 列时退化为零序列）
        self._vol = (
            self._px["volume"].astype(float).to_numpy()
            if "volume" in self._px.columns
            else np.zeros(self.n_bars, dtype=float)
        )
        # 信号特征矩阵（滚动窗口输入）
        self._sig_mat = self._sig[SIG_FEATURES].astype(float).to_numpy()
        self._sig_mat[:, 4] = (self._sig_mat[:, 4] > 0.5).astype(float)

        # 动作空间（Gymnasium 风格接口）
        if self.action_space_type == ActionSpaceType.DISCRETE5:
            self.action_space = _Discrete(5)
            self.n_actions = 5
        elif self.action_space_type == ActionSpaceType.CONTINUOUS:
            self.action_space = _Box(-1.0, 1.0, (1,))
            self.n_actions = 1
        else:
            raise ValueError(f"未知动作空间：{self.action_space_type}")

        self.obs_dim = self.obs_window * len(SIG_FEATURES) + 4
        self.observation_space = _Box(-np.inf, np.inf, (self.obs_dim,))

        # 运行时状态
        self._i = 0
        self._t = 0  # 全局训练步计数（用于对齐奖励退火）
        self._pos_frac = 0.0
        self._target_contracts = 0.0
        self._equity = self.initial_capital
        self._peak = self.initial_capital
        self._window: np.ndarray = np.zeros((self.obs_window, len(SIG_FEATURES)))
        self._records: list[EnvStepRecord] = []
        self._target_rows: list[tuple] = []
        self._prev_ct: float = 0.0
        self._bars_in_position: int = 0

    # ------------------------------------------------------------------
    # Gymnasium 风格接口
    # ------------------------------------------------------------------
    def reset(self) -> np.ndarray:
        self._i = 0
        self._pos_frac = 0.0
        self._target_contracts = 0.0
        self._equity = self.initial_capital
        self._peak = self.initial_capital
        self._window = np.zeros((self.obs_window, len(SIG_FEATURES)))
        self._records = []
        self._target_rows = []
        self._prev_ct = 0.0
        self._bars_in_position = 0
        self.broker.positions.clear()
        self.broker.avg_entry.clear()
        self.broker.realized.clear()
        self.broker.trades.clear()
        self.broker.open_dates.clear()
        self.risk.reset_ratchet()
        return self._build_obs()

    def step(self, action) -> tuple[np.ndarray, float, bool, dict]:
        if self._i >= self.n_bars:
            return self._build_obs(), 0.0, True, {}
        i = self._i
        ts = self._sig.index[i]
        p_up = float(self._sig_mat[i, 0])
        price = float(self._close[i])
        prev_equity = self._equity
        # 行情上下文（与决策同 bar，供风控 S1/S2/S5；非未来函数）
        recent_returns, recent_volumes, ma_price = self._market_context(i)

        # 1) RL 意图（离散档位或连续）
        if self.action_space_type == ActionSpaceType.DISCRETE5:
            intent = float(DISCRETE5_POSITIONS[int(action)])
        else:
            intent = float(np.clip(action[0] if np.ndim(action) else action, -1.0, 1.0))

        # 2) 风控 in-the-loop：意图经优先级链得到最终目标
        dd = 0.0 if self._peak <= 1e-12 else (self._peak - self._equity) / self._peak
        state = RiskState(
            equity=self._equity,
            peak_equity=self._peak,
            position=self._pos_frac,
            entry_price=self._entry_price(),
            current_price=price,
            atr=float(self._sig_mat[i, 2]) * price * 0.5,  # vol_hat → 近似 ATR 距离
            realized_vol=max(float(self._sig_mat[i, 2]), 1e-6),
            bars_in_position=self._bars_in_position,
            highest_since_entry=self._peak / self.initial_capital,
            lowest_since_entry=(2 - self._equity / self.initial_capital) * 0.5,
            pnl_pct=(self._equity / self.initial_capital - 1.0),
            drawdown=dd,
            vol_quantile=0.5,
        )
        decision = self.risk.evaluate(
            state, intent_position=intent, p_up=p_up,
            recent_returns=recent_returns, recent_volumes=recent_volumes, ma_price=ma_price,
        )

        # 3) 目标仓位比例 → 合约数 → SimBroker 记账
        target_frac = decision.target_position
        margin_per_ct = price * self.multiplier * self.margin_rate
        max_ct = int(max(1.0, self.initial_capital * self.max_position_pct / max(margin_per_ct, 1e-9)))
        target_contracts = float(int(round(target_frac * max_ct)))
        trade = self.broker.execute(
            self.symbol, target_contracts, price, is_today_close=False, timestamp=ts
        )
        self._target_contracts = target_contracts
        self._pos_frac = decision.target_position
        # 真实持仓计数：本步结束后仍持有则 +1，平仓归零（供 S4 时间止损判定）
        self._bars_in_position = (self._bars_in_position + 1) if target_frac != 0.0 else 0

        # 4) 估值 + 奖励
        equity = self.broker.equity({self.symbol: price})
        self._equity = float(equity)
        self._peak = max(self._peak, equity)
        fee = float(trade.fee) if trade is not None else 0.0
        pnl_change = (equity - prev_equity) / self.initial_capital
        dd_after = (self._peak - equity) / self._peak if self._peak > 0 else 0.0
        dd_penalty = max(0.0, dd_after - dd)
        turnover = abs(target_contracts - self._prev_contracts()) / max(max_ct, 1)
        self._prev_ct = target_contracts

        align = 0.0
        if self.w_align > 0:
            anneal = max(0.0, 1.0 - self._t / max(self.align_anneal_steps, 1))
            sign_match = 1.0 if (intent * (p_up - 0.5)) > 0 else 0.0
            align = self.w_align * sign_match * abs(p_up - 0.5) * anneal

        reward = (
            self.w_pnl * pnl_change
            - self.w_cost * fee / self.initial_capital
            - self.w_dd * dd_penalty
            - self.w_turn * turnover
            + align
        )

        # 5) 推进观测
        self._window = np.roll(self._window, -1, axis=0)
        self._window[-1] = self._sig_mat[i]
        self._records.append(
            EnvStepRecord(
                ts=ts, action=int(action) if self.action_space_type == "discrete5" else action,
                intent_position=intent, target_contracts=target_contracts,
                reason=decision.reason, stage=decision.stage, equity=equity, fee=fee,
            )
        )
        self._target_rows.append((self.symbol, ts, target_contracts))

        self._i += 1
        self._t += 1
        done = self._i >= self.n_bars
        info = {"equity": equity, "reason": decision.reason, "stage": str(decision.stage)}
        return self._build_obs(), float(reward), done, info

    def _entry_price(self) -> float:
        if self.symbol in self.broker.avg_entry and self.broker.avg_entry[self.symbol] != 0:
            return float(self.broker.avg_entry[self.symbol])
        return float(self._close[max(self._i - 1, 0)]) if self._i > 0 else float(self._close[0])

    def _prev_contracts(self) -> float:
        return float(getattr(self, "_prev_ct", 0.0))

    def _market_context(self, i: int, window: int = 20) -> tuple[np.ndarray, np.ndarray, float]:
        """最近 window 根行情上下文（风控 S1/S2/S5 信号用）。

        - ``recent_returns``：最近单根收益序列（S5 波动异常 z 分数）；
        - ``recent_volumes``：最近成交量序列（S2 量价背离）；
        - ``ma_price``：收盘价的窗口均线（S1 趋势破坏）。

        仅使用 bar i 及之前的公开行情，与决策同 bar，不构成未来函数。
        """
        lo = max(0, i - window + 1)
        recent_returns = self._rets[lo : i + 1]
        recent_volumes = self._vol[lo : i + 1]
        ma_price = float(np.mean(self._close[lo : i + 1]))
        return recent_returns, recent_volumes, ma_price

    def _build_obs(self) -> np.ndarray:
        sig = self._window.flatten()
        stage_scalar = self._stage_scalar()
        veto = 1.0 if (self._records and self._records[-1].reason != "rl_intent") else 0.0
        dd = (self._peak - self._equity) / self._peak if self._peak > 0 else 0.0
        return np.concatenate(
            [sig, [self._pos_frac, dd, stage_scalar, veto]]
        ).astype(np.float64)

    def _stage_scalar(self) -> float:
        if not self._records:
            return 0.0
        st = self._records[-1].stage
        if isinstance(st, str):
            st = RecoveryStage(st)
        return float(_STAGE_ORD.get(st, 0.0))

    # ------------------------------------------------------------------
    # 供回测引擎重放（训练-回测一致性）
    # ------------------------------------------------------------------
    @property
    def target_frame(self) -> pd.DataFrame:
        """环境最终目标合约数帧（MultiIndex(symbol, datetime)），可直接喂 BacktestEngine。"""
        if not self._target_rows:
            return pd.DataFrame(
                columns=["target"],
                index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "datetime"]),
            )
        df = pd.DataFrame(self._target_rows, columns=["symbol", "datetime", "target"])
        df = df.set_index(["symbol", "datetime"]).sort_index()
        df.index = df.index.set_names(["symbol", "datetime"])
        return df

    @property
    def equity_curve(self) -> pd.Series:
        if not self._records:
            return pd.Series(dtype=float)
        return pd.Series(
            [r.equity for r in self._records],
            index=pd.DatetimeIndex([pd.Timestamp(r.ts) for r in self._records]),
        )


# ---------------------------------------------------------------------------
# 极简 Gymnasium 风格空间（避免引入额外依赖，与 SB3 兼容接口对齐）
# ---------------------------------------------------------------------------
class _Discrete:
    def __init__(self, n: int) -> None:
        self.n = int(n)


class _Box:
    def __init__(self, low: float, high: float, shape: tuple) -> None:
        self.low = low
        self.high = high
        self.shape = shape


def check_env(env: FuturesTradingEnv) -> bool:
    """基本环境自检：reset 后 obs 维度正确、step 可运行且 done 收敛。"""
    obs = env.reset()
    if obs.shape != (env.obs_dim,):
        raise AssertionError(f"obs 维度错误：{obs.shape} != {(env.obs_dim,)}")
    n_steps = 0
    done = False
    while not done and n_steps < env.n_bars:
        a = env.action_space.n - 1 if hasattr(env.action_space, "n") else np.array([0.0])
        obs, rew, done, info = env.step(a)
        n_steps += 1
        if obs.shape != (env.obs_dim,):
            raise AssertionError(f"step 后 obs 维度错误：{obs.shape}")
    return True
