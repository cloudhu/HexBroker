"""预测层核心契约（§3.2 / §8.4）。

- ``ForecastSignal``：预测层唯一对外产物（跨层真理）。
- ``ForecastModel``：所有预测模型的抽象基类，统一 ``fit / predict / save / load``。
- 统一约定：``predict`` 接收带 ``MultiIndex(symbol, datetime)`` 的特征 DataFrame，
  逐 bar 利用 ``lookback`` 窗口自回归地预测未来 ``horizon`` 步收益分布，
  输出 ``List[ForecastSignal]``。

所有模型**只能读取历史**，禁止同 bar 窥探未来（见 ``build_windows`` 的索引对齐）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from .. import HexConfigError
from ..utils.fingerprint import model_id as _model_id


@dataclass
class ForecastSignal:
    """预测信号（数据契约）。"""

    symbol: str
    ts: pd.Timestamp
    horizon: int
    p_up: float
    exp_ret: float
    quantiles: dict[str, float]
    vol_hat: float
    conf: float
    model_id: str
    train_end: pd.Timestamp
    is_effective: bool

    def to_record(self) -> dict[str, Any]:
        """转为可序列化的 dict（时间戳转 ISO）。"""
        d = asdict(self)
        d["ts"] = pd.Timestamp(self.ts).isoformat()
        d["train_end"] = pd.Timestamp(self.train_end).isoformat()
        return d

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "ForecastSignal":
        rec = dict(rec)
        rec["ts"] = pd.Timestamp(rec["ts"])
        rec["train_end"] = pd.Timestamp(rec["train_end"])
        return cls(**rec)


@dataclass
class TrainLog:
    """训练日志。"""

    loss: float = float("nan")
    n_epochs: int = 0
    n_params: int = 0
    extra: dict = field(default_factory=dict)


def build_windows(
    feature_df: pd.DataFrame, lookback: int
) -> tuple[np.ndarray, pd.MultiIndex]:
    """从特征 DataFrame 构造滑窗样本（严格因果，不窥探未来）。

    返回
    ----
    windows : (n_valid, lookback * n_features) 的 2D 数组
    valid_index : 每个窗口末端 bar 的 MultiIndex(symbol, datetime)
    """
    feats = feature_df.select_dtypes(include=[np.number]).fillna(0.0)
    col_order = list(feats.columns)
    windows: list[np.ndarray] = []
    sym_list: list[str] = []
    dt_list: list = []
    for sym in feature_df.index.get_level_values(0).unique():
        grp = feature_df.loc[[sym]].sort_index()
        gf = grp[col_order]
        arr = gf.to_numpy(dtype=float)
        idx_arr = grp.index
        syms_level = idx_arr.get_level_values(0)
        dts_level = idx_arr.get_level_values(1)
        for i in range(lookback, len(arr) + 1):
            windows.append(arr[i - lookback : i].reshape(-1))
            sym_list.append(str(syms_level[i - 1]))
            dt_list.append(dts_level[i - 1])
    if not windows:
        return np.empty((0, lookback * len(col_order))), pd.MultiIndex.from_arrays(
            [[], []], names=["symbol", "datetime"]
        )
    valid_index = pd.MultiIndex.from_arrays(
        [sym_list, pd.to_datetime(dt_list)], names=["symbol", "datetime"]
    )
    return np.stack(windows), valid_index


class ForecastModel(ABC):
    """预测模型抽象基类。"""

    #: 模型家族名（用于 model_id 与注册表）
    family: str = "base"

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        self.cfg = cfg
        self.horizon = int(getattr(cfg.forecast, "horizon", 5))
        self.lookback = int(getattr(cfg.feature, "normalize_window", 120) if hasattr(cfg, "feature") else 20)
        # 采用更紧凑的 lookback 用于自回归（避免窗口过大拖慢 CPU 演示）
        self.lookback = min(self.lookback, 30)
        self.n_mc = int(getattr(cfg.forecast, "n_mc_samples", 30))
        self.eff_thr = float(getattr(cfg.forecast, "effective_threshold", 0.05))
        self._model_id = model_id
        self.train_end: Optional[pd.Timestamp] = None
        self._params: dict[str, Any] = {}

    @property
    def model_id(self) -> str:
        if self._model_id is None:
            cfg_dump = {}
            try:
                cfg_dump = self.cfg.model_dump()
            except Exception:
                cfg_dump = {}
            self._model_id = _model_id(
                {"family": self.family, **cfg_dump.get("forecast", {})},
                data_range=("na", "na"),
            )
        return self._model_id

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> TrainLog:
        """训练模型。X 为带 MultiIndex 的特征 DataFrame，y 为对齐的未来收益。"""

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> list[ForecastSignal]:
        """对特征 DataFrame 逐 bar 预测，返回信号列表。"""

    # ---- 保存 / 加载 ----
    def save(self, path: str | Any) -> None:
        """将模型参数落盘（pickle）。"""
        import pickle

        blob = {
            "family": self.family,
            "params": self._params,
            "model_id": self.model_id,
            "train_end": self.train_end.isoformat() if self.train_end is not None else None,
            "cfg_forecast": self.cfg.forecast.model_dump(),
            "horizon": self.horizon,
            "lookback": self.lookback,
            "n_mc": self.n_mc,
            "eff_thr": self.eff_thr,
        }
        with open(path, "wb") as f:
            pickle.dump(blob, f)

    @classmethod
    def load(cls, path: str | Any) -> "ForecastModel":
        import pickle

        with open(path, "rb") as f:
            blob = pickle.load(f)
        if blob["family"] != cls.family:
            raise HexConfigError(
                f"模型族不匹配：磁盘为 {blob['family']}，期望 {cls.family}"
            )
        obj = cls.__new__(cls)
        # 用一份最小 cfg 占位（预测只需 forecast 字段）
        from ..config import ForecastConfig

        obj.cfg = type("C", (), {"forecast": ForecastConfig(**blob["cfg_forecast"])})()
        obj.horizon = blob["horizon"]
        obj.lookback = blob["lookback"]
        obj.n_mc = blob["n_mc"]
        obj.eff_thr = blob["eff_thr"]
        obj._model_id = blob["model_id"]
        obj.train_end = pd.Timestamp(blob["train_end"]) if blob["train_end"] else None
        obj._params = blob["params"]
        return obj

    # ---- 自回归采样（子类提供单步预测） ----
    def sample_paths(self, windows: np.ndarray) -> np.ndarray:
        """循环采样 N 条独立收益路径，返回 (n_mc, horizon) 矩阵。

        子类需实现 ``_next_return``（返回每个窗口的单步收益均值/标准差）。
        """
        means, stds = self._next_return(windows)
        n = windows.shape[0]
        paths = np.zeros((self.n_mc, n, self.horizon))
        for s in range(self.n_mc):
            rng = np.random.default_rng(self._seed_for_sample(s))
            rets = np.zeros((n, self.horizon))
            cur_mean = means.copy()
            cur_std = stds.copy()
            for h in range(self.horizon):
                step = rng.normal(cur_mean, np.maximum(cur_std, 1e-6))
                rets[:, h] = step
            paths[s] = rets
        return paths

    def _seed_for_sample(self, s: int) -> int:
        base = int(getattr(self.cfg.forecast, "seed", 42) if hasattr(self.cfg, "forecast") else 42)
        return base * 1000 + s

    @abstractmethod
    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """返回 (mean, std) 形状均为 (n_windows,)。"""

    # ---- 由路径分布构造信号 ----
    def _signals_from_paths(
        self, paths: np.ndarray, valid_index: pd.MultiIndex
    ) -> list[ForecastSignal]:
        n = paths.shape[1]
        n_mc = paths.shape[0]  # MC 采样路径数（vol_hat 的样本维度，非窗口数）
        out: list[ForecastSignal] = []
        for i in range(n):
            cum = paths[:, i, :].sum(axis=1)  # 累计 horizon 步收益（每路径一个标量）
            p_up = float(np.mean(cum > 0))
            exp_ret = float(np.mean(cum))
            qs = {f"q{q}": float(np.quantile(cum, q / 100)) for q in (10, 25, 50, 75, 90)}
            # 用 n_mc 判 ddof：单条路径时 ddof=1 的 std 为 NaN；之前误用窗口数 n
            vol_hat = float(np.std(cum, ddof=1)) if n_mc > 1 else 0.0
            iqr = qs["q90"] - qs["q10"]
            conf = float(np.clip(1 - iqr / (6 * (vol_hat + 1e-9)), 0.0, 1.0))
            ts = valid_index[i][1]
            sym = valid_index[i][0]
            out.append(
                ForecastSignal(
                    symbol=sym,
                    ts=pd.Timestamp(ts),
                    horizon=self.horizon,
                    p_up=float(np.clip(p_up, 1e-6, 1 - 1e-6)),
                    exp_ret=exp_ret,
                    quantiles=qs,
                    vol_hat=vol_hat,
                    conf=conf,
                    model_id=self.model_id,
                    train_end=self.train_end if self.train_end is not None else pd.Timestamp(ts),
                    is_effective=abs(p_up - 0.5) > self.eff_thr,
                )
            )
        # 推理路径（predict）只调用 forward 不调用 backward，各层 _cache/_samples
        # 会跨折累积造成内存泄漏；在此统一清理（见 _clear_inference_caches）。
        self._clear_inference_caches()
        return out

    def _clear_inference_caches(self) -> None:
        """集中清理推理期各网络层的前向缓存（predict 不触发 backward）。

        覆盖自回归(ARTransformer)、TCN、GRU 三类模型使用的 encoder/head/blocks/
        convs/gru 组件，避免 65 折 × 数十测试窗的缓存跨折累积撑爆内存。
        """
        for obj in (getattr(self, "encoder", None), getattr(self, "head", None)):
            if hasattr(obj, "clear_cache"):
                obj.clear_cache()
        for blk in getattr(self, "blocks", []) or []:
            for sub in (getattr(blk, "attn", None), getattr(blk, "ffn", None)):
                if hasattr(sub, "clear_cache"):
                    sub.clear_cache()
        for conv in getattr(self, "convs", []) or []:
            if hasattr(conv, "clear_cache"):
                conv.clear_cache()
        gru = getattr(self, "gru", None)
        if hasattr(gru, "clear_cache"):
            gru.clear_cache()
