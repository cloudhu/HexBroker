"""概率校准（§3.2）：把模型输出的 ``p_up`` 校准为经验概率。

实现 Platt 缩放（logistic 校准），并提供校准误差度量（分箱最大偏差）。
用于满足「校准误差 < 0.05」的验收要求。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _logistic(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


@dataclass
class PlattScaler:
    """Platt 缩放：``p_cal = sigmoid(a * logit(p) + b)``。"""

    a: float = 1.0
    b: float = 0.0

    def fit(self, p_raw: np.ndarray, y_true: np.ndarray, n_iter: int = 100, lr: float = 0.1) -> "PlattScaler":
        """用梯度下降拟合 a, b（NLL）。"""
        p = np.clip(np.asarray(p_raw, dtype=float), 1e-3, 1 - 1e-3)
        y = np.asarray(y_true, dtype=float)
        # 从 logit 空间初始化
        self.a = 1.0
        self.b = 0.0
        for _ in range(n_iter):
            z = np.log(p / (1 - p)) * self.a + self.b
            pr = _logistic(z)
            # dNLL/da = sum (pr - y) * logit(p); dNLL/db = sum (pr - y)
            da = np.mean((pr - y) * np.log(p / (1 - p)))
            db = np.mean(pr - y)
            self.a -= lr * da
            self.b -= lr * db
        return self

    def transform(self, p_raw: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(p_raw, dtype=float), 1e-3, 1 - 1e-3)
        return _logistic(np.log(p / (1 - p)) * self.a + self.b)


def calibration_error(p_pred: np.ndarray, y_true: np.ndarray, n_bins: int = 10) -> float:
    """分箱校准误差：各箱中 |平均预测概率 - 经验频率| 的最大值。"""
    p = np.asarray(p_pred, dtype=float)
    y = np.asarray(y_true, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    errs: list[float] = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        if i == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        if mask.sum() >= 10:  # 箱内有足够样本才统计
            errs.append(abs(p[mask].mean() - y[mask].mean()))
    return float(max(errs)) if errs else 0.0


def reliability_curve(p_pred: np.ndarray, y_true: np.ndarray, n_bins: int = 10) -> dict:
    """返回各箱的 (均值预测, 经验频率, 样本数)。"""
    p = np.asarray(p_pred, dtype=float)
    y = np.asarray(y_true, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    pred_m, obs_m, cnt = [], [], []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < n_bins - 1 else (p >= lo) & (p <= hi)
        if mask.sum() >= 10:
            pred_m.append(float(p[mask].mean()))
            obs_m.append(float(y[mask].mean()))
            cnt.append(int(mask.sum()))
    return {"pred": pred_m, "obs": obs_m, "count": cnt}


def calibrate_signals(signals, y_true: np.ndarray, method: str = "platt"):
    """就地校准信号列表的 ``p_up`` 与 ``is_effective``，返回使用的 scaler。

    method：
      - "platt"    —— Platt 缩放（logistic）
      - "isotonic" —— Isotonic 回归（单调非参数，sklearn）
      - "none"     —— 不校准（原样保留 p_up，仅重算 is_effective）
    """
    if not signals:
        return None
    if method == "platt":
        scaler = PlattScaler().fit([s.p_up for s in signals], y_true)
        for s in signals:
            s.p_up = float(np.clip(scaler.transform([s.p_up])[0], 1e-6, 1 - 1e-6))
            s.is_effective = abs(s.p_up - 0.5) > s.eff_thr if hasattr(s, "eff_thr") else abs(s.p_up - 0.5) > 0.05
        return scaler
    if method == "isotonic":
        return _calibrate_isotonic(signals, y_true)
    if method == "none":
        for s in signals:
            s.is_effective = abs(s.p_up - 0.5) > s.eff_thr if hasattr(s, "eff_thr") else abs(s.p_up - 0.5) > 0.05
        return None
    raise ValueError(f"未知校准方法：{method}")


def _calibrate_isotonic(signals, y_true: np.ndarray):
    """Isotonic 校准（sklearn），p 越高 y=1 频率越高。

    注意：fold 内样本较少（>=20）时 isotonic 易过拟合，此处不做额外平滑，
    与 Platt 在同一样本量下公平对比。
    """
    from sklearn.isotonic import IsotonicRegression

    p_raw = np.clip(np.asarray([s.p_up for s in signals], dtype=float), 1e-3, 1 - 1e-3)
    y = np.asarray(y_true, dtype=float)
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip", increasing=True)
    iso.fit(p_raw, y)
    cal = np.clip(iso.predict(p_raw), 1e-6, 1 - 1e-6)
    for s, c in zip(signals, cal):
        s.p_up = float(c)
        s.is_effective = abs(s.p_up - 0.5) > s.eff_thr if hasattr(s, "eff_thr") else abs(s.p_up - 0.5) > 0.05
    return iso
