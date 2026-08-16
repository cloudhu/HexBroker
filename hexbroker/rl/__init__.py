"""RL 决策层（§3.3 / §8.6）：风控 in-the-loop 的期货交易环境 + 策略。

- ``FuturesTradingEnv``：Gymnasium 环境，**只读 SignalStore 产出的信号 DataFrame**，
  构造函数禁止接收 ForecastModel 实例（防泄漏，见 ``assert_env_has_no_model_dependency``）。
- ``agent``：纯 numpy PPO（CPU 沙箱默认），``sb3_wrap``：可选 Stable-Baselines3 适配。
- 风控（``RiskManager``）嵌入 ``step()``，RL 在风控约束下学习。
"""
