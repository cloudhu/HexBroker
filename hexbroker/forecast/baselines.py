"""预测层基线模型（§3.2）：TCN / GRU / LightGBM / Ensemble。

- ``TCNForecast``：纯 numpy 因果卷积时序网络（复用 ``CausalConv1D``）。
- ``GRUForecast``：纯 numpy GRU 时序网络（复用 ``GRU``）。
- ``LightGBMForecast``：基于 LightGBM 的强基线，分别回归均值与方差。
- ``EnsembleForecast``：LGB+XGB+CatBoost 三模型集成（各自回归均值/方差，
  采样路径后平均 p_up/exp_ret），降低单模型方差。

均实现 ``ForecastModel`` 接口，可无缝接入 ``trainer`` 与 ``SignalStore``。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.registry import register
from . import ForecastModel, TrainLog, build_windows
from ._nets import Adam, CausalConv1D, Linear, ReLU, Sequential, collect_params, train_gaussian


def _cfg_get(cfg, *keys, default=None):
    """依次从 cfg.forecast.<key> 读取，缺省回退 default。"""
    fc = getattr(cfg, "forecast", None)
    for k in keys:
        if fc is not None and hasattr(fc, k):
            return getattr(fc, k)
    return default


# --------------------------- TCN 基线 ---------------------------
@register("tcn")
class TCNForecast(ForecastModel):
    """时序因果卷积网络基线。"""

    family = "tcn"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.hidden = int(_cfg_get(cfg, "tcn_hidden", default=32))
        self.kernel = int(_cfg_get(cfg, "tcn_kernel", default=3))
        self.n_layers = int(_cfg_get(cfg, "tcn_layers", default=3))
        self.n_epochs = int(_cfg_get(cfg, "tcn_epochs", default=40))
        self.lr = float(_cfg_get(cfg, "tcn_lr", default=1e-3))
        self.batch = int(_cfg_get(cfg, "tcn_batch", default=256))
        self.n_features = 0
        self._rng = np.random.default_rng(int(_cfg_get(cfg, "seed", default=42)))
        self.optimizer = None  # type: ignore[assignment]

    def _build(self, n_features: int) -> None:
        self.n_features = n_features
        rng = self._rng
        self.encoder = Linear(n_features, self.hidden, rng)
        self.convs: list = []
        for _ in range(self.n_layers):
            self.convs.append(
                Sequential([CausalConv1D(self.hidden, self.hidden, self.kernel, rng), ReLU()])
            )
        self.head = Linear(self.hidden, 2, rng)
        self.optimizer = Adam(
            collect_params(self.encoder, *self.convs, self.head), lr=self.lr
        )

    def forward_windows(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.n_features == 0:
            self._build(windows.shape[1] // self.lookback)
        n = windows.shape[0]
        means = np.zeros(n)
        log_stds = np.zeros(n)
        for i in range(n):
            x = windows[i].reshape(self.lookback, self.n_features)
            h = self.encoder.forward(x)
            for conv in self.convs:
                h = conv.forward(h)
            out = self.head.forward(h[-1])
            means[i] = out[0]
            log_stds[i] = float(np.clip(out[1], -3.0, 1.0))
        return means, log_stds

    def backward_windows(self, dmean: np.ndarray, dlog_std: np.ndarray) -> None:
        n = len(dmean)
        for i in range(n):
            dout = np.array([dmean[i], dlog_std[i]])
            dh_last = self.head.backward(dout)
            dh = np.zeros((self.lookback, self.hidden))
            dh[-1] = dh_last
            for conv in reversed(self.convs):
                dh = conv.backward(dh)
            self.encoder.backward(dh)

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("TCN: 无可用训练窗口。")
        self._build(windows.shape[1] // self.lookback)
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        loss = train_gaussian(self, windows, y, n_epochs=self.n_epochs, lr=self.lr, batch=self.batch)
        self._params = {"loss": float(loss), "n_params": int(sum(p.data.size for p in self.optimizer.params))}
        return TrainLog(loss=float(loss), n_epochs=self.n_epochs,
                        n_params=int(sum(p.data.size for p in self.optimizer.params)))

    def predict(self, X: pd.DataFrame) -> list:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            return []
        self._next_return(windows)
        paths = self.sample_paths(windows)
        return self._signals_from_paths(paths, valid_idx)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        means, log_stds = self.forward_windows(windows)
        return means, np.exp(log_stds)


# --------------------------- GRU 基线 ---------------------------
@register("gru")
class GRUForecast(ForecastModel):
    """GRU 时序网络基线（纯 numpy 实现）。"""

    family = "gru"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.hidden = int(_cfg_get(cfg, "gru_hidden", default=32))
        self.n_epochs = int(_cfg_get(cfg, "gru_epochs", default=40))
        self.lr = float(_cfg_get(cfg, "gru_lr", default=1e-3))
        self.batch = int(_cfg_get(cfg, "gru_batch", default=256))
        self.n_features = 0
        self._rng = np.random.default_rng(int(_cfg_get(cfg, "seed", default=42)))
        self.optimizer = None  # type: ignore[assignment]

    def _build(self, n_features: int) -> None:
        self.n_features = n_features
        rng = self._rng
        from ._nets import GRU

        self.encoder = Linear(n_features, self.hidden, rng)
        self.gru = GRU(self.hidden, self.hidden, rng)
        self.head = Linear(self.hidden, 2, rng)
        self.optimizer = Adam(
            collect_params(self.encoder, self.gru, self.head), lr=self.lr
        )

    def forward_windows(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.n_features == 0:
            self._build(windows.shape[1] // self.lookback)
        n = windows.shape[0]
        means = np.zeros(n)
        log_stds = np.zeros(n)
        for i in range(n):
            x = windows[i].reshape(self.lookback, self.n_features)
            h = self.encoder.forward(x)
            last = self.gru.forward(h)
            out = self.head.forward(last)
            means[i] = out[0]
            log_stds[i] = float(np.clip(out[1], -3.0, 1.0))
        return means, log_stds

    def backward_windows(self, dmean: np.ndarray, dlog_std: np.ndarray) -> None:
        n = len(dmean)
        for i in range(n):
            dout = np.array([dmean[i], dlog_std[i]])
            dh_last = self.head.backward(dout)
            dx_seq = self.gru.backward(dh_last)
            self.encoder.backward(dx_seq)

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("GRU: 无可用训练窗口。")
        self._build(windows.shape[1] // self.lookback)
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        loss = train_gaussian(self, windows, y, n_epochs=self.n_epochs, lr=self.lr, batch=self.batch)
        self._params = {"loss": float(loss), "n_params": int(sum(p.data.size for p in self.optimizer.params))}
        return TrainLog(loss=float(loss), n_epochs=self.n_epochs,
                        n_params=int(sum(p.data.size for p in self.optimizer.params)))

    def predict(self, X: pd.DataFrame) -> list:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            return []
        self._next_return(windows)
        paths = self.sample_paths(windows)
        return self._signals_from_paths(paths, valid_idx)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        means, log_stds = self.forward_windows(windows)
        return means, np.exp(log_stds)


# --------------------------- LightGBM 基线 ---------------------------
@register("lightgbm")
class LightGBMForecast(ForecastModel):
    """LightGBM 强基线：分别回归均值与方差（残差平方）。"""

    family = "lightgbm"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.n_estimators = int(_cfg_get(cfg, "lgbm_n_estimators", default=300))
        self.lr = float(_cfg_get(cfg, "lgbm_lr", default=0.05))
        self.max_depth = int(_cfg_get(cfg, "lgbm_max_depth", default=6))
        self.num_leaves = int(_cfg_get(cfg, "lgbm_num_leaves", default=31))
        # 额外可调超参（默认取 LightGBM 原生默认，保持既有行为不变）
        self.min_child_samples = int(_cfg_get(cfg, "lgbm_min_child_samples", default=20))
        self.subsample = float(_cfg_get(cfg, "lgbm_subsample", default=1.0))
        self.colsample_bytree = float(_cfg_get(cfg, "lgbm_colsample_bytree", default=1.0))
        self.reg_lambda = float(_cfg_get(cfg, "lgbm_reg_lambda", default=0.0))
        self.reg_alpha = float(_cfg_get(cfg, "lgbm_reg_alpha", default=0.0))
        self._mean_model = None
        self._var_model = None

    def _lgbm_kwargs(self) -> dict:
        """构造 LGBMRegressor 共享超参（均值/方差模型一致）。"""
        return dict(
            n_estimators=self.n_estimators,
            learning_rate=self.lr,
            max_depth=self.max_depth,
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            colsample_bytree=self.colsample_bytree,
            reg_lambda=self.reg_lambda,
            reg_alpha=self.reg_alpha,
            verbose=-1,
            # 默认 1 以保持官方 ForecastTrainer 的确定性复现；调优/复跑可经
            # cfg.forecast.lgbm_n_jobs 提升（但折级并行时 worker 内应置 1 避免超订）。
            n_jobs=int(_cfg_get(self.cfg, "lgbm_n_jobs", default=1)),
        )

    @staticmethod
    def _flatten(windows: np.ndarray) -> np.ndarray:
        return windows.reshape(windows.shape[0], -1)

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        from lightgbm import LGBMRegressor

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("LightGBM: 无可用训练窗口。")
        self.n_features = windows.shape[1] // self.lookback
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        feats = self._flatten(windows)
        self._mean_model = LGBMRegressor(**self._lgbm_kwargs())
        self._mean_model.fit(feats, y)
        resid = y - self._mean_model.predict(feats)
        var_target = resid ** 2
        self._var_model = LGBMRegressor(**self._lgbm_kwargs())
        self._var_model.fit(feats, var_target)
        pred = self._mean_model.predict(feats)
        loss = float(np.mean((y - pred) ** 2))
        self._params = {"loss": loss, "n_estimators": self.n_estimators}
        return TrainLog(loss=loss, n_epochs=self.n_estimators, n_params=-1)

    def predict(self, X: pd.DataFrame) -> list:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0 or self._mean_model is None:
            return []
        self._next_return(windows)
        paths = self.sample_paths(windows)
        return self._signals_from_paths(paths, valid_idx)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        feats = self._flatten(windows)
        mean = self._mean_model.predict(feats)
        var = np.maximum(self._var_model.predict(feats), 1e-8)
        return mean, np.sqrt(var)


class EnsembleForecast(LightGBMForecast):
    """三模型集成：LightGBM + XGBoost + CatBoost。

    每个成员模型与 LightGBMForecast 同构：分别回归均值与残差平方（方差），
    采样路径后平均各成员路径分布 → 汇总 p_up/exp_ret/分位数。

    集成降低单模型方差；均值预测取 3 模型平均，方差取 3 模型平均的平方根。
    超参复用 lgbm_*（映射到 xgb/catboost 对应参数），并支持覆盖。
    """

    family = "ensemble"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self._members: list = []  # [(mean_model, var_model)]
        # 默认 LGB+XGB 双成员（CatBoost 在本环境 var 拟合触发原生崩溃，
        # 且该崩溃是进程级 SIGSEGV 无法 try/except 捕获）；显式设
        # ensemble_members="lgb,xgb,cat" 才尝试三成员。
        raw = str(_cfg_get(cfg, "ensemble_members", default="lgb,xgb")).lower()
        self._member_kinds = [k.strip() for k in raw.split(",") if k.strip()]

    # ---- 成员构造（懒导入，避免重依赖阻塞其它模型） ----
    def _build_member(self, kind: str):
        try:
            from lightgbm import LGBMRegressor
            from xgboost import XGBRegressor
            from catboost import CatBoostRegressor
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EnsembleForecast 需要 lightgbm/xgboost/catboost。请安装："
                "pip install lightgbm xgboost catboost"
            ) from exc

        common = dict(
            n_estimators=self.n_estimators,
            learning_rate=self.lr,
            max_depth=self.max_depth,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            colsample_bytree=self.colsample_bytree,
            reg_lambda=self.reg_lambda,
            reg_alpha=self.reg_alpha,
        )
        if kind == "lgb":
            return LGBMRegressor(**common, verbose=-1, n_jobs=1)
        if kind == "xgb":
            return XGBRegressor(
                n_estimators=self.n_estimators,
                learning_rate=self.lr,
                max_depth=self.max_depth,
                min_child_weight=max(1, int(self.min_child_samples // 4)),
                subsample=self.subsample,
                colsample_bytree=self.colsample_bytree,
                reg_lambda=self.reg_lambda,
                reg_alpha=self.reg_alpha,
                tree_method="hist",
                n_jobs=1,
                verbosity=0,
            )
        if kind == "cat":
            return CatBoostRegressor(
                iterations=self.n_estimators,
                learning_rate=self.lr,
                depth=self.max_depth,
                min_child_samples=self.min_child_samples,
                subsample=self.subsample,
                loss_function="MAE",  # MAE 对含极端值的方差目标更稳（RMSE 在真实数据上触发原生崩溃）
                l2_leaf_reg=max(self.reg_lambda, 3.0),
                verbose=False,
                thread_count=1,
                random_seed=42,
            )
        raise ValueError(f"未知集成成员：{kind}")

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("Ensemble: 无可用训练窗口。")
        self.n_features = windows.shape[1] // self.lookback
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        feats = self._flatten(windows)
        y = np.asarray(y, dtype=float)

        self._members = []
        for kind in self._member_kinds:
            mean_m = self._build_member(kind)
            mean_m.fit(feats, y)
            resid = y - mean_m.predict(feats)
            var_m = self._build_member(kind)
            # 方差目标用 log1p 缩放：r² 大量近零（尤其 MAE 拟合后）会触发
            # CatBoost 原生崩溃，log 空间数值更分散稳定；predict 时逆变换。
            var_m.fit(feats, np.log1p(np.maximum(resid, 0.0) ** 2 * self._VAR_SCALE))
            self._members.append((mean_m, var_m))

        if not self._members:
            raise RuntimeError("Ensemble: 无集成成员。")
        pred = self._ensemble_mean(feats)
        loss = float(np.mean((y - pred) ** 2))
        self._params = {"loss": loss, "n_estimators": self.n_estimators, "members": len(self._members)}
        return TrainLog(loss=loss, n_epochs=self.n_estimators, n_params=-1)

    _VAR_SCALE = 1e4  # log1p(r²·1e4) 目标缩放常数（predict 时 expm1/1e4 还原）

    def _ensemble_mean(self, feats: np.ndarray) -> np.ndarray:
        return np.mean([m.predict(feats) for m, _ in self._members], axis=0)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        feats = self._flatten(windows)
        means = np.array([m.predict(feats) for m, _ in self._members])
        # 方差目标 log1p(r²·1e4) → 还原 r² = expm1(pred)/1e4
        vars_ = np.array(
            [np.maximum(np.expm1(np.clip(v.predict(feats), -30, 30)) / self._VAR_SCALE, 1e-8)
             for _, v in self._members]
        )
        mean = means.mean(axis=0)
        # 总方差 = 成员均值方差（均值集成不确定性）+ 平均成员方差（观测噪声）
        var = (means.var(axis=0) + vars_.mean(axis=0))
        return mean, np.sqrt(np.maximum(var, 1e-8))

    @property
    def _mean_model(self):  # 兼容 walk_forward 的 collect_models（取 LGB 成员）
        return self._members[0][0] if self._members else None

    @_mean_model.setter
    def _mean_model(self, v):
        pass  # 保持父类占位符兼容（fit 前为 None）
