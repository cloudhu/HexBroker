"""P0-4 block bootstrap 绩效区间（新增统计层，只读复用 compute_metrics）。

对权益曲线收益序列做 circular block bootstrap（保留自相关结构），
输出 Sharpe / Calmar / MaxDD 的 2.5%–97.5% 分位 CI；固定 seed 完全可复现。
DSR/PBO 点估计逻辑零改动（并列输出，不参与闸门判定）。

参考：PyBroker bootstrap 绩效区间；block_len 默认 20 与日频月度自相关尺度匹配
（Q5 裁决）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import compute_metrics

_ANNUALIZE = {"1d": 252, "60m": 252 * 4, "30m": 252 * 8, "5m": 252 * 48, "1m": 252 * 240}


def block_bootstrap(returns: np.ndarray, block_len: int = 20,
                    n_boot: int = 1000, seed: int = 42) -> np.ndarray:
    """circular block bootstrap：返回 ``(n_boot, n)`` 重采样收益矩阵（numpy 向量化）。

    每段重采样序列由 ``ceil(n/block_len)`` 个循环块拼接后截断到 n；
    块起点均匀取自 ``[0, n)``，块内偏移取模 n（circular）。固定 seed 结果完全一致。
    """
    r = np.asarray(returns, dtype=float).ravel()
    n = r.shape[0]
    if n == 0:
        return np.empty((n_boot, 0), dtype=float)
    block_len = max(1, min(int(block_len), n))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    offsets = np.arange(block_len)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    idx = idx.reshape(n_boot, -1)[:, :n]
    return r[idx]


@dataclass
class MetricCI:
    """单指标的「点估计 + 95% CI」+ 参数快照（seed 入参保证可复现）。"""

    point: float
    ci_low: float
    ci_high: float
    n_boot: int
    block_len: int
    seed: int

    def to_dict(self) -> dict:
        return {
            "point": self.point,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "n_boot": self.n_boot,
            "block_len": self.block_len,
            "seed": self.seed,
        }


@dataclass
class BootstrapResult:
    """Bootstrap 绩效区间结果。"""

    sharpe: MetricCI
    calmar: MetricCI
    max_drawdown: MetricCI
    by_symbol: dict | None = None      # by_symbol=True 时 {symbol: {metric: MetricCI}}

    def to_dict(self) -> dict:
        return {
            "sharpe": self.sharpe.to_dict(),
            "calmar": self.calmar.to_dict(),
            "max_drawdown": self.max_drawdown.to_dict(),
            "by_symbol": self.by_symbol,
        }


def _metrics_from_returns(rets: np.ndarray, freq: str) -> tuple[float, float, float]:
    """由重采样收益序列计算 (sharpe, calmar, max_drawdown)（与 compute_metrics 同口径）。"""
    ann = _ANNUALIZE.get(freq, 252)
    n = rets.shape[0]
    if n < 2:
        return 0.0, 0.0, 0.0
    mean_r = float(rets.mean())
    std_r = float(rets.std(ddof=1))
    sharpe = float(mean_r / std_r * np.sqrt(ann)) if std_r > 1e-12 else 0.0
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    max_dd = float(dd.min())
    total_return = float(eq[-1] - 1.0)
    base = 1.0 + total_return
    if base > 0:
        annual_return = float(base ** (ann / max(n, 1)) - 1.0)
    elif total_return < 0:
        annual_return = -1.0
    else:
        annual_return = 0.0
    calmar = float(annual_return / abs(max_dd)) if abs(max_dd) > 1e-9 else 0.0
    return sharpe, calmar, max_dd


def bootstrap_metrics_ci(equity: pd.Series, *, freq: str = "1d",
                         block_len: int = 20, n_boot: int = 1000,
                         seed: int = 42, by_symbol: bool = False) -> BootstrapResult:
    """对权益曲线收益序列做 block bootstrap，输出 Sharpe/Calmar/MaxDD 的 95% CI。

    - ``point`` 取 ``compute_metrics`` 点估计（与报告口径一致）；
    - CI 为 1000 次重采样分布的 2.5%–97.5% 分位；
    - ``by_symbol=True`` 且 equity 为 MultiIndex(symbol, datetime) 时输出截面 CI；
      纯 DatetimeIndex 时忽略（返回 None）。
    """
    eq = equity.astype(float)
    if len(eq) < 2:
        z = MetricCI(0.0, 0.0, 0.0, int(n_boot), int(block_len), int(seed))
        return BootstrapResult(sharpe=z, calmar=z, max_drawdown=z)

    def _ci(m_point: float, boot_vals: np.ndarray) -> MetricCI:
        lo, hi = np.percentile(boot_vals, [2.5, 97.5])
        return MetricCI(
            point=float(m_point), ci_low=float(lo), ci_high=float(hi),
            n_boot=int(n_boot), block_len=int(block_len), seed=int(seed),
        )

    m_point = compute_metrics(eq, freq=freq)
    point = (m_point.sharpe, m_point.calmar, m_point.max_drawdown)

    by_symbol_out: dict | None = None
    if by_symbol and isinstance(eq.index, pd.MultiIndex) and eq.index.nlevels >= 2:
        by_sym: dict[str, dict] = {}
        for sym in sorted(eq.index.get_level_values(0).unique()):
            sub = eq.xs(sym, level=0)
            if len(sub) < 2:
                continue
            m_sub = compute_metrics(sub, freq=freq)
            rets = sub.pct_change().dropna().to_numpy(dtype=float)
            boot = block_bootstrap(rets, block_len, n_boot, seed)
            vals = np.array([_metrics_from_returns(b, freq) for b in boot])
            by_sym[sym] = {
                "sharpe": _ci(m_sub.sharpe, vals[:, 0]).to_dict(),
                "calmar": _ci(m_sub.calmar, vals[:, 1]).to_dict(),
                "max_drawdown": _ci(m_sub.max_drawdown, vals[:, 2]).to_dict(),
            }
        by_symbol_out = by_sym or None

    rets = eq.pct_change().dropna().to_numpy(dtype=float)
    boot = block_bootstrap(rets, block_len, n_boot, seed)
    vals = np.array([_metrics_from_returns(b, freq) for b in boot])

    return BootstrapResult(
        sharpe=_ci(point[0], vals[:, 0]),
        calmar=_ci(point[1], vals[:, 1]),
        max_drawdown=_ci(point[2], vals[:, 2]),
        by_symbol=by_symbol_out,
    )


def bootstrap_report_block(res: BootstrapResult) -> dict:
    """报告用块：``{metric: {point, ci_low, ci_high}, params: {...}}``（T05 接入 pipeline）。"""
    return {
        "sharpe": {"point": res.sharpe.point, "ci_low": res.sharpe.ci_low, "ci_high": res.sharpe.ci_high},
        "calmar": {"point": res.calmar.point, "ci_low": res.calmar.ci_low, "ci_high": res.calmar.ci_high},
        "max_drawdown": {"point": res.max_drawdown.point, "ci_low": res.max_drawdown.ci_low, "ci_high": res.max_drawdown.ci_high},
        "params": {
            "block_len": res.sharpe.block_len,
            "n_boot": res.sharpe.n_boot,
            "seed": res.sharpe.seed,
            "by_symbol": res.by_symbol is not None,
        },
    }
