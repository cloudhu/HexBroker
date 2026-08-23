"""Walk-forward 回测器（§3.5 / §8.5）。

把 prices/targets 按时间切为若干等长子段，逐段用 ``BacktestEngine`` 独立回测，
输出每段绩效报告与聚合指标——用于跨时段稳健性检验（与 T01 ``WalkForwardSplitter``
的训练/测试切分互补：splitter 管训练数据防泄漏，本回测器管策略绩效分时段评估）。
"""

from __future__ import annotations

from typing import Any

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

        # E4 修复：短折（不足该 bar 数）不做年化，避免 3~5 bar 被年度化因子放大成无意义大数
        annualize_min_bars = 20

        reports: list[MetricsReport] = []
        for seg in edges:
            if len(seg) < 3:
                continue
            lo = ts[int(seg[0])]
            hi = ts[int(seg[-1])]
            plvl = prices.index.get_level_values(1)
            tlvl = targets.index.get_level_values(1)
            p_sub = prices[(plvl >= lo) & (plvl <= hi)]
            t_sub = targets[(tlvl >= lo) & (tlvl <= hi)]
            if len(p_sub) < 5:
                continue
            eng = BacktestEngine(self.cfg)
            pf = eng.run(p_sub, t_sub)
            do_annualize = len(p_sub) >= annualize_min_bars
            reports.append(
                compute_metrics(
                    pf.equity_curve,
                    freq=str(getattr(self.cfg.data, "freq", "1d")),
                    annualize=do_annualize,
                )
            )

        aggregate: dict = {}
        if reports:
            # 全部折参与聚合的指标
            all_keys = ["total_return", "max_drawdown", "win_rate", "profit_factor"]
            # 仅足够长的折参与年化聚合的指标（E4 修复：短折不年化）
            ann_keys = ["annual_return", "sharpe", "sortino", "calmar"]
            for k in all_keys:
                vals = np.array([getattr(r, k) for r in reports])
                aggregate[k] = float(vals.mean())
                aggregate[f"{k}_std"] = float(vals.std(ddof=1))
            for k in ann_keys:
                vals = np.array(
                    [getattr(r, k) for r in reports if r.n_bars >= annualize_min_bars]
                )
                aggregate[k] = float(vals.mean()) if vals.size else 0.0
                aggregate[f"{k}_std"] = float(vals.std(ddof=1)) if vals.size else 0.0
        return {"reports": reports, "aggregate": aggregate, "folds": len(reports)}
