"""SENTINEL L1：IntensityRanker——自研纯 numpy 排序学习器（2026-08-17）。

设计动机（探索经验）：方向二元预测≈随机，真实 alpha 在截面强度排序。
本模块用 RankNet pairwise 排序损失直接优化"截面相对强度"（品种内 z-score
后的已实现收益排序），而非回归/分类目标。

- MLP：2 层 × hidden 神经元，tanh 激活，纯 numpy 手写梯度；
- 目标：同一交易日截面内 pairwise 排序损失（logistic）；
- 输出：截面强度分数（可替代 exp_ret 用于 top-K 做多）。
"""

from __future__ import annotations

import numpy as np


class IntensityRanker:
    """强度排序学习器（RankNet 目标，纯 numpy MLP）。"""

    def __init__(
        self,
        in_dim: int,
        hidden: int = 64,
        lr: float = 1e-3,
        epochs: int = 30,
        batch_size: int = 256,
        sigma: float = 1.0,
        seed: int = 0,
    ) -> None:
        self.in_dim = in_dim
        self.hidden = hidden
        self.lr = lr
        self.epochs = epochs
        self.batch_size = batch_size
        self.sigma = sigma  # RankNet σ（分数差缩放）
        self.rng = np.random.default_rng(seed)
        self.W1: np.ndarray | None = None
        self.b1: np.ndarray | None = None
        self.W2: np.ndarray | None = None
        self.b2: np.ndarray | None = None

    def _init_params(self) -> None:
        lim1 = np.sqrt(6.0 / (self.in_dim + self.hidden))
        self.W1 = self.rng.uniform(-lim1, lim1, (self.in_dim, self.hidden))
        self.b1 = np.zeros(self.hidden)
        lim2 = np.sqrt(6.0 / (self.hidden + 1))
        self.W2 = self.rng.uniform(-lim2, lim2, (self.hidden, 1))
        self.b2 = np.zeros(1)

    # ---- 前向 ----
    def _forward(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """X: (n, in_dim) → (scores, h1)。"""
        z1 = X @ self.W1 + self.b1
        h1 = np.tanh(z1)
        s = h1 @ self.W2 + self.b2
        return s.ravel(), h1

    def predict(self, X: np.ndarray) -> np.ndarray:
        """强度分数（标量数组，越大越强）。"""
        s, _ = self._forward(np.asarray(X, dtype=float))
        return s

    # ---- 训练（RankNet pairwise） ----
    def fit(self, X: np.ndarray, y: np.ndarray, group_idx: np.ndarray) -> "IntensityRanker":
        """训练。

        X       : (n, in_dim) 特征（已横截面标准化）
        y       : (n,) 目标（品种内 z-score 的已实现收益——截面可比）
        group_idx: (n,) 每个样本所属"截面组"（如交易日序号）；pairwise 仅组内构建
        """
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        group_idx = np.asarray(group_idx)
        self._init_params()

        # 预构建组内 pair（y_i > y_j 的有向对）
        pairs_i: list[int] = []
        pairs_j: list[int] = []
        for g in np.unique(group_idx):
            idx = np.where(group_idx == g)[0]
            if len(idx) < 2:
                continue
            # 组内按 y 排序 → 相邻对（简化 RankNet，降低复杂度）
            order = idx[np.argsort(y[idx])[::-1]]
            for k in range(len(order) - 1):
                pairs_i.append(order[k])
                pairs_j.append(order[k + 1])
        pairs_i = np.array(pairs_i, dtype=int)
        pairs_j = np.array(pairs_j, dtype=int)
        if len(pairs_i) == 0:
            return self
        n_pairs = len(pairs_i)

        # Adam 状态
        m = {k: np.zeros_like(v) for k, v in self._params().items()}
        v = {k: np.zeros_like(v) for k, v in self._params().items()}
        t = 0
        beta1, beta2, eps = 0.9, 0.999, 1e-8

        for ep in range(self.epochs):
            perm = self.rng.permutation(n_pairs)
            total_loss = 0.0
            for start in range(0, n_pairs, self.batch_size):
                ids = perm[start : start + self.batch_size]
                bi, bj = pairs_i[ids], pairs_j[ids]
                t += 1
                # 前向两批
                si, h1i = self._forward(X[bi])
                sj, h1j = self._forward(X[bj])
                o = self.sigma * (si - sj)  # y_i > y_j 期望 o 大
                p = 1.0 / (1.0 + np.exp(-o))
                loss = -np.log(p + 1e-12)
                total_loss += float(loss.sum())

                # RankNet 梯度：∂L/∂o = p - 1
                lam = self.sigma * (p - 1.0)  # (B,)
                # 分数梯度：si 方向 +lam（提 i），sj 方向 -lam（压 j）
                # 逐样本回传（简化：同一批内样本不共享）
                grads = self._grad_batch(X, bi, bj, h1i, h1j, lam)
                # Adam 更新
                for k in grads:
                    m[k] = beta1 * m[k] + (1 - beta1) * grads[k]
                    v[k] = beta2 * v[k] + (1 - beta2) * grads[k] ** 2
                    mhat = m[k] / (1 - beta1 ** t)
                    vhat = v[k] / (1 - beta2 ** t)
                    setattr(self, k, getattr(self, k) - self.lr * mhat / (np.sqrt(vhat) + eps))
            if (ep + 1) % 10 == 0:
                print(f"  [RankNet] epoch {ep+1}/{self.epochs} loss={total_loss/n_pairs:.4f}")
        return self

    def _params(self) -> dict[str, np.ndarray]:
        return {"W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2}

    def _grad_batch(
        self, X: np.ndarray, bi: np.ndarray, bj: np.ndarray,
        h1i: np.ndarray, h1j: np.ndarray, lam: np.ndarray,
    ) -> dict[str, np.ndarray]:
        """批内逐 pair 回传（O(B) 独立样本，无跨样本共享简化）。"""
        B = len(bi)
        gW1 = np.zeros_like(self.W1)
        gb1 = np.zeros_like(self.b1)
        gW2 = np.zeros_like(self.W2)
        gb2 = np.zeros_like(self.b2)
        for k in range(B):
            lk = lam[k]
            xi, xj = X[bi[k]], X[bj[k]]
            hi, hj = h1i[k], h1j[k]
            # 输出层
            gW2 += lk * (hi - hj)[:, None] @ np.ones((1, 1))  # dL/dW2 = Σ lam*hi - lam*hj
            gb2 += lk - lk  # 常数抵消（b2 梯度 = Σlam - Σlam = 0）
            # 隐层（tanh 导数）
            gi = lk * (self.W2 @ np.ones((1, 1))).ravel()  # (hidden,)
            gj = -lk * (self.W2 @ np.ones((1, 1))).ravel()
            di = gi * (1 - hi ** 2)
            dj = gj * (1 - hj ** 2)
            gW1 += np.outer(xi, di) + np.outer(xj, dj)
            gb1 += di + dj
        return {"W1": gW1 / B, "b1": gb1 / B, "W2": gW2 / B, "b2": gb2 / B}
