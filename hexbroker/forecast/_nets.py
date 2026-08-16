"""轻量纯 numpy 网络原语（CPU 友好，无 torch 依赖）。

提供 ``Linear`` / ``CausalConv1D`` / ``GRU`` 等可训练层与 ``Adam`` 优化器，
供 ARTransformer / TCN / GRU 基线共用。所有层实现前向 + 反向（解析梯度），
避免手工数值微分。仅用于 MVP 级小网络（参数量 < 1M）。

**缓存约定（支持批处理前向-后向分离）**：每个有状态的层在 ``forward`` 时把
样本级缓存追加进 ``self._cache``（FIFO 列表），``backward`` 时按入队顺序 ``pop(0)``
取出。这样 ``train_gaussian`` 可以先对整批样本做 ``forward_windows``，再统一
``backward_windows``，梯度在各层以 ``+=`` 方式跨样本累积。
"""

from __future__ import annotations

import numpy as np


# --------------------------- 激活层 ---------------------------
def relu_forward(x: np.ndarray) -> np.ndarray:
    return np.maximum(x, 0.0)


def relu_backward(x: np.ndarray, dout: np.ndarray) -> np.ndarray:
    return dout * (x > 0).astype(float)


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)


# --------------------------- 参数容器 ---------------------------
class Param:
    """一个可训练参数（ndarray）及其梯度。"""

    def __init__(self, data: np.ndarray) -> None:
        self.data = np.asarray(data, dtype=float)
        self.grad = np.zeros_like(self.data)

    def zero_grad(self) -> None:
        self.grad = np.zeros_like(self.data)


# --------------------------- 线性层 ---------------------------
class Linear:
    def __init__(self, in_f: int, out_f: int, rng: np.random.Generator, scale: float = 0.1) -> None:
        self.W = Param(rng.normal(0, scale, (in_f, out_f)))
        self.b = Param(np.zeros(out_f))
        self._cache: list = []

    def forward(self, x: np.ndarray) -> np.ndarray:
        self._cache.append(x)
        return x @ self.W.data + self.b.data

    def backward(self, dout: np.ndarray) -> np.ndarray:
        x = self._cache.pop(0)
        xr = x.reshape(-1, x.shape[-1])
        dr = dout.reshape(-1, dout.shape[-1])
        self.W.grad += xr.T @ dr
        self.b.grad += dr.sum(axis=0)
        gout = dr @ self.W.data.T  # (N, in_f) 或 (1, in_f)
        # 保持与输入 x 相同的维度（1D 输入返回 1D，便于链式反向）
        if x.ndim == 1:
            return gout.reshape(-1)
        return gout

    def params(self):
        return [self.W, self.b]

    def clear_cache(self) -> None:
        """清空前向缓存（predict 路径不调用 backward，需手动清理防内存泄漏）。"""
        self._cache.clear()


# --------------------------- 因果 1D 卷积（仅含线性，梯度解析） ---------------------------
class CausalConv1D:
    """一维因果卷积：输入 (T, C_in) -> (T, C_out)，左侧 zero-pad。

    卷积对权重是线性的，梯度解析可得。
    """

    def __init__(self, c_in: int, c_out: int, kernel: int, rng: np.random.Generator, scale: float = 0.1) -> None:
        self.kernel = kernel
        self.c_in = c_in
        self.c_out = c_out
        self.W = Param(rng.normal(0, scale, (c_out, c_in, kernel)))
        self.b = Param(np.zeros(c_out))
        self._cache: list = []

    def forward(self, x: np.ndarray) -> np.ndarray:
        T = x.shape[0]
        pad = self.kernel - 1
        xp = np.concatenate([np.zeros((pad, self.c_in)), x], axis=0)  # (T+pad, C_in)
        out = np.zeros((T, self.c_out))
        for k in range(self.kernel):
            win = xp[k : k + T]  # (T, C_in)
            out += win @ self.W.data[:, :, k].T  # (T, C_out)
        out += self.b.data
        self._cache.append((x, xp))
        return out

    def backward(self, dout: np.ndarray) -> np.ndarray:
        x, xp = self._cache.pop(0)
        T = x.shape[0]
        pad = self.kernel - 1
        dxp = np.zeros_like(xp)
        for k in range(self.kernel):
            win = xp[k : k + T]  # (T, C_in)
            self.W.grad[:, :, k] += win.T @ dout
            self.W.grad[:, :, k] = self.W.grad[:, :, k].T
            dxp[k : k + T] += dout @ self.W.data[:, :, k]  # (T, C_in)
        self.b.grad += dout.sum(axis=0)
        return dxp[pad:]

    def params(self):
        return [self.W, self.b]

    def clear_cache(self) -> None:
        """清空前向缓存（predict 路径不调用 backward，需手动清理防内存泄漏）。"""
        self._cache.clear()


