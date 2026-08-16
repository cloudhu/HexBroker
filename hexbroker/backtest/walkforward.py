"""Walk-forward 回测器（§3.5 / §8.5）。

把 prices/targets 按时间切为若干等长子段，逐段用 ``BacktestEngine`` 独立回测，
输出每段绩效报告与聚合指标——用于跨时段稳健性检验（与 T01 ``WalkForwardSplitter``
的训练/测试切分互补：splitter 管训练数据防泄漏，本回测器管策略绩效分时段评估）。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from ..evaluation.metrics import MetricsReport, compute_metrics
from .engine import BacktestEngine


class WalkForwardBacktester:
    """分时段回测器。"""

    def __init__(self, cfg: Any, name: str = "walkforward") -> None:
        self.cfg = cfg
        self.name = name

    def run(
        self,
        prices: pd.DataFrame,
        targets: pd.DataFrame,
        n_folds: int = 3,
    ) -> dict:
        """逐时段回测。

        参数
        ----
        prices  : MultiIndex(symbol, datetime)，含 close。
        targets : MultiIndex(symbol, datetime)，含 target。
        n_folds : 把时间轴等分为多少段。

        返回
        ----
        {"reports": [MetricsReport...], "aggregate": {...}, "folds": n}
        """
        ts = sorted(prices.index.get_level_values(1).unique())
        if len(ts) < n_folds * 5:
            n_folds = max(1, len(ts) // 5)
        edges = np.array_split(np.arange(len(ts)), max(1, n_folds))

        reports: list[MetricsReport] = []
        for seg in edges:
            if len(seg) < 3:
                continue
            lo = ts[int(seg[0])]
            hi = ts[int(seg[-1])]
            p_sub = prices[prices.index.get_level_values(1).between(lo, hi)]
            t_sub = targets[targets.index.get_level_values(1).between(lo, hi)]
            if len(p_sub) < 5:
                continue
            eng = BacktestEngine(self.cfg)
            pf = eng.run(p_sub, t_sub)
            reports.append(compute_metrics(pf.equity_curve, freq=str(getattr(self.cfg.data, "freq", "1d"))))

        aggregate: dict = {}
        if reports:
            keys = ["total_return", "annual_return", "sharpe", "sortino", "max_drawdown", "calmar", "win_rate"]
            for k in keys:
                vals = np.array([getattr(r, k) for r in reports])
                aggregate[k] = float(vals.mean())
                aggregate[f"{k}_std"] = float(vals.std())
        return {"reports": reports, "aggregate": aggregate, "folds": len(reports)}
