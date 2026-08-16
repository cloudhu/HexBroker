"""信号集成（§3.2）：多模型 OOS 信号的概率平均。

把多个 ``ForecastModel`` 在相同或不同 fold 上产出的信号按 (symbol, ts) 对齐，
对 ``p_up`` / ``exp_ret`` 做加权平均，得到更稳健的集成信号。
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from .base import ForecastSignal


def ensemble_signals(signal_lists: Iterable[list[ForecastSignal]], weights: list[float] | None = None) -> list[ForecastSignal]:
    """对多组信号做加权平均，返回集成后的信号列表。"""
    lists = [list(s) for s in signal_lists]
    if not lists:
        return []
    if weights is None:
        weights = [1.0] * len(lists)
    weights = np.asarray(weights, dtype=float)
    weights = weights / weights.sum()

    by_key: dict[tuple, list[tuple[float, ForecastSignal]]] = {}
    for w, sigs in zip(weights, lists):
        for s in sigs:
            key = (s.symbol, pd.Timestamp(s.ts))
            by_key.setdefault(key, []).append((float(w), s))

    out: list[ForecastSignal] = []
    for key, items in by_key.items():
        w_sum = sum(w for w, _ in items)
        # numpy>=2 弃用 np.sum(generator)，用内置 sum 聚合
        p_up = float(sum(w * s.p_up for w, s in items) / w_sum)
        exp_ret = float(sum(w * s.exp_ret for w, s in items) / w_sum)
        vol = float(sum(w * s.vol_hat for w, s in items) / w_sum)
        # 取最早 train_end 作为集成指纹
        train_end = min((s.train_end for _, s in items), default=key[1])
        model_ids = sorted({s.model_id for _, s in items})
        out.append(
            ForecastSignal(
                symbol=key[0],
                ts=key[1],
                horizon=items[0][1].horizon,
                p_up=float(np.clip(p_up, 1e-6, 1 - 1e-6)),
                exp_ret=exp_ret,
                quantiles={q: float(np.mean([s.quantiles.get(q, exp_ret) for _, s in items])) for q in ("q10", "q25", "q50", "q75", "q90")},
                vol_hat=vol,
                conf=float(np.mean([s.conf for _, s in items])),
                model_id="|".join(model_ids)[:24],
                train_end=train_end,
                # ForecastSignal 无 eff_thr 字段（校准层才赋予），默认 0.05 与校准契约一致
                is_effective=abs(p_up - 0.5) > getattr(items[0][1], "eff_thr", 0.05),
            )
        )
    out.sort(key=lambda s: (s.symbol, s.ts))
    return out