# --------------------------- GRU 单元（手写 BPTT，简化门控） ---------------------------
class GRU:
    """单层 GRU（minibatch 友好）。

    实现：``zr = sigmoid([x;h] @ Wzr + bzr)``，``z,r = split(zr)``，
    ``n = tanh([x; r*h] @ Wn + bn)``，``h = (1-z)*n + z*h``。
    手写 BPTT 反向，形状严格对齐。每个样本成组入队/出队。
    """

    def __init__(self, input_size: int, hidden: int, rng: np.random.Generator, scale: float = 0.1) -> None:
        self.input_size = input_size
        self.hidden = hidden
        self.W_zr = Param(rng.normal(0, scale, (input_size + hidden, 2 * hidden)))
        self.b_zr = Param(np.zeros(2 * hidden))
        self.W_n = Param(rng.normal(0, scale, (input_size + hidden, hidden)))
        self.b_n = Param(np.zeros(hidden))
        self._samples: list = []

    def forward(self, x: np.ndarray, h0: np.ndarray | None = None) -> np.ndarray:
        T = x.shape[0]
        h = np.zeros(self.hidden)
        if h0 is not None:
            h = h0.copy()
        cache: list = []
        last = h
        for t in range(T):
            xt = x[t]
            cat = np.concatenate([xt, h])
            zr = sigmoid(cat @ self.W_zr.data + self.b_zr.data)
            z, r = zr[: self.hidden], zr[self.hidden :]
            catn = np.concatenate([xt, r * h])
            n = tanh(catn @ self.W_n.data + self.b_n.data)
            h = (1 - z) * n + z * h
            cache.append((xt, h.copy(), r, z, n, cat, catn, zr))
            last = h
        self._samples.append((T, cache))
        return last

    def backward(self, dlast: np.ndarray) -> np.ndarray:
        T, cache = self._samples.pop(0)
        H = self.hidden
        I = self.input_size
        dx = np.zeros((T, I))
        dh_next = np.zeros(H)
        for t in reversed(range(T)):
            xt, h_prev, r, z, n, cat, catn, zr = cache[t]
            dh_total = dlast + dh_next
            # h = (1-z)*n + z*h_prev
            dn = dh_total * (1 - z)
            dz = dh_total * (h_prev - n)
            # 候选 n = tanh(catn @ Wn + bn) → 对 catn 的梯度需经 Wn.T 传回
            dpre = dn * (1 - n ** 2)  # (hidden,)
            self.W_n.grad += np.outer(catn, dpre)
            self.b_n.grad += dpre
            dcatn_full = dpre @ self.W_n.data.T  # (input_size+hidden,)
            dx[t] += dcatn_full[:I]
            drh = dcatn_full[I:]  # 对 (r*h_prev) 的梯度
            dr = drh * h_prev
            dh_prev = drh * r
            # z 门（sigmoid）—— zr 维度为 2*hidden，需拼接 dz/dr 后再求 sigmoid 梯度
            dzr = np.concatenate([dz, dr])  # (2*hidden,)
            dsig_z = dzr * zr * (1 - zr)  # zr 为 sigmoid 输出（2*hidden,）
            self.W_zr.grad += np.outer(cat, dsig_z)
            self.b_zr.grad += dsig_z
            dx[t] += dsig_z[:I]
            dh_prev += dsig_z[I:]
            dh_prev += z * dh_total  # z*h_prev 项对 h_prev 的梯度
            dh_next = dh_prev
        return dx

    def params(self):
        return [self.W_zr, self.b_zr, self.W_n, self.b_n]

    def clear_cache(self) -> None:
        """清空 GRU 前向序列缓存（predict 路径不调用 backward，需手动清理防内存泄漏）。"""
        self._samples.clear()


