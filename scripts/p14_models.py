"""P14 标签/任务错配修复：LightGBM 二分类预测模型（walk-forward 兼容）。

与 ``hexbroker.forecast.baselines.LightGBMForecast`` 同构接口（fit/predict），
可直接作为 ``walk_forward_lightgbm(..., model_cls=...)`` 的模型类传入，
因此与 v8 完全共享特征构建 / 标签面板 / walk-forward 网格 / per-fold 校准。

P14-1 立项（主理人）：
  引擎 A 训练=回归预测收益水平（cross_z 标签），推理=截面选 top30% 做多——
  两目标不同（P13 定论：多模型融合改善 IC 但不转化收益，瓶颈在标签/任务错配）。
  本模型把训练目标改为与推理同构的**二元分类**：标签 y = 当日全品种截面 fwd
  收益是否 top30%（label_mode="cross_top30"）→ objective=binary，输出概率
  p_buy；引擎 A 按日截面 rank(p_buy) 选 top30%（与 exp_ret 选择同构）。

超参口径（与 v8 同 HP）：
  读取 cfg.forecast.lgbm_*（walk_forward_lightgbm 的 params 会 setattr 注入），
  与 LightGBMForecast 完全一致；仅 objective 改为 binary，无均值/方差双模型、
  无 MC 路径采样（分类概率本身就是 p_up，无需高斯采样）。

输出映射（信号列对齐 v8：symbol/ts/p_up/exp_ret/is_effective）：
  - p_up    = p_buy（分类概率，clip 到 [1e-6, 1-1e-6]）
  - exp_ret = p_buy（选择变量映射：引擎 A 按 exp_ret 截面 rank 即按 p_buy rank，
    与「rank(p_buy) 选 top30%」同构；引擎 A 无需改选择逻辑）
  - is_effective = abs(p_buy - 0.5) > eff_thr

注意（pickle 安全）：
  本模块必须是可导入模块（scripts.p14_models），不能只定义在 __main__ 里——
  ``_train_eval_fold`` 的 ProcessPoolExecutor 会按 qualified name 反序列化
  ``model_cls``，子进程需能 ``import scripts.p14_models``（同 P13 约定）。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from hexbroker.forecast.base import ForecastModel, ForecastSignal, TrainLog, build_windows


def _cfg_get(cfg: Any, *keys: str, default: Any = None) -> Any:
    """依次从 cfg.forecast.<key> 读取，缺省回退 default（与 baselines._cfg_get 同语义）。"""
    fc = getattr(cfg, "forecast", None)
    for k in keys:
        if fc is not None and hasattr(fc, k):
            return getattr(fc, k)
    return default


class LGBMClassifyForecast(ForecastModel):
    """LightGBM 二分类（objective=binary）：标签 = 当日截面 top30% → 0/1。

    P14-1 实验专用。超参读取 cfg.forecast.lgbm_*（与 v8 同 HP，仅 objective 换
    binary）；``fit`` 校验标签严格为 {0,1}（LightGBM binary objective 硬约束），
    ``predict`` 输出 p_up=exp_ret=p_buy（见模块 docstring 的映射说明）。
    """

    family = "lgbm_cls"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        self.n_estimators = int(_cfg_get(cfg, "lgbm_n_estimators", default=300))
        self.lr = float(_cfg_get(cfg, "lgbm_lr", default=0.05))
        self.max_depth = int(_cfg_get(cfg, "lgbm_max_depth", default=6))
        self.num_leaves = int(_cfg_get(cfg, "lgbm_num_leaves", default=31))
        self.min_child_samples = int(_cfg_get(cfg, "lgbm_min_child_samples", default=20))
        self.subsample = float(_cfg_get(cfg, "lgbm_subsample", default=1.0))
        self.colsample_bytree = float(_cfg_get(cfg, "lgbm_colsample_bytree", default=1.0))
        self.reg_lambda = float(_cfg_get(cfg, "lgbm_reg_lambda", default=0.0))
        self.reg_alpha = float(_cfg_get(cfg, "lgbm_reg_alpha", default=0.0))
        self._model = None
        self._mean_model = None  # 兼容 walk_forward collect_models 路径（collect=False 不访问）
        self._var_model = None

    def _lgbm_kwargs(self) -> dict:
        """构造 LGBMClassifier 超参（与 LightGBMForecast 同 HP，仅 objective=binary）。"""
        return dict(
            objective="binary",
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
            n_jobs=int(_cfg_get(self.cfg, "lgbm_n_jobs", default=1)),
            random_state=42,
        )

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # ForecastModel 抽象方法占位：分类模型不使用 MC 路径采样——
        # predict() 直接输出 predict_proba 概率（p_up=exp_ret=p_buy），
        # 不调用 sample_paths / _next_return（调用即编程错误，显式报错）。
        raise NotImplementedError(
            "LGBMClassifyForecast 不使用 MC 路径采样（predict 直接输出分类概率）"
        )

    @staticmethod
    def _flatten(windows: np.ndarray) -> np.ndarray:
        return windows.reshape(windows.shape[0], -1)

    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        from lightgbm import LGBMClassifier

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("LGBMClassify: 无可用训练窗口。")
        self.n_features = windows.shape[1] // self.lookback
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        feats = self._flatten(windows)
        y = np.asarray(y, dtype=float)
        uniq = np.unique(y[~np.isnan(y)])
        if not np.all(np.isin(uniq, [0.0, 1.0])):
            raise ValueError(f"LGBMClassify: 标签必须为 0/1，实际唯一值 {uniq[:10]}")
        self._model = LGBMClassifier(**self._lgbm_kwargs())
        self._model.fit(feats, y)
        pred = self._model.predict_proba(feats)[:, 1]
        # 二元交叉熵（log 损失）作为 fit 报告损失
        loss = float(-np.mean(
            y * np.log(np.clip(pred, 1e-9, 1.0)) + (1.0 - y) * np.log(np.clip(1.0 - pred, 1e-9, 1.0))
        ))
        self._params = {"loss": loss, "n_estimators": self.n_estimators}
        return TrainLog(loss=loss, n_epochs=self.n_estimators, n_params=-1)

    def predict(self, X: pd.DataFrame) -> list:
        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0 or self._model is None:
            return []
        feats = self._flatten(windows)
        p = self._model.predict_proba(feats)[:, 1].astype(float)
        out: list = []
        for i in range(windows.shape[0]):
            p_buy = float(np.clip(p[i], 1e-6, 1 - 1e-6))
            ts = pd.Timestamp(valid_idx[i][1])
            sym = valid_idx[i][0]
            out.append(
                ForecastSignal(
                    symbol=sym,
                    ts=ts,
                    horizon=self.horizon,
                    p_up=p_buy,
                    exp_ret=p_buy,  # 选择变量映射：exp_ret = p_buy（见模块 docstring）
                    quantiles={f"q{q}": p_buy for q in (10, 25, 50, 75, 90)},
                    vol_hat=0.0,
                    conf=float(abs(2.0 * p_buy - 1.0)),
                    model_id=self.model_id,
                    train_end=self.train_end if self.train_end is not None else ts,
                    is_effective=abs(p_buy - 0.5) > self.eff_thr,
                )
            )
        return out
