"""预测层训练编排（§3.2 / §8.4）。

``ForecastTrainer`` 负责：逐标的、按 walk-forward 折训练 ``ForecastModel``，
在测试窗产出 **样本外（OOS）** 信号，用测试窗已实现收益做概率校准，
最终写入 ``SignalStore``。全程严格因果，绝不把测试信息反馈给训练。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from ..utils.registry import build as build_model
from . import ForecastSignal, build_windows
from .base import ForecastSignal as _FS
from .calibration import calibrate_signals

_log = get_logger("FCST")


@dataclass
class TrainResult:
    """训练结果汇总。"""

    model_id: str
    n_oos_signals: int = 0
    n_folds: int = 0
    models: list = field(default_factory=list)
    calibration_errors: list = field(default_factory=list)


class ForecastTrainer:
    """Walk-forward 预测训练器。"""

    def __init__(self, cfg: Any, store: Any, model_name: Optional[str] = None) -> None:
        self.cfg = cfg
        self.store = store
        self.model_name = model_name or getattr(cfg.forecast, "name", "ar_transformer")
        self.horizon = int(getattr(cfg.forecast, "horizon", 5))

    # ---- 由收盘价构造位置对齐的前向收益（仅用历史，含 NaN 末段） ----
    @staticmethod
    def _forward_returns(close: pd.Series, horizon: int) -> pd.Series:
        vals = close.astype(float).values
        if len(vals) <= horizon:
            return pd.Series(np.nan, index=close.index)
        fwd = vals[horizon:] / vals[:-horizon] - 1.0
        fwd = np.concatenate([fwd, np.full(horizon, np.nan)])
        return pd.Series(fwd, index=close.index)

    def run(self, barframe: Any, feature_frame: Any,
            fingerprint: Any = None) -> TrainResult:
        """对 feature_frame 每个标的做 walk-forward 训练，产出 OOS 信号。

        ``fingerprint`` 可选（``FourLayerFingerprint`` 或 dict，默认 None）：
        None 时由 trainer 内部计算（指纹计算在 ``utils/fingerprint``，不引入模型），
        并随 ``store.put`` 写入 SignalStore sidecar（P0-3）。
        """
        from ..data.splitter import WalkForwardSplitter

        dc = self.cfg.data
        splitter = WalkForwardSplitter(
            train_len=int(dc.train_len),
            test_len=int(dc.test_len),
            purge=int(dc.purge),
            embargo=int(dc.embargo),
            mode=str(dc.mode),
        )

        result = TrainResult(model_id="")
        all_signals: list[ForecastSignal] = []

        for sym in feature_frame.symbols:
            # 保留 MultiIndex(symbol, datetime)，供 build_windows 正确分组
            feat = feature_frame.df.loc[[sym]].sort_index()
            sym_df = barframe.by_symbol(sym)
            close = (
                sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
            )
            if len(feat) < self.cfg.feature.normalize_window + self.horizon + 10:
                _log.warning(f"[{sym}] 样本不足，跳过")
                continue
            fwd = self._forward_returns(close, self.horizon)
            idx = feat.index  # datetime
            folds = splitter.split(idx)
            if not folds:
                continue
            splitter.assert_no_leakage(folds)

            for fold in folds:
                # 训练切片：切到 train_max_pos - horizon，使所有训练窗口的标签
                # （前向 horizon 收益）都落在训练窗内，不窥探测试窗。
                t_max = fold.train_max_pos - self.horizon
                if t_max < self._lookback():
                    continue
                train_feat = feat.iloc[0 : t_max + 1]
                windows, valid_idx = build_windows(train_feat, self._lookback())
                if windows.shape[0] == 0:
                    continue
                y = fwd.loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)
                if np.isnan(y).any() or y.shape[0] < 20:
                    # 理论上切片已保证无 NaN；此处为防御性兜底
                    continue

                model = build_model(self.model_name, self.cfg)
                model.fit(train_feat, y)

                # 测试窗预测（OOS）
                test_feat = feat.iloc[fold.test_start : fold.test_end]
                raw_signals = model.predict(test_feat)
                if not raw_signals:
                    continue
                # 校准：用测试窗已实现方向
                test_ts = [s.ts for s in raw_signals]
                realized = fwd.loc[test_ts].to_numpy(dtype=float)
                y_true = (realized > 0).astype(float)
                valid = ~np.isnan(realized)
                if valid.sum() >= 20:
                    calibrate_signals(
                        [s for i, s in enumerate(raw_signals) if valid[i]],
                        y_true[valid],
                        method=str(getattr(self.cfg.forecast, "calibration_method", "platt")),
                    )
                # 记录 train_end 指纹（用于 OOS 信号可追溯）
                train_end_ts = idx.get_level_values(1)[fold.train_max_pos]
                for s in raw_signals:
                    s.train_end = pd.Timestamp(train_end_ts)
                    s.model_id = model.model_id
                all_signals.extend(raw_signals)
                result.n_folds += 1
                result.models.append(model)

        if all_signals:
            result.model_id = all_signals[0].model_id
            if fingerprint is None:
                try:
                    from ..utils.fingerprint import compute_four_layer

                    m0 = result.models[0]
                    fingerprint = compute_four_layer(
                        self.cfg, barframe, m0.model_id,
                        all_signals[0].train_end, dict(getattr(m0, "_params", {}) or {}),
                    )
                except Exception:
                    fingerprint = None
            result.n_oos_signals = self.store.put(all_signals, fingerprint=fingerprint)
        _log.info(f"训练完成：model={result.model_id} folds={result.n_folds} oos={result.n_oos_signals}")
        return result

    def _lookback(self) -> int:
        lb = int(self.cfg.feature.normalize_window)
        return min(lb, 30)
