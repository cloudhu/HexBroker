"""EXAMM 风格神经进化（§3.7 / §8.7 外层 C，R6）。

规范来源：travisdesell/exact（EXAMM/EXALT，GECCO'19/EvoApps'19 同行评审），
Windows 宿主下以轻量 Python 自研实现，仅作为**算子设计对照**，不引入 C++ 依赖。

设计要点：
- 记忆单元覆盖 ≥3 种：``delta_rnn`` / ``gru`` / ``lstm`` / ``mgu`` / ``ugrnn``（对应规范仓库 UGRNN/MGU/GRU/Delta-RNN/LSTM）。
- 岛屿模型（islands）+ 锦标赛选择 + 变异（权重扰动/结构变异/单元类型切换）+ 交叉 + 岛屿迁移。
- **Kronos 主干全程冻结，仅基线小网络参与进化**；参数上限 ``max_params``（默认 <1M），
  进化过程动态剪枝超出规模的结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# 记忆单元（全部 numpy 前向）
# ---------------------------------------------------------------------------
def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _init_rnn_params(cell: str, in_dim: int, h: int, rng: np.random.Generator, scale: float = 0.15) -> dict:
    """按单元类型初始化权重。"""
    p: dict[str, np.ndarray] = {}
    if cell == "delta_rnn":
        p["Wxh"] = rng.normal(0, scale, (in_dim, h))
        p["Whh"] = rng.normal(0, scale, (h, h))
        p["b"] = np.zeros(h)
        p["alpha"] = np.full(h, 0.9)  # 泄漏系数
    elif cell == "gru":
        p["Wz"] = rng.normal(0, scale, (in_dim, h))
        p["Uz"] = rng.normal(0, scale, (h, h))
        p["bz"] = np.zeros(h)
        p["Wr"] = rng.normal(0, scale, (in_dim, h))
        p["Ur"] = rng.normal(0, scale, (h, h))
        p["br"] = np.zeros(h)
        p["Wh"] = rng.normal(0, scale, (in_dim, h))
        p["Uh"] = rng.normal(0, scale, (h, h))
        p["bh"] = np.zeros(h)
    elif cell == "lstm":
        p["Wf"] = rng.normal(0, scale, (in_dim, h))
        p["Uf"] = rng.normal(0, scale, (h, h))
        p["bf"] = np.ones(h)
        p["Wi"] = rng.normal(0, scale, (in_dim, h))
        p["Ui"] = rng.normal(0, scale, (h, h))
        p["bi"] = np.zeros(h)
        p["Wo"] = rng.normal(0, scale, (in_dim, h))
        p["Uo"] = rng.normal(0, scale, (h, h))
        p["bo"] = np.zeros(h)
        p["Wc"] = rng.normal(0, scale, (in_dim, h))
        p["Uc"] = rng.normal(0, scale, (h, h))
        p["bc"] = np.zeros(h)
    elif cell == "mgu":
        p["Wx"] = rng.normal(0, scale, (in_dim, h))
        p["Wh"] = rng.normal(0, scale, (h, h))
        p["bx"] = np.zeros(h)
        p["Wf"] = rng.normal(0, scale, (in_dim, h))
        p["Uf"] = rng.normal(0, scale, (h, h))
        p["bf"] = np.zeros(h)
    elif cell == "ugrnn":
        p["Wx"] = rng.normal(0, scale, (in_dim, h))
        p["Wh"] = rng.normal(0, scale, (h, h))
        p["bx"] = np.zeros(h)
        p["Wg"] = rng.normal(0, scale, (in_dim, h))
        p["Ug"] = rng.normal(0, scale, (h, h))
        p["bg"] = np.zeros(h)
    else:
        raise ValueError(f"未知单元：{cell}")
    return p


def _cell_forward(cell: str, p: dict, x_seq: np.ndarray, h0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """x_seq: (T, in_dim)，返回 (outputs, last_hidden)。"""
    T, _ = x_seq.shape
    h = h0
    c = np.zeros_like(h)  # 仅 LSTM 使用：跨时间步携带单元状态（记忆）
    outs = []
    for t in range(T):
        x = x_seq[t]
        if cell == "delta_rnn":
            dh = np.tanh(x @ p["Wxh"] + h @ p["Whh"] + p["b"])
            h = p["alpha"] * h + (1 - p["alpha"]) * dh
        elif cell == "gru":
            z = _sig(x @ p["Wz"] + h @ p["Uz"] + p["bz"])
            r = _sig(x @ p["Wr"] + h @ p["Ur"] + p["br"])
            hh = np.tanh(x @ p["Wh"] + (r * h) @ p["Uh"] + p["bh"])
            h = (1 - z) * h + z * hh
        elif cell == "lstm":
            # V1 修复：c 在循环外初始化并跨步携带；原实现每步 `c = np.zeros_like(h)`
            # 重置 → 单元状态记忆被抹除 → LSTM 退化为无记忆门控（等价于仅 i*cc）。
            f = _sig(x @ p["Wf"] + h @ p["Uf"] + p["bf"])
            i = _sig(x @ p["Wi"] + h @ p["Ui"] + p["bi"])
            o = _sig(x @ p["Wo"] + h @ p["Uo"] + p["bo"])
            cc = np.tanh(x @ p["Wc"] + h @ p["Uc"] + p["bc"])
            c = f * c + i * cc
            h = o * np.tanh(c)
        elif cell == "mgu":
            f = _sig(x @ p["Wf"] + h @ p["Uf"] + p["bf"])
            hh = np.tanh(x @ p["Wx"] + (f * h) @ p["Wh"] + p["bx"])
            h = (1 - f) * h + f * hh
        elif cell == "ugrnn":
            g = _sig(x @ p["Wg"] + h @ p["Ug"] + p["bg"])
            hh = np.tanh(x @ p["Wx"] + (g * h) @ p["Wh"] + p["bx"])
            h = (1 - g) * h + g * hh
        outs.append(h)
    return np.stack(outs), h


def _sig(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


# ---------------------------------------------------------------------------
# 基因组
# ---------------------------------------------------------------------------
@dataclass
class RNNGenome:
    """RNN 基因组（结构 + 权重）。"""

    cell: str
    in_dim: int
    hidden: int
    out_dim: int
    params: dict = field(default_factory=dict)
    fitness: float = float("nan")
    island: int = 0

    def init(self, rng: np.random.Generator, scale: float = 0.15) -> "RNNGenome":
        self.params = _init_rnn_params(self.cell, self.in_dim, self.hidden, rng, scale)
        self.params["Who"] = rng.normal(0, scale, (self.hidden, self.out_dim))
        self.params["bo"] = np.zeros(self.out_dim)
        return self

    def n_params(self) -> int:
        return int(sum(p.size for p in self.params.values()))

    def forward(self, x_seq: np.ndarray) -> np.ndarray:
        """x_seq: (T, in_dim) → 输出 (out_dim,)（取最后时间步经输出头）。"""
        h0 = np.zeros(self.hidden)
        _, h = _cell_forward(self.cell, self.params, x_seq, h0)
        return h @ self.params["Who"] + self.params["bo"]

    def clone(self) -> "RNNGenome":
        g = RNNGenome(
            cell=self.cell, in_dim=self.in_dim, hidden=self.hidden, out_dim=self.out_dim,
            params={k: v.copy() for k, v in self.params.items()},
            fitness=self.fitness, island=self.island,
        )
        return g


CELLS = ["delta_rnn", "gru", "lstm", "mgu", "ugrnn"]


# ---------------------------------------------------------------------------
# 进化引擎
# ---------------------------------------------------------------------------
def _fitness_fn(genome: RNNGenome, X: np.ndarray, y: np.ndarray) -> float:
    """X: (n, T, in_dim)，y: (n,) 回归目标。fitness = -MSE。"""
    preds = np.array([genome.forward(x) for x in X])[:, 0]
    return -float(np.mean((preds - y) ** 2))


def _mutate(g: RNNGenome, rng: np.random.Generator, noise: float = 0.15) -> RNNGenome:
    """变异：权重扰动 + 概率结构变异（换单元 / 扩缩隐层）。"""
    ng = g.clone()
    for k in ng.params:
        ng.params[k] = ng.params[k] + rng.normal(0, noise, ng.params[k].shape)
    r = rng.random()
    if r < 0.15 and ng.hidden < 16:
        ng.hidden += 1
        ng = _rebuild(ng, rng)
    elif r < 0.30 and ng.hidden > 2:
        ng.hidden -= 1
        ng = _rebuild(ng, rng)
    elif r < 0.45:
        ng.cell = CELLS[rng.integers(len(CELLS))]
        ng = _rebuild(ng, rng)
    return ng


def _rebuild(g: RNNGenome, rng: np.random.Generator) -> RNNGenome:
    """按当前结构重建权重（换单元/变隐层后使用）。"""
    g.params = _init_rnn_params(g.cell, g.in_dim, g.hidden, rng)
    g.params["Who"] = rng.normal(0, 0.15, (g.hidden, g.out_dim))
    g.params["bo"] = np.zeros(g.out_dim)
    return g


def _crossover(a: RNNGenome, b: RNNGenome, rng: np.random.Generator) -> RNNGenome:
    """交叉：结构取 fitness 高者，权重逐参数按 50% 概率互换（掩码混合）。"""
    parent = a if a.fitness >= b.fitness else b
    child = parent.clone()
    for k in parent.params:
        if k not in b.params or parent.params[k].shape != b.params[k].shape:
            continue  # 结构不一致的权重块不交叉（保留 parent 的）
        mask = rng.random(parent.params[k].shape) < 0.5
        child.params[k] = np.where(mask, b.params[k], parent.params[k])
    return child


@dataclass
class EvolutionResult:
    """进化结果。"""

    best: Optional[RNNGenome] = None
    best_fitness: float = float("-inf")
    n_generations: int = 0
    n_params: int = 0
    history: list = field(default_factory=list)
    island_migrations: int = 0


def evolve(
    X: np.ndarray,
    y: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    n_generations: int = 6,
    n_islands: int = 4,
    pop_size: int = 8,
    in_dim: int = 3,
    out_dim: int = 1,
    max_params: int = 1_000_000,
    seed: int = 42,
) -> EvolutionResult:
    """EXAMM 风格岛屿神经进化。

    返回 ``EvolutionResult``（best 基因组、fitness 历史、参数数、迁移次数）。
    ``n_params`` 保证 < max_params。
    """
    rng = np.random.default_rng(seed)
    islands: list[list[RNNGenome]] = []
    for i in range(n_islands):
        pop = []
        for _ in range(pop_size):
            g = RNNGenome(
                cell=str(CELLS[i % len(CELLS)]),
                in_dim=in_dim, hidden=int(rng.integers(4, 10)), out_dim=out_dim,
            ).init(rng)
            g.fitness = _fitness_fn(g, X, y)
            g.island = i
            pop.append(g)
        islands.append(pop)

    result = EvolutionResult()
    best_global: Optional[RNNGenome] = None
    best_global_val = float("-inf")  # V1：全局最优以验证集适应度裁决

    for gen in range(n_generations):
        for i, pop in enumerate(islands):
            pop.sort(key=lambda g: g.fitness, reverse=True)
            new_pop = [pop[0].clone()]  # 精英保留
            while len(new_pop) < pop_size:
                a = pop[int(rng.integers(min(3, len(pop))))]
                b = pop[int(rng.integers(min(3, len(pop))))]
                if rng.random() < 0.6:
                    child = _crossover(a, b, rng)
                else:
                    child = _mutate(a, rng)
                child.island = i
                if child.n_params() > max_params:
                    child = a.clone()  # 超限回退
                child.fitness = _fitness_fn(child, X, y)  # 训练集适应度（岛内锦标赛选种用）
                new_pop.append(child)
            islands[i] = new_pop
            local_best = islands[i][0]
            # V1 修复：全局最优以验证集适应度裁决（防训练集过拟合选种），
            # 不再用训练集 fitness 直接选 best_global。
            local_best_val = _fitness_fn(local_best, X_val, y_val)
            if local_best_val > best_global_val:
                best_global = local_best.clone()
                best_global_val = local_best_val
        # 岛屿迁移：每代把最佳个体复制到邻岛（替换最差）
        for i in range(n_islands):
            donor = islands[i][0]
            nbr = (i + 1) % n_islands
            if rng.random() < 0.5:
                islands[nbr][-1] = donor.clone()
                islands[nbr][-1].island = nbr
                result.island_migrations += 1
        # 验证集评估历史（按验证集适应度）
        if best_global is not None:
            result.history.append(best_global_val)

    if best_global is not None:
        best_global.fitness = best_global_val  # 已是验证集适应度
        result.best = best_global
        result.best_fitness = float(best_global.fitness)
        result.n_params = best_global.n_params()
    result.n_generations = n_generations
    if result.n_params > max_params:
        raise ValueError(f"进化结果参数数 {result.n_params} 超过上限 {max_params}")
    return result
