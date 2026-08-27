# HexFutures-AI

**中国商品期货高胜率预测 + RL 双层 + 自回归进化研究框架**（软件团队 software-hexfutures-ai 交付）。

基于开源生态「特性拼图」构建：Kronos（自回归基础模型，可选）+ 自研 ARTransformer（CPU 可跑 fallback）
+ Stable-Baselines3/纯 numpy PPO（RL 决策层）+ Optuna & EXAMM 风格神经进化 + 天勤 TQSDK/AkShare 数据 + vn.py 实盘骨架。

## 版本

- **v0.1.0**（2026-08-23）：代码审计 v2 收官——31 项缺陷定点修复（P0/P1/P2 全闭环，549 项测试全绿）；pyproject 依赖声明补齐；CI 干净环境稳定
- **2026-08-22**：P25 影子基线 v2→v8（生产基线对照）升级
- **2026-08-16**：LightGBM 冠军 v4（方向准确率 70.23%）终报 + 首轮代码审计（4 项 P1 修复）

## 模型回测表现（可溯源交付报告）

> 数据来源：`deliverables/software-hexfutures-ai/lightgbm-champion-final-report-2026-08-16.md`、`sentinel2-p25-v13-natural-rebuild-2026-08-22.md`

### 预测层（LightGBM 冠军 v4，闸门1 口径）

标的 SHFE.au / SHFE.ag / DCE.m 主力连续，日线 2018-01~2024-12，5 日 horizon，66 折 walk-forward（train 250/test 60/purge 5/embargo 2）+ per-fold Platt 校准：

| 指标 | R7 基准 | **冠军 v4** | Δ |
|---|---|---|---|
| 方向准确率 dir_acc | 67.89% | **70.23%** | **+2.34pp** |
| RankIC | 0.4092 | **0.4510** | **+0.042** |
| 覆盖 | 84.60% | **85.04%** | +0.44pp |

- 有效增量仅 4 项：per-fold Platt 校准（基础）、Optuna 调优（+0.44pp）、区间位置 f_range_pos_20（+0.34pp）、UUP 美元组（+1.17pp）；其余 20+ 候选全 66 折实证为负贡献
- 贵金属宏观锚：**UUP / SPX 为唯二正向特征**，TNX/TLT/IEF/IXIC/DJI 均负向

### 生产组合（引擎 A/B，闸门2 口径，含成本）

18 品种，v8 生产基线（cross_z 标签 + 冠军 HP），OOS 2024-07-18 后；口径：引擎 A top_k=0.30/min=3/group_cap=0.5、引擎 B win252/thr0.70、组合 A30/B70、完整回测（滑点 1tick + 费 0.005% + 保证金 12% + CONTRACTS18）：

| 指标 | v8 生产基线 | tail_ext（跨边界参考） |
|---|---|---|
| 引擎 A S2 OOS Sharpe | **1.064** | 1.096 |
| 引擎 A S2 OOS 复利 | **+11.59%** | +11.81% |
| 组合 A30/B70 OOS Sharpe | **0.717** | 0.725 |

- 影子基线（p12 S4，v8 vs rt30 候选）：滚动 IC(63) -0.199 vs -0.026，滚动命中率(63) 0.416 vs 0.528，池化相关性 0.311

### demo 快速开始（合成数据，`--demo`）

方向准确率 76.82%（闸门1 ✅ PASS；闸门2 合成数据下 ⛔ FAIL，PBO 0.500——研究型输出非交付承诺）

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

# 可选：安装外部数据源（akshare / pytdx / tqsdk / requests），缺依赖时对应源 health_check 返回 False
pip install -e .[sources]

# 兼容路径：仍可用 requirements.txt（15 项核心依赖 + pytest(dev)，向后兼容）
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

## 近期优化与修复

### 优化（2026-08 精进）

- **LightGBM 冠军 v4**：方向准确率 67.89% → 70.23%、RankIC 0.4092 → 0.4510（per-fold Platt 校准 / Optuna 调优 / f_range_pos_20 / UUP 美元锚 4 项有效增量）
- **双引擎架构**：引擎 A（LightGBM 横截面 top30%）+ 引擎 B（趋势 win252/thr0.7）+ 组合配置（生产 A30/B70），OOS 全程含成本评估
- **贵金属宏观锚锁定**：UUP/SPX 正向、TNX/TLT/IEF/IXIC/DJI 负向（唯二正向特征）
- **依赖治理**：pyproject 单一事实源（核心 15 项 + dev/torch/sb3/sources/optional 5 组 extra）；gymnasium 移入 sb3（零引用）；torch 上限校准 <3

### 修复（2026-08-23 代码审计，31 项闭环）

- **红线级**：PBO 过拟合闸门失效（`is` 恒 False→恒 0.5）、PPO 策略梯度空操作（`[:,None]` 广播 + 冗余 ratio）、DSR 退化恒真（重写为偏度/峰度感知 Bailey–López de Prado 式）
- **风控**：ATR ratchet 方向反向（只收窄→只增不减）、trailing_stop 死代码接入、RL 路径 S1/S2/S5 上下文透传、预算上限 `min(abs(budget), max_position_pct)`、涨跌停 bar 乐观成交拦截、平今双倍费不生效（open_dates 跟踪）
- **防泄漏（L1–L9）**：winsorize/tokenizer 全样本未来函数（改 rolling/expanding 因果）、尾部标签伪造看涨（np.nan 剔除）、跨品种归一锁列、外盘 reindex→asof、pivot 短名静默均值、splitter 非幂等、pytdx 主力/市场硬编码（跨市场枚举 + 实时持仓量选主力）、Kronos 配对绕过、SignalStore 无 OOS 校验
- **评估口径（E1–E4）**：缺盈亏比（补 profit_factor + PF 行）、DSR/PBO 文档混淆、RL 单品种 vs 基线全品种口径不可比、walkforward 短折年化 + ddof=0
- **进化/实盘（V1–V5）**：EXAMM LSTM 单元记忆重置 + 训练集选种（改验证集）、Optuna 剪枝失效（MedianPruner + report）、PSI 空箱爆炸、CTP 缺凭证仅 print（改 raise）、contracts=None 乘数规格回退
- 全部附回归测试（549/549 绿），提交链可溯源（`deliverables/code_audit_recheck_20260823.md` 含问题级提交映射）

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
tests/                # 549 项测试（防泄漏/风控优先级/成本/一致性/RL/进化/漂移/管线/实盘守卫）
docs/                 # 架构设计（system_design.md 等）
```

## 许可

MIT（研究用途；实盘前请自行完成穿透式监管报备与合规审查，本框架不构成投资建议）。
