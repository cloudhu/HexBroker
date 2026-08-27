"""P0-1 撮合假设开关与双口径能力（新增模块，默认值 = 生产基线口径零变化）。

承载三类能力：
- ``ExecutionConfig``：撮合假设开关的单一载体（``next_bar_execution`` /
  ``volume_cap`` / ``volume_cap_mode``），默认值与生产基线一致；
- 语义函数：``next_bar_ref_price``（下一 bar 开盘价）、``cap_order_qty``
  （成交量约束下的目标仓位调整）；
- 双口径对照：``run_dual_caliber``（同 bar vs next_bar 绩效对照，供报告使用）。

设计红线：默认配置（``next_bar_execution=False``、``volume_cap=None``）下，
``BacktestEngine`` 走与原代码逐语句等价的路径，数值完全不变（<1e-12）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..constants import VOLUME_CAP_DEFAULT_RATIO


@dataclass(frozen=True)
class ExecutionConfig:
    """撮合假设开关（默认值 = 生产基线口径，零变化）。

    参数
    ----
    next_bar_execution : bool
        False：同 bar close 成交（生产基线）；True：成交价取下一 bar open ± 滑点（Q1 裁决）。
    volume_cap : float | dict[str, float] | None
        None：无成交量约束；float：统一比例（下单量 ≤ bar.volume × cap）；
        dict[symbol, ratio]：per-symbol 覆盖（缺失品种回退 ``VOLUME_CAP_DEFAULT_RATIO``）。
    volume_cap_mode : str
        "partial"：按比例部分成交（剩余丢弃不追单）；"reject"：整单拒绝。
    """

    next_bar_execution: bool = False
    volume_cap: float | dict[str, float] | None = None
    volume_cap_mode: str = "partial"

    @classmethod
    def from_cfg(cls, cfg: Any) -> "ExecutionConfig":
        """从配置对象（HexConfig / DictConfig 均可）读取撮合开关。"""
        bt = getattr(cfg, "backtest", None)
        return cls(
            next_bar_execution=bool(getattr(bt, "next_bar_execution", False)),
            volume_cap=getattr(bt, "volume_cap", None),
            volume_cap_mode=str(getattr(bt, "volume_cap_mode", "partial")),
        )

    def resolve_cap(self, symbol: str) -> float | None:
        """解析某标的的成交量比例上限；支持 per-symbol dict 覆盖（Q4 裁决）。"""
        if self.volume_cap is None:
            return None
        if isinstance(self.volume_cap, dict):
            if symbol in self.volume_cap:
                return float(self.volume_cap[symbol])
            short = symbol.split(".")[-1]
            if short in self.volume_cap:
                return float(self.volume_cap[short])
            return float(VOLUME_CAP_DEFAULT_RATIO)
        return float(self.volume_cap)


def next_bar_ref_price(open_series: pd.Series, ts: Any) -> float | None:
    """返回 ts 下一根 bar 的 open；无下一根返回 None（调用方跳过成交）。

    ``open_series`` 应为已 ``shift(-1)`` 的 open 序列（预计算），
    索引与 prices 的 datetime 层级一致；末根 bar 对应 NaN → None。
    """
    if ts not in open_series.index:
        return None
    val = open_series.loc[ts]
    if pd.isna(val):
        return None
    return float(val)


def cap_order_qty(target_qty: float, current_qty: float, bar_volume: float,
                  cap: float, mode: str = "partial") -> float:
    """按 volume_cap 约束目标仓位。

    - partial：delta = target - current 截断到 ±(bar_volume×cap)（按比例部分成交，
      剩余丢弃不追单——单 bar 内不产生第二笔补单）；
    - reject ：超量时返回 current_qty（整单拒绝，delta=0）；未超量原样返回。

    ``cap<=0`` 或 ``bar_volume<=0`` 时视为无成交量（不可成交），返回 current_qty。
    """
    cap = float(cap)
    bar_volume = float(bar_volume)
    if cap <= 0.0 or bar_volume <= 0.0:
        return float(current_qty)
    max_delta = bar_volume * cap
    delta = float(target_qty) - float(current_qty)
    if mode == "reject":
        return float(target_qty) if abs(delta) <= max_delta else float(current_qty)
    # partial（默认）
    capped = float(np.clip(delta, -max_delta, max_delta))
    return float(current_qty) + capped


def run_dual_caliber(prices: pd.DataFrame, targets: pd.DataFrame, cfg: Any,
                     metrics=("sharpe", "calmar", "max_drawdown", "win_rate")) -> dict:
    """同 bar vs next_bar 双口径绩效对照。

    对同一 targets 分别以「同 bar close 成交」与「下一 bar open 成交」各跑一次
    回测，输出两组绩效与 Δ%。返回::

        {"same_bar": {...}, "next_bar": {...}, "delta_pct": {...},
         "note": "信号方向准确率为信号层指标，不受撮合口径影响（Δ%=0 预期）"}

    默认 ``metrics`` 元组与 PRD F1.4 一致（Sharpe / Calmar / MaxDD / 胜率）。
    """
    from ..evaluation.metrics import compute_metrics
    from .engine import BacktestEngine

    freq = str(getattr(getattr(cfg, "data", None), "freq", "1d"))

    def _run(execution: "ExecutionConfig") -> dict:
        eng = BacktestEngine(cfg, execution=execution)
        pf = eng.run(prices, targets)
        m = compute_metrics(pf.equity_curve, freq=freq)
        md = m.to_dict()
        return {k: md[k] for k in metrics}

    same = _run(ExecutionConfig(next_bar_execution=False, volume_cap=None))
    nxt = _run(ExecutionConfig(next_bar_execution=True, volume_cap=None))

    delta_pct: dict[str, float | None] = {}
    for k in metrics:
        base = same[k]
        if base is None or base == 0.0 or (isinstance(base, float) and not np.isfinite(base)):
            delta_pct[k] = None
        else:
            delta_pct[k] = float((nxt[k] - base) / abs(base))
    return {
        "same_bar": same,
        "next_bar": nxt,
        "delta_pct": delta_pct,
        "note": "信号方向准确率为信号层指标，不受撮合口径影响（Δ%=0 预期）",
    }
