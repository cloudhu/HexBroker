# 阶段一：开源 AI 期货交易模型调研报告

> 团队：software-hexfutures-ai ｜ 执行：产品经理（许清楚，研究模式）＋ 主理人核验
> 范围锁定（用户拍板）：商品期货（日线/小时线）｜ 预测+RL 双层 ｜ 本地 GPU 可训练小模型

## 1. 开源模型清单（14 个，均经 GitHub/仓库页实测核实）

| 仓库 | 地址 | 语言 | Star | 更新 | 核心方法论 |
|------|------|------|------|------|-----------|
| Microsoft Qlib | github.com/microsoft/qlib | Python | ~47.4k | 2025.08(v0.9.7) | AI 量化研究平台：特征工程 DSL + 模型动物园 + walk-forward 回测 + RD-Agent 自动研究循环 |
| VeighNa (vn.py) | github.com/vnpy/vnpy | Python | ~44.5k | 活跃 | 国内最主流开源期货交易平台：CTP 柜台接入、Tick/Bar 回测、TQSDK 数据源 |
| Kronos | github.com/shiyu-coder/Kronos | Python | ~37.1k | 2025.11 | 金融 K 线基础大模型：解码器自回归 Transformer + 金融分词器，零样本预测 |
| backtrader | github.com/mementum/backtrader | Python | ~22.8k | 维护放缓 | 规则型回测引擎（非 RL-native） |
| FinRL | github.com/AI4Finance-Foundation/FinRL | Python | ~16k | 活跃 | 金融 RL 完整管线：PPO/DQN/SAC/A2C，配 FinRL-Meta 多市场数据集 |
| TensorTrade | github.com/tensortrade-org/tensortrade | Python | ~6.7k | 停滞 | Gym-native RL 交易环境（Apache-2.0），社区 fork 为 TensorTrade-NG |
| RQAlpha | github.com/ricequant/rqalpha | Python | ~6.7k | 活跃 | 聚宽开源回测框架，中国品种覆盖好 |
| ElegantRL | github.com/AI4Finance-Foundation/ElegantRL | Python | ~4.4k | 维护放缓 | 轻量 PyTorch RL（PPO/SAC/A2C/DDPG/TD3） |
| RLTrader | github.com/notadamking/RLTrader | Python | ~1.9k | 停滞 | 加密 RL 交易（PPO） |
| Stable-Baselines3 | github.com/DLR-RM/stable-baselines3 | Python | 高 | 活跃 | RL 算法事实标准实现（PPO/SAC/DQN/A2C/TD3/DDPG） |
| pinkfish | github.com/hugo-andrew/pinkfish | Python | ~302 | 停滞 | 轻量回测/选股 |
| 天勤 TQSDK | github.com/shinnytech/tqsdk | Python | 中 | 活跃 | 期货实时行情+历史全 Tick+Tick 级回测+90% 期货公司实盘/模拟（Apache-2.0） |
| TensorTrade-NG | 社区 fork | Python | — | 活跃 | 接替停滞的原 TensorTrade |
| QUANT (maxime-blanchard) | 未核实 | — | — | — | 多次检索未找到权威仓库，标注"未核实" |

## 2. 重点深研：Kronos

- **是什么**：首个开源的"金融 K 线语言"基础大模型（论文 arXiv:2508.02739，2025.08，MIT 协议）。
- **架构**：专用金融分词器把连续多维 OHLCV 量化为分层离散 token；**解码器式分层自回归 Transformer** 做预测（这与用户要求的"自回归进化方向"高度契合）。
- **预训练规模**：覆盖 45 个全球交易所、120 亿条记录；模型族 mini(4.1M)/small(24.7M)/base(102.3M) 已开源，large(499.2M) 未开源。
- **能力**：**零样本（zero-shot）预测**，RankIC 提升 87~93%；自带 `finetune/` + `qlib_test.py` 回测；已有**中国 A 股示例**。
- **是否支持期货**：预训练数据含期货，但开箱示例仅 A 股/BTC，**中国期货需自建数据管线**。
- **胜率/回测**：官方未公布"胜率"，头条指标是 RankIC；回测为演示级（top-K 简单策略），非生产级。
- **局限**：自回归推理对显存有要求；中文文档/中国期货适配弱；需接入真实交易成本与滑点才能评估实盘胜率。

## 3. 特性对比表（预测方法 / 期货 / 中国期货 / 数据 / 内置RL / 自动调参 / 回测 / 成熟度）

