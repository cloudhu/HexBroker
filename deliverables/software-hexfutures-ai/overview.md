# HexFutures-AI 交付总览（overview）

## 做了什么
面向**中国商品期货高胜率预测 + RL 双层 + 自回归进化**的研究框架（Python 包 `hexbroker/`）：
调研了 GitHub 14+ 开源 AI 期货交易模型（重点 kronos），取长补短形成"特性拼图"，
经架构师综合设计后实现完整可运行 MVP，71 项测试全绿，一条命令端到端出报告。

## 关键决策
- **预测底座**：finetune Kronos-small（主）+ 自研 ARTransformer（CPU fallback）+ TCN/GRU/LightGBM（基线）
- **RL 层**：SB3 PPO/SAC + 自建 Gymnasium 环境（风控 in-the-loop）；CPU 沙箱用纯 numpy PPO
- **进化**：Optuna（TPE/NSGA-II/Hyperband）+ EXAMM 风格神经进化（5 种记忆单元）+ PSI 漂移检测
- **防泄漏**：预测层 walk-forward 产出 OOS 信号入 SignalStore；RL 环境只读 SignalStore、构造禁收模型
- **风控**：复用用户交易系统 v4.0（ATR 三档只增不减、S1–S5、风险预算、R1–R4 恢复）
- **验收**：Q5 双闸门（闸门1 信号有效性 / 闸门2 含成本可交付性）+ DSR/PBO 诊断；弱信号带 54–58% 保守处置

## 验证结果
- `pytest -q` → **71 passed**
- `python -m hexbroker.pipeline --demo` → 合成数据 2.9 分钟跑通，报告落盘 `artifacts/reports/`
- Demo 结论（诚实）：闸门1 PASS（信号非噪声）、闸门2 FAIL（RL 尚未收敛到优于阈值，如实诊断）

## 后续
真实数据（天勤/AkShare）复跑 → RL 训练调优 → Kronos 微调消融 → 实盘走受控 CTP 骨架（需穿透式监管报备）。
