# HexFutures-AI 交付报告（2026-08-15）

> 团队：software-hexfutures-ai ｜ 主理人：齐活林 ｜ 模式：标准 SOP（工程师因 429 限流中断后按用户既定容错切降级模式，主理人直接实施+自验）

## TL;DR
交付了面向中国商品期货的「预测 + RL 双层 + 自回归进化」研究框架 **HexFutures-AI**（Python 包 `hexbroker/`，62+ 源文件，71 项测试全绿），端到端一条命令出研究报告（Q5 双闸门 + DSR/PBO 诊断）。

## 交付概览
- ✅ **T01 数据/基础设施**：AkShare/天勤/CSV/合成四数据源、换月复权、walk-forward splitter（防泄漏红线单测）、Parquet 本地湖、OmegaConf+Pydantic 配置
- ✅ **T02 特征+预测**：Kronos Adapter（git submodule + 优雅降级 ARTransformer）、TCN/GRU/LightGBM 基线、`sample_paths` 独立路径采样、SignalStore OOS 落盘、校准
- ✅ **T03 风控+回测**：RiskManager（硬止损>S1–S5>预算>R1–R4>RL 意图；ATR 三档只增不减，复用 v4.0）、SimBroker/CostModel/BacktestEngine（训练-回测一致性 <1e-6 单测）
- ✅ **T04 RL 决策层**：FuturesTradingEnv（**只读 SignalStore、构造禁收 ForecastModel**，静态检查单测）、纯 numpy PPO（CPU 可跑）+ SB3 可选适配、风控 in-the-loop
- ✅ **T05 进化+编排**：Optuna（TPE/NSGA-II/Hyperband，sqlite 断点续跑）、EXAMM 风格神经进化（5 种记忆单元、岛屿迁移、<1M 参数）、PSI 漂移检测、`python -m hexbroker.pipeline --demo` 端到端、受控 CTP 实盘骨架（`--i-understand-the-risk` 守卫）
- ✅ **评估口径**：闸门1（信号有效性）/闸门2（含成本可交付性）+ 弱信号带处置 + DSR/PBO 诊断；报告强制并列 方向准确率/交易胜率/盈亏比/最大回撤

## 测试与验证
- `pytest -q`：**71 passed**（防泄漏、风控优先级链、成本模型 <1e-8、训练-回测一致性 <1e-6、RL 奖励上升、进化、漂移、管线 e2e、实盘守卫、Kronos 配对）
- `python -m hexbroker.pipeline --demo`：合成数据端到端 **2.9 分钟**跑通并落盘报告（≤5 分钟验收达标）

## Demo 报告要点（合成数据，ARTransformer 路径）
- 闸门1：方向准确率 **76.8%** / 有效信号准确率 78.7%（coverage 93.5%）/ RankIC 0.56 / 校准误差 0.004 → **PASS**（仅证明信号非噪声）
- 基线：buy_hold Sharpe 3.58（合成数据含可学习趋势）、dual_ma/macd 高杠杆下回撤大
- RL：Sharpe -0.79（训练未收敛到优于阈值 → **如实出诊断而非放行**，符合 T04 验收条款）
- 闸门2：**FAIL（不可交付）** → 报告如实标注，待真实数据+更多训练后复评
- 进化层：漂移 6 条全触发；Optuna 预测/RL 与 EXAMM 均已跑出结果

## 文件清单
| 类型 | 路径 |
|------|------|
| 主包 | `hexbroker/`（data/feature/forecast/risk/backtest/rl/evolution/live/evaluation/pipeline.py） |
| 配置 | `configs/`（base + 各层默认 + `experiment/e01_cu_daily.yaml` 示例） |
| 测试 | `tests/`（17 个测试文件，71 用例） |
| 脚本 | `scripts/`（run_forecast / fetch_data / make_sample_data） |
| 文档 | `docs/system_design.md`、`README.md`、`deliverables/software-hexfutures-ai/research-report.md`、`kronos-api-reference.md` |
| Demo 报告 | `artifacts/reports/report_demo_*.md/.json` |

## 下一步建议
1. **换真实数据复跑**：接入天勤 TQSDK（免费版日线）或 AkShare 拉取 cu/rb/sc 日线，跑 `scripts/run_forecast` 验证 OOS 信号质量与 R7（Kronos vs LightGBM）。
2. **RL 训练调优**：`configs/rl/ppo_default.yaml` 上调 `total_timesteps`（10 万+）、降低 `w_dd` 初始值、开 `use_sb3: true`（装 stable-baselines3）对比；达标判据是 RL 优于纯信号阈值（Calmar/MaxDD）。
3. **Kronos 微调**：`git submodule update --init third_party/Kronos`，下载权重到 `third_party/weights/`，用 `finetune_csv` 路径微调 Kronos-small，跑零样本 vs 微调 RankIC 消融。
4. **实盘合规**：如需实盘，先向期货公司报备穿透式监管，配置 CTP 环境变量后以 `--i-understand-the-risk` 走骨架验证，严禁在骨架阶段真实下单。
5. **过拟合防线**：正式结论必须以 闸门2（含成本 Sharpe/Calmar 跑赢 4 基线 + PBO<0.5 + DSR>0）为准，54% 方向准确率仅是"非噪声"下限，不是盈利承诺。

## 风险与已知限制
- 本沙箱无 GPU/无真实期货数据，Kronos 路径与实盘均未实测（有优雅降级与受控守卫兜底）。
- RL 层在合成数据上尚未收敛到优于阈值策略（已如实报告并出诊断）；需真实数据+更大训练预算。
- 合成数据含可学习 regime，闸门1 高分不代表真实市场表现。