| 项目 | 预测方法 | 期货 | 中国期货 | 数据接入 | 内置RL | 自回归/进化 | 回测 | 成熟度 |
|------|---------|------|---------|---------|-------|-----------|------|-------|
| Kronos | 自回归 Transformer | 含(训练) | 需自建 | CSV/qlib | 否 | ✅自回归 | qlib_test(演示) | 中(新) |
| Qlib | 树/DL/TS 模型 | ✅ | ✅ | 内置+Yahoo/自定义 | 否 | 部分(模型选择) | ✅walk-forward | 高 |
| vn.py | 策略驱动 | ✅✅ | ✅✅ | CTP/TQSDK/TuShare等 | 否 | 否 | ✅Tick/Bar | 高 |
| TQSDK | 策略驱动 | ✅✅ | ✅✅ | 全 Tick/实时 | 否 | 否 | ✅Tick级 | 高 |
| FinRL | RL(PPO等) | ✅ | 部分 | Yahoo/CSV | ✅ | 否 | ✅ | 中高 |
| TensorTrade | RL(Gym) | ✅ | 否 | 自备(CCXT/yf) | ✅ | 否 | Gym仿真 | 低(停滞) |
| ElegantRL | RL | ✅ | 否 | 自备 | ✅ | 否 | 自备 | 中 |
| RQAlpha | 规则/ML | ✅ | ✅ | 聚宽 | 否 | 否 | ✅ | 高(中国) |
| Stable-Baselines3 | RL算法库 | — | — | 接环境 | ✅(算法) | 否 | 接环境 | 高 |
| backtrader | 规则 | ✅ | ✅ | 多源 | 否 | 否 | ✅ | 高 |

## 4. 取长补短——"特性拼图"建议（关键交付）

为构建**面向中国商品期货、高胜率、预测+RL 双层、本地 GPU 可训练**的模型，建议从以下开源项目各取所长拼成 7 模块：

1. **信号基座 → Kronos（自回归基础模型）**：取其分层自回归 K 线分词+预测，做零样本泛化强的方向/涨跌概率信号；在中国商品期货数据上 finetune 小模型（mini/small）。
2. **数据与回测 → Qlib + RQAlpha + 天勤 TQSDK**：Qlib 做特征工程与 walk-forward 回测（防过拟合）；天勤/聚宽提供中国商品期货历史全 Tick 与实时数据；RQAlpha 做规则基线对比。
3. **RL 执行层 → FinRL + Stable-Baselines3（+ TensorTrade-NG 参考）**：上层 RL 智能体（PPO/SAC）负责仓位/择时/退出，用 SB3 的可靠算法实现，FinRL 的环境范式做参考。
4. **自动迭代/进化 → RD-Agent + Optuna + 神经进化(EXAMM)**：RLlib/Optuna 做超参进化；借鉴 EXAMM 做在线神经架构搜索（online NAS），随实时数据流动态重构网络拓扑以对抗市场漂移。
5. **集成基线 → 传统技术指标 + 轻量网络(LSTM/GRU/TCN)**：作为集成成员与基线，提升鲁棒性、避免单模型崩溃。
6. **工程落地 → vn.py (CTP) / 天勤**：实盘接入、穿透式监管、Tick 级回测。
7. **风控层（自建）**：ATR 自适应止损、回撤恢复协议、凯利/风险预算，独立于信号防止"高胜率低收益"。

> 用户交易系统 v4.0 已有：ATR×2.5/2.0/1.5 自适应止损、卖出信号引擎(S1–S5)、风险预算框架、回撤恢复 R1–R4——可直接复用为风控层。

## 5. 待澄清问题（已部分由用户拍板，余下供架构阶段确认）

- ✅ 目标品种/周期：商品期货·日线/小时线
- ✅ 模型形态：预测+RL 双层
- ✅ 算力：本地 GPU 可训练小模型
- 余：数据授权与获取渠道（天勤/聚宽/SIMNOW 免费行情 vs 付费）、是否最终实盘、胜率口径（方向准确率 vs 交易胜率）、回测成本/滑点假设

## 6. 结论

开源生态已覆盖"预测基座—数据回测—RL 执行—自动进化—实盘接入"全链路。Kronos 的自回归架构天然契合用户"自回归进化"诉求；EXAMM/遗传算法的在线进化有学术实证（期货场景跑赢 ARMA/MACD/买入持有）。下一阶段由架构师把上述拼图落为可运行系统的具体设计与任务分解。
