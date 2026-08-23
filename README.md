# HexFutures-AI

**中国商品期货高胜率预测 + RL 双层 + 自回归进化研究框架**（软件团队 software-hexfutures-ai 交付）。

基于开源生态「特性拼图」构建：Kronos（自回归基础模型，可选）+ 自研 ARTransformer（CPU 可跑 fallback）
+ Stable-Baselines3/纯 numpy PPO（RL 决策层）+ Optuna & EXAMM 风格神经进化 + 天勤 TQSDK/AkShare 数据 + vn.py 实盘骨架。

## 架构（三层协作）

```
数据层(天勤/AkShare/CSV) → 特征层(技术指标/微观结构/滚动归一化)
  → 预测层(自回归: Kronos | ARTransformer | TCN/GRU/LightGBM) —— walk-forward 产出 OOS 信号
    → SignalStore（只落 OOS 信号，RL 环境只读它——物理防泄漏）
      → RL 决策层（PPO，风控 in-the-loop）
        → 风控层（硬止损>S1–S5>预算>R1–R4>RL 意图；ATR 三档只增不减）
          → 回测/评估层（bar 级事件回测，训练-回测一致性 <1e-6）
            → 进化层（Optuna 预测/RL 超参 + EXAMM 神经进化 + PSI 漂移检测）
              → 报告（Q5 双闸门 + DSR/PBO 过拟合诊断）
```

## 安装

```bash
# 推荐：以 pyproject.toml 为单一事实源，安装核心运行依赖 + 开发依赖（pytest）
pip install -e .[dev]

# 可选：安装 RL 重依赖（torch / stable-baselines3），CPU 沙箱可省略
pip install -e .[torch]       # 或 .[sb3] 启用 SB3 适配层

# 兼容路径：仍可用 requirements.txt（17 行核心依赖与 pyproject 同步，向后兼容）
pip install -r requirements.txt
```

## 快速开始

```bash
# 端到端 demo（合成数据 + ARTransformer fallback，CPU 可跑，≤5 分钟）
python -m hexbroker.pipeline --demo --report-dir artifacts/reports

# 真实数据（需先有 CSV 或接天勤 TQSDK 账户）
python -m scripts.run_forecast --source csv --symbols SHFE.cu SHFE.rb INE.sc

# 跑全部测试
pytest -q
```

## 验收口径（防"高胜率假象"）

- **闸门1（T02 信号有效性）**：方向准确率≥54% 且 有效信号准确率≥58%(coverage≥30%) 且 跑赢 LightGBM —— 只证明"非噪声"。
- **闸门2（T05 可交付性）**：计入成本后 OOS Sharpe/Calmar 跑赢 4 基线 + PBO<0.5 + DSR>0 —— 唯一交付判据。
- **弱信号带（54–58%）**：仅作辅助信号，RL 切保守档，集成向 LightGBM/技术指标倾斜。
- 报告强制并列：方向准确率 / 交易胜率 / 盈亏比 / 最大回撤 —— 只报胜率不报盈亏比 = 无效结论。

## 关键设计（防泄漏红线）

1. **预测层属于环境的一部分**：先 walk-forward 滚动训练，OOS 信号写入 `SignalStore`（带 model_id+train_end 指纹）；
   `FuturesTradingEnv` **只读 SignalStore，构造禁止接收 ForecastModel 实例**（`test_env_no_model_dependency` 静态检查）。
2. **风控 in-the-loop**：`RiskManager` 嵌入 `env.step()`，RL 在风控约束下学习，避免训练/部署分布漂移。
3. **Kronos 配对校验**：模型↔分词器必须配对（`validate_kronos_pairing`），错配抛 `HexConfigError`。
4. **Kronos 采样口径**：`predict(sample_count=N)` 返回均值拿不到分布 → 循环 `sample_count=1` 多次收集独立路径（`sample_paths`）。
5. **实盘受控**：CTP 骨架必须 `--i-understand-the-risk` + 环境变量凭证才启动，且默认拒绝真实下单（穿透式监管报备自理）。

## 目录

```
hexbroker/            # 主包
  data/               # 数据：sources(天勤/AkShare/CSV/合成) 复权/换月/walk-forward splitter/Parquet
  feature/            # 特征：技术指标/微观结构/滚动归一化
  forecast/           # 预测：base契约/ARTransformer/KronosAdapter/基线/校准/集成/SignalStore
  risk/               # 风控：优先级链/ATR止损/预算/恢复R1-R4/S1-S5卖出引擎
  backtest/           # 回测：SimBroker/CostModel/BacktestEngine/WalkForwardBacktester
  rl/                 # RL：FuturesTradingEnv(风控in-loop)/纯numpy PPO/SB3适配
  evolution/          # 进化：Optuna(TPE/NSGA-II/Hyperband)/EXAMM风格神经进化/PSI漂移
  live/               # 实盘：CTP 受控骨架
  evaluation/         # 评估：指标/基线/DSR-PBO诊断/报告
  pipeline.py         # 端到端一条命令
configs/              # OmegaConf 实验配置（base + 各层默认 + e01_cu_daily 示例）
tests/                # 71 项测试（防泄漏/风控优先级/成本/一致性/RL/进化/漂移/管线/实盘守卫）
docs/                 # 架构设计（system_design.md 等）
```

## 许可

MIT（研究用途；实盘前请自行完成穿透式监管报备与合规审查，本框架不构成投资建议）。
