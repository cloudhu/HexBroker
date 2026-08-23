"""自研 ARTransformer（纯 numpy，CPU 可跑，预测层主 fallback）。

解码式自回归 Transformer：把 ``lookback`` 窗口的逐 bar 特征当成序列，做因果自注意力，
于最后一个 token 输出「未来单步收益」的高斯分布 ``(mean, log_std)``。
无 torch 依赖，全部层前向/反向均为解析梯度，适配 ``train_gaussian`` 的批处理接口。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.registry import register
from . import ForecastModel
from ._nets import (
    Adam,
    Linear,
    ReLU,
    Sequential,
    collect_params,
    train_gaussian,
)


# --------------------------- 多头因果自注意力（FIFO 缓存） ---------------------------
class CausalSelfAttention:
    """多头因果自注意力（逐头循环以清晰支持反向）。"""

    def __init__(self, d_model: int, n_heads: int, rng: np.random.Generator, scale: float = 0.08) -> None:
        self.d_model = d_model
        self.n_heads = n_heads
        self.d_head = d_model // n_heads
        self.Wq = Linear(d_model, d_model, rng, scale)
        self.Wk = Linear(d_model, d_model, rng, scale)
        self.Wv = Linear(d_model, d_model, rng, scale)
        self.Wo = Linear(d_model, d_model, rng, scale)
        self._cache: list = []

    def forward(self, x: np.ndarray) -> np.ndarray:
        T = x.shape[0]
        Q = self.Wq.forward(x)
        K = self.Wk.forward(x)
        V = self.Wv.forward(x)
        scale = 1.0 / np.sqrt(self.d_head)
        Qh = Q.reshape(T, self.n_heads, self.d_head).transpose(1, 0, 2)
        Kh = K.reshape(T, self.n_heads, self.d_head).transpose(1, 0, 2)
        Vh = V.reshape(T, self.n_heads, self.d_head).transpose(1, 0, 2)
        mask = np.triu(np.ones((T, T), dtype=bool), k=1)  # True=未来，需屏蔽
        ctx = np.zeros((self.n_heads, T, self.d_head))
        attn_store: list = []
        for h in range(self.n_heads):
            scores = Qh[h] @ Kh[h].T * scale  # (T, T)
            scores = np.where(mask, -1e9, scores)
            sh = scores - scores.max(axis=1, keepdims=True)
            e = np.exp(sh)
            attn = e / e.sum(axis=1, keepdims=True)
            ctx[h] = attn @ Vh[h]
            attn_store.append(attn)
        ctx = ctx.transpose(1, 0, 2).reshape(T, self.d_model)
        out = self.Wo.forward(ctx)
        self._cache.append((x, Qh, Kh, Vh, mask, attn_store, scale, T))
        return out

    def backward(self, dout: np.ndarray) -> np.ndarray:
        x, Qh, Kh, Vh, mask, attn_store, scale, T = self._cache.pop(0)
        dctx = self.Wo.backward(dout)  # (T, d_model)
        dctxh = dctx.reshape(T, self.n_heads, self.d_head).transpose(1, 0, 2)
        dQh = np.zeros_like(Qh)
        dKh = np.zeros_like(Kh)
        dVh = np.zeros_like(Vh)
        for h in range(self.n_heads):
            attn = attn_store[h]  # (T, T)
            dctx_h = dctxh[h]  # (T, d_head)
            # 先对 V 投影得到 scores 的梯度 (T, T)
            dscores = dctx_h @ Vh[h].T
            # softmax 反向（行归一）：dscores = attn * (dscores - sum(attn*dscores))
            dscores = attn * (dscores - (attn * dscores).sum(axis=1, keepdims=True))
            dscores = dscores * (~mask).astype(float)  # 屏蔽位梯度为 0
            dQh[h] = dscores @ Kh[h] * scale
            dKh[h] = (dscores.T @ Qh[h]) * scale
            dVh[h] = attn.T @ dctx_h
        dQ = dQh.transpose(1, 0, 2).reshape(T, self.d_model)
        dK = dKh.transpose(1, 0, 2).reshape(T, self.d_model)
        dV = dVh.transpose(1, 0, 2).reshape(T, self.d_model)
        dx = self.Wq.backward(dQ) + self.Wk.backward(dK) + self.Wv.backward(dV)
        return dx

    def params(self):
        return collect_params(self.Wq, self.Wk, self.Wv, self.Wo)

    def clear_cache(self) -> None:
        """清空自注意力前向缓存（predict 路径不调用 backward，需手动清理防内存泄漏）。"""
        self._cache.clear()


class TransformerBlock:
    """Pre-残差 Transformer 块：Attn + FFN（含残差）。"""

    def __init__(self, d_model: int, n_heads: int, rng: np.random.Generator, ff_dim: int) -> None:
        self.attn = CausalSelfAttention(d_model, n_heads, rng)
        self.ffn = Sequential(
            [Linear(d_model, ff_dim, rng), ReLU(), Linear(ff_dim, d_model, rng)]
        )

    def forward(self, x: np.ndarray) -> np.ndarray:
        a = self.attn.forward(x)
        x2 = x + a
        f = self.ffn.forward(x2)
        return x2 + f

    def backward(self, dout: np.ndarray) -> np.ndarray:
        df = dout
        dx2_from_f = self.ffn.backward(df)
        dx2 = dout + dx2_from_f
        da = dx2
        dx = self.attn.backward(da)
        return dx2 + dx

    def params(self):
        return self.attn.params() + self.ffn.params()


def _make_pos_encoding(T: int, d: int) -> np.ndarray:
    pos = np.zeros((T, d))
    for t in range(T):
        for i in range(0, d, 2):
            denom = 10000 ** (i / d)
            pos[t, i] = np.sin(t / denom)
            if i + 1 < d:
                pos[t, i + 1] = np.cos(t / denom)
    return pos


@register("ar_transformer")
class ARTransformer(ForecastModel):
    """自研解码式自回归 Transformer（预测层主 fallback）。"""

    family = "ar_transformer"

    #: 由 cfg.forecast.ar 控制的关键超参（缺省均冻结为演示友好值）
    _AR_DEFAULTS = {
        "d_model": 32,
        "n_heads": 4,
        "n_layers": 2,
        "ff_dim": 64,
        "n_epochs": 40,
        "lr": 1e-3,
        "batch": 256,
    }

    def __init__(self, cfg: Any, model_id: Optional[str] = None) -> None:
        super().__init__(cfg, model_id)
        ar = getattr(cfg.forecast, "ar", None)
        fc = cfg.forecast
        # 优先 ar 子块，其次 cfg.forecast 顶层（hidden_size/n_layers/n_heads/epochs/lr）
        self.d_model = int(getattr(ar, "d_model", getattr(fc, "hidden_size", self._AR_DEFAULTS["d_model"])))
        self.n_heads = int(getattr(ar, "n_heads", getattr(fc, "n_heads", self._AR_DEFAULTS["n_heads"])))
        self.n_layers = int(getattr(ar, "n_layers", getattr(fc, "n_layers", self._AR_DEFAULTS["n_layers"])))
        self.ff_dim = int(getattr(ar, "ff_dim", self._AR_DEFAULTS["ff_dim"]))
        self.n_epochs = int(getattr(ar, "n_epochs", getattr(fc, "epochs", self._AR_DEFAULTS["n_epochs"])))
        self.lr = float(getattr(ar, "lr", getattr(fc, "lr", self._AR_DEFAULTS["lr"])))
        self.batch = int(getattr(ar, "batch", self._AR_DEFAULTS["batch"]))
        self.n_features = 0
        self._rng = np.random.default_rng(int(getattr(fc, "seed", 42)))
        self._pos: Optional[np.ndarray] = None
        self.optimizer = None  # type: ignore[assignment]

    # ---- 网络构建（fit 时已知特征维度） ----
    def _build(self, n_features: int) -> None:
        self.n_features = n_features
        rng = self._rng
        self.encoder = Linear(n_features, self.d_model, rng)
        self.blocks = [
            TransformerBlock(self.d_model, self.n_heads, rng, self.ff_dim)
            for _ in range(self.n_layers)
        ]
        self.head = Linear(self.d_model, 2, rng)
        self.optimizer = Adam(
            collect_params(self.encoder, *self.blocks, self.head), lr=self.lr
        )
        self._pos = _make_pos_encoding(self.lookback, self.d_model)

    # ---- train_gaussian 接口 ----
    def forward_windows(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._pos is None:  # 尚未构建
            self._build(windows.shape[1] // self.lookback)
        n = windows.shape[0]
        means = np.zeros(n)
        log_stds = np.zeros(n)
        for i in range(n):
            x = windows[i].reshape(self.lookback, self.n_features)
            h = self.encoder.forward(x)
            h = h + self._pos
            for blk in self.blocks:
                h = blk.forward(h)
            out = self.head.forward(h[-1])
            means[i] = out[0]
            log_stds[i] = float(np.clip(out[1], -3.0, 1.0))
        return means, log_stds

    def backward_windows(self, dmean: np.ndarray, dlog_std: np.ndarray) -> None:
        n = len(dmean)
        for i in range(n):
            dout = np.array([dmean[i], dlog_std[i]])
            dh_last = self.head.backward(dout)
            dh = np.zeros((self.lookback, self.d_model))
            dh[-1] = dh_last
            for blk in reversed(self.blocks):
                dh = blk.backward(dh)
            self.encoder.backward(dh)

    # ---- ForecastModel 接口 ----
    def fit(self, X: pd.DataFrame, y: np.ndarray, X_valid: Optional[pd.DataFrame] = None) -> Any:
        from .base import build_windows

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            raise ValueError("ARTransformer: 无可用训练窗口（特征样本不足 lookback）。")
        self._build(windows.shape[1] // self.lookback)
        self.train_end = pd.Timestamp(valid_idx.get_level_values(1).max())
        # 仅用 train_end 之前的有效性信号指纹
        loss = train_gaussian(
            self, windows, y, n_epochs=self.n_epochs, lr=self.lr, batch=self.batch
        )
        self._params = {
            "d_model": self.d_model,
            "n_params": sum(p.data.size for p in self.optimizer.params),
            "loss": float(loss),
        }
        return self._make_log(loss)

    def predict(self, X: pd.DataFrame) -> list:
        from .base import build_windows

        windows, valid_idx = build_windows(X, self.lookback)
        if windows.shape[0] == 0:
            return []
        means, stds = self._next_return(windows)
        paths = self.sample_paths(windows)
        return self._signals_from_paths(paths, valid_idx)

    def _next_return(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        means, log_stds = self.forward_windows(windows)
        return means, np.exp(log_stds)

    def _make_log(self, loss: float):
        from .base import TrainLog

        return TrainLog(
            loss=float(loss),
            n_epochs=self.n_epochs,
            n_params=int(sum(p.data.size for p in self.optimizer.params)),
        )

    # ---- 持久化（保存解析出的参数） ----
    def save(self, path: str | Any) -> None:
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
            "arch": {
                "d_model": self.d_model,
                "n_heads": self.n_heads,
                "n_layers": self.n_layers,
                "ff_dim": self.ff_dim,
                "n_features": self.n_features,
            },
            "weights": {
                "encoder": [p.data.copy() for p in self.encoder.params()],
                "head": [p.data.copy() for p in self.head.params()],
                "blocks": [
                    [p.data.copy() for p in blk.params()] for blk in self.blocks
                ],
            },
        }
        with open(path, "wb") as f:
            pickle.dump(blob, f)
