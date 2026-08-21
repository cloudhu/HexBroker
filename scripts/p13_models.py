"""P13 多模型融合：XGBoost / HistGradientBoosting 预测模型（walk-forward 兼容）。

与 ``hexbroker.forecast.baselines.LightGBMForecast`` 同构：
- 分别回归均值（exp_ret）与残差平方（方差），方差用于 MC 路径采样
  （``ForecastModel.sample_paths`` → ``p_up/exp_ret`` 分布）。
- 实现 ``ForecastModel`` 接口（fit/predict/_next_return/_mean_model），
  可直接作为 ``walk_forward_lightgbm(..., model_cls=...)`` 的模型类传入，
  因此与 v8 完全共享特征构建 / 标签截面化 / walk-forward 网格 / per-fold 校准。

P13 立项（主理人）：
  在 LightGBM 之外引入 XGBoost 与 sklearn HistGradientBoosting，同特征同标签
  （v8 口径：label_pool=all + cross_z）独立 walk_forward 训练 → 融合 exp_ret
  → 重估引擎 A / 组合，目标改善 OOS 截面 IC 与单引擎表现。

超参口径（见 scripts/p13_multimodel.py 的 XGB_PARAMS / HGB_PARAMS）：
  - xgb：n_estimators=200 / lr=0.05 / max_depth=6 / subsample=0.8（与 LGB 对齐量级）
  - hgb：max_iter=200 / lr=0.05 / max_depth=6 / min_samples_leaf=20（sklearn HGB 替代 catboost）
  与 v8 完全同口径的仅限「数据/特征/标签/网格」；模型本体及其 HP 是本实验的变量。

注意（pickle 安全）：
  本模块必须是可导入模块（scripts.p13_models），不能只定义在 __main__ 里——
  ``_train_eval_fold`` 的 ProcessPoolExecutor 会按 qualified name 反序列化
  ``model_cls``，子进程需能 ``import scripts.p13_models``。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from hexbroker.forecast.base import ForecastModel, TrainLog, build_windows


def _cfg_get(cfg: Any, *keys: str, default: Any = None) -> Any:
    """依次从 cfg.forecast.<key> 读取，缺省回退 default（与 baselines._cfg_get 同语义）。"""
    fc = getattr(cfg, "forecast", None)
    for k in keys:
        if fc is not None and hasattr(fc, k):
            return getattr(fc, k)
    return default


class XGBoostForecast(ForecastModel):
    """XGBoost 强基线：分别回归均值与方差（残差平方），与 LightGBMForecast 同构。

    超参读取 cfg.forecast.xgb_*（walk_forward_lightgbm 的 params 会 setattr 注入）：
      xgb_n_estimators / xgb_lr / xgb_max_depth / xgb_min_child_weight /
      xgb_subsample / xgb_colsample_bytree / xgb_reg_lambda / xgb_reg_alpha / xgb_n_jobs。
    tree_method=hist 加速且与 LightGBM 的 leaf-wise 结构互补（diversity 来源）。
    """

    family = "xgboost"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.n_estimators = int(_cfg_get(cfg, "xgb_n_estimators", default=200))
        self.lr = float(_cfg_get(cfg, "xgb_lr", default=0.05))
        self.max_depth = int(_cfg_get(cfg, "xgb_max_depth", default=6))
        self.min_child_weight = float(_cfg_get(cfg, "xgb_min_child_weight", default=1.0))
        self.subsample = float(_cfg_get(cfg, "xgb_subsample", default=0.8))
        self.colsample_bytree = float(_cfg_get(cfg, "xgb_colsample_bytree", default=1.0))
        self.reg_lambda = float(_cfg_get(cfg, "xgb_reg_lambda", default=1.0))
        self.reg_alpha = float(_cfg_get(cfg, "xgb_reg_alpha", default=0.0))
        self._mean_model = None
        self._var_model = None

    def _xgb_kwargs(self) -> dict:
        """构造 XGBRegressor 共享超参（均值/方差模型一致；worker 内单线程防超订）。"""
        return dict(
            n_estimators=self.n_estimators,
            learning_rate=self.lr,
            max_depth=self.max_depth,
            min_child_weight=self.min_child_weight,
            subsample=self.subsample,
            colsample_bytree=self.colsample_bytree,
            reg_lambda=self.reg_lambda,
            reg_alpha=self.reg_alpha,
            tree_method="hist",
            n_jobs=int(_cfg_get(self.cfg, "xgb_n_jobs", default=1)),
            verbosity=0,
            random_state=42,
        )

    @staticmethod
    def _flatten(windows: np.ndarray) -> np.ndarray:
        return windows.reshape(windows.shape[0], -1)

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        from xgboost import XGBRegressor

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("XGBoost: 无可用训练窗口。")
        self.n_features = windows.shape[1] // self.lookback
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        feats = self._flatten(windows)
        y = np.asarray(y, dtype=float)

        self._mean_model = XGBRegressor(**self._xgb_kwargs())
        self._mean_model.fit(feats, y)
        resid = y - self._mean_model.predict(feats)
        var_target = resid ** 2
        self._var_model = XGBRegressor(**self._xgb_kwargs())
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


class HGBForecast(ForecastModel):
    """sklearn HistGradientBoostingRegressor 基线（catboost 缺失时的梯度提升替代）。

    超参读取 cfg.forecast.hgb_*：
      hgb_max_iter / hgb_lr / hgb_max_depth / hgb_min_samples_leaf / hgb_l2。
    HGB 为 level-wise 直方图提升，与 LightGBM(leaf-wise)/XGBoost(hist) 结构互补。
    sklearn HGB 无 feature_importances_ 属性；本实验 collect_models=False 不访问它。
    """

    family = "hgb"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.max_iter = int(_cfg_get(cfg, "hgb_max_iter", default=200))
        self.lr = float(_cfg_get(cfg, "hgb_lr", default=0.05))
        self.max_depth = int(_cfg_get(cfg, "hgb_max_depth", default=6))
        self.min_samples_leaf = int(_cfg_get(cfg, "hgb_min_samples_leaf", default=20))
        self.l2_regularization = float(_cfg_get(cfg, "hgb_l2", default=1.0))
        self._mean_model = None
        self._var_model = None
        # sklearn HistGradientBoosting 的 binning 在特征列 distinct<2 时崩溃
        # （sliding_window_view(distinct_values, 2)）。实测 walk_forward 的
        # build_windows 并不真正 fillna（fillna 只影响列选择，窗口值仍含 NaN），
        # 早期训练窗（滚动预热期）大量特征全 NaN → 全 0 → 恒定列 → 崩溃。
        # 修复：训练/预测前 nan_to_num（NaN→0，inf→±1e6），再对恒定列加
        # 1e-9 量级确定性微扰，保证每列至少 2 个不同值。
        # ⚠️ 观察项（QA 复核登记）：该微扰并非字面零信息——它在恒定列上引入
        # 行序单调的 1e-9 幅值斜坡（幅值可忽略，且按列乘不同系数避免共享同一
        # 时间趋势），仅用于规避 sklearn binning 崩溃；对模型预测的影响应视为
        # 可忽略但不为零。LightGBM/XGBoost 原生把 NaN 当缺失值处理，不受影响。
        self._n_const = 0

    def _hgb_kwargs(self) -> dict:
        """构造 HistGradientBoostingRegressor 共享超参（均值/方差模型一致）。"""
        return dict(
            max_iter=self.max_iter,
            learning_rate=self.lr,
            max_depth=self.max_depth,
            min_samples_leaf=self.min_samples_leaf,
            l2_regularization=self.l2_regularization,
            random_state=42,
        )

    @staticmethod
    def _flatten(windows: np.ndarray) -> np.ndarray:
        return windows.reshape(windows.shape[0], -1)

    @staticmethod
    def _sanitize(feats: np.ndarray) -> np.ndarray:
        """NaN→0、±inf→±1e6（build_windows 未真正 fillna，窗口值可能含 NaN/inf）。"""
        return np.nan_to_num(feats, nan=0.0, posinf=1e6, neginf=-1e6)

    def _prepare_train(self, feats: np.ndarray) -> np.ndarray:
        """训练矩阵预处理：sanitize + 恒定列确定性微扰（避免 sklearn HGB binning 崩溃）。"""
        feats = self._sanitize(feats)
        const_idx = np.where(feats.std(axis=0) == 0.0)[0]
        self._n_const = int(len(const_idx))
        if len(const_idx) > 0:
            feats = feats.copy()
            # 每列不同尺度的 1e-9 级微扰（行单调，但幅值可忽略、且不共享同一时间趋势）
            ramp = np.linspace(0, 1e-9, feats.shape[0])[:, None] * (
                np.arange(len(const_idx))[None, :] + 1
            )
            feats[:, const_idx] = feats[:, const_idx] + ramp
        return feats

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        from sklearn.ensemble import HistGradientBoostingRegressor

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("HGB: 无可用训练窗口。")
        self.n_features = windows.shape[1] // self.lookback
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        feats = self._prepare_train(self._flatten(windows))
        y = np.asarray(y, dtype=float)

        self._mean_model = HistGradientBoostingRegressor(**self._hgb_kwargs())
        self._mean_model.fit(feats, y)
        resid = y - self._mean_model.predict(feats)
        var_target = resid ** 2
        self._var_model = HistGradientBoostingRegressor(**self._hgb_kwargs())
        self._var_model.fit(feats, var_target)

        pred = self._mean_model.predict(feats)
        loss = float(np.mean((y - pred) ** 2))
        self._params = {"loss": loss, "n_estimators": self.max_iter,
                        "n_const_jittered": self._n_const}
        return TrainLog(loss=loss, n_epochs=self.max_iter, n_params=-1)

    def predict(self, X: pd.DataFrame) -> list:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0 or self._mean_model is None:
            return []
        self._next_return(windows)
        paths = self.sample_paths(windows)
        return self._signals_from_paths(paths, valid_idx)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        feats = self._sanitize(self._flatten(windows))
        mean = self._mean_model.predict(feats)
        var = np.maximum(self._var_model.predict(feats), 1e-8)
        return mean, np.sqrt(var)