# --------------------------- Adam 优化器 ---------------------------
class Adam:
    def __init__(self, params, lr: float = 1e-3, b1: float = 0.9, b2: float = 0.999, eps: float = 1e-8) -> None:
        self.params = params
        self.lr = lr
        self.b1 = b1
        self.b2 = b2
        self.eps = eps
        self.t = 0
        self.m = [np.zeros_like(p.data) for p in params]
        self.v = [np.zeros_like(p.data) for p in params]

    def step(self) -> None:
        self.t += 1
        for i, p in enumerate(self.params):
            g = np.clip(p.grad, -5.0, 5.0)
            self.m[i] = self.b1 * self.m[i] + (1 - self.b1) * g
            self.v[i] = self.b2 * self.v[i] + (1 - self.b2) * (g ** 2)
            mhat = self.m[i] / (1 - self.b1 ** self.t)
            vhat = self.v[i] / (1 - self.b2 ** self.t)
            p.data -= self.lr * mhat / (np.sqrt(vhat) + self.eps)

    def zero_grad(self) -> None:
        for p in self.params:
            p.zero_grad()


def collect_params(*modules):
    ps = []
    for m in modules:
        ps.extend(m.params())
    return ps


# --------------------------- ReLU 层与 Sequential 容器 ---------------------------
class ReLU:
    def __init__(self) -> None:
        self._cache: list = []

    def forward(self, x: np.ndarray) -> np.ndarray:
        self._cache.append(x)
        return relu_forward(x)

    def backward(self, dout: np.ndarray) -> np.ndarray:
        x = self._cache.pop(0)
        return relu_backward(x, dout)

    def params(self):
        return []

    def clear_cache(self) -> None:
        """清空前向缓存（predict 路径不调用 backward，需手动清理防内存泄漏）。"""
        self._cache.clear()


class Sequential:
    """顺序容器（仅 Linear / ReLU 等具备 forward/backward 的层）。"""

    def __init__(self, layers: list) -> None:
        self.layers = layers

    def forward(self, x: np.ndarray) -> np.ndarray:
        out = x
        for l in self.layers:
            out = l.forward(out)
        return out

    def backward(self, dout: np.ndarray) -> np.ndarray:
        for l in reversed(self.layers):
            dout = l.backward(dout)
        return dout

    def params(self):
        ps: list = []
        for l in self.layers:
            ps.extend(l.params())
        return ps

    def clear_cache(self) -> None:
        """清空容器内各层的前向缓存。"""
        for l in self.layers:
            if hasattr(l, "clear_cache"):
                l.clear_cache()


# --------------------------- 高斯损失辅助 ---------------------------
def gaussian_loss(mean: np.ndarray, log_std: np.ndarray, y: np.ndarray) -> float:
    """Gaussian NLL（均值化）。"""
    var = np.exp(2.0 * log_std) + 1e-6
    return float(0.5 * np.mean(np.log(var) + (y - mean) ** 2 / var))


def gaussian_loss_grad(mean: np.ndarray, log_std: np.ndarray, y: np.ndarray):
    """返回 (dmean, dlog_std)。"""
    var = np.exp(2.0 * log_std) + 1e-6
    dmean = -(y - mean) / var
    dlog_std = 1.0 - (y - mean) ** 2 / var
    return dmean, dlog_std


def train_gaussian(model, windows: np.ndarray, y: np.ndarray, n_epochs: int, lr: float, batch: int = 256) -> float:
    """通用高斯回归训练：model 需实现 forward_windows/backward_windows/params/optimizer。"""
    n = windows.shape[0]
    best = float("inf")
    for epoch in range(n_epochs):
        perm = np.random.default_rng(epoch).permutation(n)
        for s in range(0, max(n, 1), batch):
            idx = perm[s : s + batch]
            if len(idx) == 0:
                continue
            model.optimizer.zero_grad()
            mean, log_std = model.forward_windows(windows[idx])
            loss = gaussian_loss(mean, log_std, y[idx])
            dmean, dlog_std = gaussian_loss_grad(mean, log_std, y[idx])
            model.backward_windows(dmean, dlog_std)
            model.optimizer.step()
        if epoch % 5 == 0:
            m2, l2 = model.forward_windows(windows)
            best = min(best, gaussian_loss(m2, l2, y))
    return best
