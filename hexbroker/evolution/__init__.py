"""进化层（§3.7 / §8.7）：自动迭代三时间尺度。

- ``drift``：PSI 漂移检测（外层 C 触发条件，PSI>0.2 → DriftEvent）。
- ``optuna_engine``：内层 A/B 的超参/奖励权重进化（TPE + NSGA-II + Hyperband，可断点续跑）。
- ``examm_engine``：外层 C 的 EXAMM 风格神经进化（仅作用于 <1M 基线小网络，
  Kronos 主干全程冻结；算子以 travisdesell/exact 为规范来源）。
"""
