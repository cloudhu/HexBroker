# HexBroker 架构对比与优化方向建议

- **项目代号**：HexBroker（HexFutures-AI）对比分析
- **作者**：高见远（软件架构师）· 2026-08-26
- **输入**：产品经理调研报告 `01-pm-market-research.md`（14 个开源项目对比矩阵 + 8 条最佳实践）
- **方法**：本地代码走读（README / docs/system_design.md / docs/developer-guide.md / hexbroker 全部子模块 / pyproject.toml / CI / tests）＋开源项目公开信息对照
- **性质**：仅架构级分析与建议，不含代码修改

---

## 0. 执行摘要（TL;DR）

HexBroker 是**研究型（research-grade）纯 Python 期货研究框架**，其**数据防泄漏纪律与评估方法论是明显强项**（SignalStore OOS 物理隔离、walk-forward purge/embargo、训练-回测一致性 <1e-6、DSR/PBO 诊断、40+ 轮"幻觉拆穿"实验史），在 9 个对比维度中**训练侧（ML/RL/特征/walk-forward）成熟度高，交易侧（撮合/实盘/中国市场规则/运维监控）成熟度低**——与 vn.py、WonderTrader、NautilusTrader 等"交易基础设施"型项目的差距集中在后者。

按"收益/成本/风险"给出优化优先级：

- **P0（立即动手，高收益低成本）**：① 撮合模型细化＋撮合假设显式文档化；② 前视/递归依赖自检工具；③ 数据-信号-模型四层版本化记录；④ Bootstrap 绩效区间。
- **P1（中等收益）**：⑤ CTP/SimNow 通道落地（Gateway 抽象）；⑥ 因子库资产化＋表达式引擎；⑦ 中国市场规则内建为规则引擎；⑧ 风控可插拔化＋组合/通道级风控；⑨ RL 滚动重训调度与模型版本管理。
- **P2（锦上添花/长线）**：⑩ 编译内核或至少向量化研究引擎；⑪ 可视化/监控/通知；⑫ 事件溯源与协议化开放架构。

**最值得立即动手的 3 条排序结论**（详见 §5）：
1. **回测撮合可信度加固**（P0-1/2）——直接决定全部历史绩效数字的对外可信度；
2. **数据/实验版本化记录层**（P0-3）——为 walk-forward 多模型迭代提供"可复现"地基，防止 40+ 轮实验中的"幻觉"模式复发；
3. **CTP/SimNow 通道**（P1-5）——补上"研究 → 实盘"最后一环，与现有 paper 簿记形成"回测-模拟-实盘"三级一致性闭环。

---

## 1. HexBroker 现状画像

### 1.1 定位与技术栈

| 项 | 内容 |
|---|---|
| 定位 | 中国商品期货**高胜率预测 + RL 双层 + 自回归进化**研究框架（README）；实际生产主线为 **Sentinel-2 双引擎组合**（引擎 A 趋势 LGBM 截面 + 引擎 B 基差时序） |
| 语言 | **纯 Python ~2.1 万行**（README），numpy/pandas/scikit-learn/lightgbm/optuna 为核心，无编译内核、无 Numba |
| 依赖 | pyproject.toml 单一事实源：核心 15 项 + dev/torch/sb3/sources/optional 5 组 extra；**optional 组已声明 pyqlib/rqalpha/vnpy/mlflow**（未启用） |
| 测试 | **549 项全绿**，覆盖防泄漏/风控优先级/成本/一致性/RL/进化/漂移/管线/实盘守卫 |
| CI | `.github/workflows/ci.yml`：py3.11/3.12 × pytest + 并行 `lint` job（ruff/black/mypy，P1-10 新增） |
| 文档 | README + `docs/system_design.md`（模拟盘设计）+ `docs/developer-guide.md`（v3.29，含 40+ 轮实验史与"幻觉拆穿清单"）——质量明显高于多数开源项目 |
| 实盘状态 | CTP **受控骨架**（仅凭证校验 + 下单占位）；paper 模拟盘（新浪实时价 + SimBroker 簿记）；生产节奏 p23_daily_run（T+1 计划-入账） |

### 1.2 三层协作架构（README）与 SENTINEL 四层

```
数据层(天勤/AkShare/CSV) → 特征层(技术/微观/滚动归一化)
  → 预测层(Kronos|ARTransformer|TCN/GRU/LightGBM) —— walk-forward 产出 OOS 信号
    → SignalStore（只落 OOS 信号，RL 环境只读它——物理防泄漏）
      → RL 决策层（PPO，风控 in-the-loop）
        → 风控层（硬止损>S1–S5>预算>R1–R4>RL 意图；ATR 三档只增不减）
          → 回测/评估层（bar 级事件回测，训练-回测一致性 <1e-6）
            → 进化层（Optuna + EXAMM 神经进化 + PSI 漂移）
              → 报告（Q5 双闸门 + DSR/PBO）
```

对照 PM 调研：**SENTINEL 四层（L1 StrengthRanker→L2 State Gate→L3 PPO→L4 ExAMM）与 QuantConnect LEAN 的 Algorithm Framework（Universe→Alpha→Portfolio→Execution→Risk）理念同构**，但 HexBroker 目前**未显式建模 Portfolio Construction 与 Execution 两个中间层**——L1 选标的、L2 门控直接落到 SimBroker 逐标的执行，缺少"组合级目标仓位合并/分配"抽象（WonderTrader 组合盘），这正是 P1-8 组合/通道级风控的架构切入点。

### 1.3 能力维度成熟度总览（1-5 分）

| 维度 | 成熟度 | 一句话判断 |
|---|---|---|
| 1 回测引擎 | ★★☆☆☆ | 事件驱动骨架可用、成本口径精细，但撮合模型简化、无自检工具、无假设文档 |
| 2 实盘接入 | ★☆☆☆☆ | CTP 仅骨架、无 SimNow、无 Gateway 抽象；paper 簿记内部化 |
| 3 数据层 | ★★★☆☆ | 分层湖 + 复权 + 多源抽象好；缺版本化与 point-in-time 数据层 |
| 4 因子/特征 | ★★★★☆ | 25+ 特征流水线严格因果、白名单设计佳；缺表达式引擎与因子库资产化 |
| 5 ML/RL 工程化 | ★★★★☆ | walk-forward + 一致性 + 诊断强；缺滚动重训调度与实验记录层 |
| 6 风控体系 | ★★★★☆ | 优先级链分层是亮点；缺可插拔、通道级与"紧急离合器" |
| 7 中国市场规则 | ★★☆☆☆ | 保证金/涨跌停/平今/夜盘零散内建；缺规则引擎、交割月限制 |
| 8 工程化 | ★★★★☆ | 测试/文档/CI 优秀；纯 Python 性能、CI 未含静态检查 |
| 9 可视化/监控 | ★★☆☆☆ | 文本报告 + CLI 监控脚本；无 Web/图表/通知 |

---

## 2. 九维度逐项对比（现状佐证 → 差距 → 借鉴 → 建议）

### 2.1 回测引擎（事件驱动 vs 向量化、撮合模型细节、前视偏差自检、回测-实盘语义一致性）

**现状（代码佐证）**：
- `hexbroker/backtest/engine.py`：bar 级事件驱动，逐 bar 逐标的按 `target` 前向填充执行 `SimBroker.execute`；涨跌停拦截开关（P8：`limit_trade_allowed`，涨跌停 bar 跳过成交）。
- `hexbroker/backtest/broker.py`：`SimBroker.execute` 含加权均价、平今判定（`_compute_is_today_close` 按净持仓开仓日近似）、反手重置、品种级 multiplier 记账（P18-P0 修复，历史绩效修正 -68% 量级）。
- `hexbroker/backtest/cost.py`：固定 `slippage_ticks=1.0`、手续费开/平/平今、保证金 12%（可配置）。
- 训练-回测一致性：`FuturesTradingEnv.target_frame` 重放给 `BacktestEngine`，权益差 <1e-6（test_backtest_env_consistency）。
- **前视偏差自检**：训练/信号层防泄漏极强（SignalStore 只落 OOS + `assert_no_leakage` purge/embargo），但**策略/特征代码本身的 lookahead/recursive 依赖无工程化检测工具**。
- developer-guide §9.6 QA 建议项明确：**"同 bar 成交约定为已知限制"**——信号 bar 收盘价与成交价同 bar，未建模"下一 bar 成交"语义。

**差距**：
1. 撮合模型过简：固定 1 tick 滑点、无成交量约束、无部分成交、无跳空处理、无订单簿/盘口深度——与 backtrader（下一根 K 线成交、跳空止损、成交量限制、部分成交）、freqtrade（14 条撮合假设显式文档化）、NautilusTrader（tick/订单簿精度）相比是最大差距。
2. 同 bar 成交语义：回测绩效系统性偏乐观，且偏差**未量化**（无"回测 vs 下一 bar 成交"对照实验）。
3. 无 lookahead/recursive 自检 CLI（freqtrade 官方工具）。
4. 向量化缺失：研究阶段大规模参数扫描慢（vectorbt 10-1000× 范式），参数网格（如 P9 12 配置网格、P19 组合网格）靠串行重跑。

**借鉴对象**：backtrader（撮合细节）、freqtrade（14 条撮合假设 + lookahead-analysis/recursive-analysis）、vectorbt（研究引擎向量化分工）。

**建议动作**：
- **P0-1 撮合假设显式化与细化（可直接借鉴 backtrader/freqtrade 思路，需自研适配期货）**：把现有"成交价=收盘价±1tick、同 bar 成交、无成交量约束"写成文档化假设清单；新增可配置的 `next_bar_execution` 开关与成交量约束（`volume_cap`），并在报告中输出"当前撮合假设 vs 保守假设"双口径绩效。
- **P0-2 前视/递归依赖自检工具（可直接借鉴 freqtrade 实现思路）**：对策略 target 生成链做 DAG 依赖分析，检测"同一 bar 内用收盘价决定收盘价成交"等模式；接入 `pytest` 与 pipeline 报告。

**收益/成本/风险**：P0-1 直接决定**全部历史绩效数字的对外可信度**（已出现过 P18 multiplier 修复导致 Sharpe -68% 的先例，撮合假设是同类风险）；纯 Python 增量，成本低；风险低（不改默认口径，只加双口径对照）。

### 2.2 实盘接入（CTP 对接成熟度、模拟盘、通道抽象）

**现状（代码佐证）**：
- `hexbroker/live/ctp_skeleton.py`：`CTPLiveGateway` 仅"凭证校验（`--i-understand-the-risk` + 环境变量 + AppId/AuthCode）→ 风控包装 → 下单占位 `submit_order` 抛 `CTPGuardError`"，**`_connect_gateway` 是 TODO 占位**，无 vnpy_ctp 依赖、无真实网络逻辑。
- `hexbroker/paper/`：`PaperBroker` 薄封装 SimBroker（同口径成本/保证金/平今费 + 资金约束），实时行情用新浪 `hq.sinajs.cn`（标准库 urllib，零依赖）；`TradingScheduler` 按品种时段/节假日轮询；情报 `IntelligenceProvider` 接口隔离（MCP 桥接列为 P1）。
- 外部模拟盘（westock/mx-moni）实测仅支持 A 股（P17 结论），**无 CTP SimNow 模拟盘接入**。
- 生产节奏：`p23_daily_run.py` T+1 计划-入账（日频，非实时 tick 交易）。
- 通道抽象：仅有 `DataSource`（数据）抽象，**无"交易通道/Gateway"插件化抽象**（vn.py Gateway 模式缺失）。

**差距**：
1. CTP 实盘 = 骨架，**无法真实下单**；穿透式监管报备（AppID/AuthCode）已预留但未与任何柜台联调。
2. **无 SimNow 模拟盘**：国内期货 CTP 生态"回测→模拟→实盘"三级验证链路缺失（vn.py 标配）。
3. 无 Gateway 插件化抽象：未来接 CTP/Femas/易盛需重构，而不是加一个适配器。
4. paper 模拟盘基于新浪公开行情（非交易所行情），撮合与真实盘口有已知偏差（system_design §8.6 已认可）。

**借鉴对象**：vn.py（Gateway 插件化 + SimNow + 20+ 柜台）、NautilusTrader（适配器模式 + 回测/实盘同一执行语义）。

**建议动作**：
- **P1-5 CTP/SimNow 通道（建议直接借鉴 vn.py vnpy_ctp 网关，HexBroker 侧做薄适配）**：在 `CTPLiveGateway._connect_gateway` 接入 vnpy_ctp；新增 `LiveBroker` 接口（与 `SimBroker`/`PaperBroker` 同 `execute` 契约），先接 SimNow 验证，再切实盘；保持 `RiskManager.evaluate` 二次校验在途（现有骨架已含风控包装，改造小）。
- **P1-5b 通道抽象**：定义 `BrokerGateway` ABC（connect/query/order/cancel），把 SimBroker/PaperBroker/CTP 网关统一为同一接口，为多柜台留扩展点。

**收益/成本/风险**：P1-5 补上"研究→实盘"最后一环，且与现有 paper 簿记同口径可交叉验证；成本中等（引入 vnpy_ctp 依赖 + 联调）；**风险为合规**（穿透式监管报备是硬门槛，且 vn.py 依赖较重，需评估是否用 `vnpy_ctp` 单包而非全量 vn.py）。

### 2.3 数据层（数据源、版本管理、point-in-time、复权换月）

**现状（代码佐证）**：
- `hexbroker/data/store.py`：`DataLake` raw/interim/processed 三层 Parquet，按 symbol/freq/year 分区；`save_processed/load_processed/exists`。
- `hexbroker/data/contract.py`：`ContractStitcher` 主力合约拼接（open_interest/成交量规则）+ 比例后向复权（保留 `raw_close` 用于成本与涨跌停判定，输出 `adj_close`）。
- 多源抽象：`DataSource` ABC（`fetch_bars` + `health_check` 懒加载降级）；实现 sina/akshare/pytdx/tqsdk/csv/synthetic/qlib_source 共 7 个源；生产实际用 PandaData `close_pcr` 后复权主力连续 + sina 校准拼接（developer-guide §2.1）。
- point-in-time：特征层严格因果（rolling/expanding z-score、外盘 `reindex→asof`、跨品种归一锁列）；但**数据落盘层无"快照/版本"语义**。
- 换月复权：有，但**无合约生命周期建模**（NautilusTrader 的合约激活/到期、交割日）。

**差距**：
1. **数据无版本管理**：数据更新（P20/P21/P6-4 等）靠手工文档记录 `RAW_SCALE_FIX` 常量（au0×1.2596 等）、`artifacts/backup_p64/` 手工备份——无 zipline 式 data bundle / NautilusTrader ParquetDataCatalog 式版本化目录。**数据版本 ↔ 信号缓存版本 ↔ 模型版本无系统化对应**，这是 40+ 轮实验"口径混淆"（v2/v8 缓存、buggy/fixed broker）的重要根因。
2. 无 point-in-time 数据层（qlib 二进制数据层防"用今日修正后的历史"）：当前靠代码纪律（rolling/asof）而非数据层保证。
3. 无合约生命周期：换月日前后是否可交易、交割月限制等未内建（影响临近交割的策略）。

**借鉴对象**：qlib（point-in-time 数据层 + 二进制存储）、zipline-reloaded（data bundle 版本化）、NautilusTrader（Parquet 目录 + 合约激活/到期建模）。

**建议动作**：
- **P0-3 数据-信号-模型四层版本化（借鉴 qlib recorder + zipline bundle，需自研轻量实现）**：在 `DataLake` 上增加 manifest（数据指纹/拉取时间/口径常量）；`SignalStore` 目录已按 `model_id/train_end` 分片（已是半版本化），补"数据版本 + 特征版本 + 模型参数"指纹列；报告统一输出四层指纹。**成本低（纯元数据层）**。
- **P2-12 合约生命周期建模（借鉴 NautilusTrader）**：`ContractStitcher` 增加交割月/活跃期标记，回测引擎支持"非活跃合约禁交易"。

**收益/成本/风险**：P0-3 收益极高——项目已多次因"版本口径混淆"造成结论反复（v2/v8、buggy/fixed、RAW_SCALE_FIX），版本化是根治；成本低；风险低。

### 2.4 因子/特征工程（表达式引擎、因子库、自动挖掘、walk-forward 支持）

**现状（代码佐证）**：
- `hexbroker/feature/pipeline.py`：`FeaturePipeline` 可组合流水线 technical→microstructure→iterative→weekly→cross→fundamental→normalize，25+ 特征（f_ret_1/f_vol_5/f_intraday_range/f_xr_spx_* 等），特征-品种白名单（分组建模核心），滚动 z-score 严格因果，`keep_features` 白名单裁剪。
- walk-forward 支持好：`WalkForwardSplitter`（purge/embargo/rolling/expanding）+ `ForecastTrainer` 逐折训练（LGBM/ARTransformer/TCN/GRU/Kronos 注册表 `utils/registry.py`）。
- 特征实现为**代码 transformer 硬编码**，无表达式 DSL。

**差距**：
1. **无因子表达式引擎**（qlib ExpressionEngine 懒加载/缓存缺失）——新增特征要写代码，不能配置化表达（如 `Ref(close,5)/close-1`）。
2. **因子库未资产化**：25 特征是"白名单 + 代码"，无 Alpha158/360 式预置库、无因子注册/复用机制、无因子 IC 档案（有零散 scripts/feature_symbol_ic.py，但非平台能力）。
3. 无自动因子挖掘（qlib RD-Agent 方向）。

**借鉴对象**：qlib（ExpressionEngine + Alpha158/360 + RD-Agent）、vn.py vnpy.alpha（Alpha158+LGBM 闭环）、Hikyuu（组件化因子组合）。

**建议动作**：
- **P1-6 因子资产化（借鉴 qlib 表达式引擎的轻量子集，需自研）**：定义 `factor_expr` 配置 DSL（复用现有 transformer 原语：ret/vol/ma/macd/rsi 等），支持"表达式 → 特征列"注册表 + 因子 IC 缓存；**不引入完整 qlib 依赖**（pyproject optional 已有 pyqlib，可评估按需启用）。
- **P2-13 自动因子挖掘（借鉴 qlib RD-Agent，长线）**：在 Optuna 搜索空间内组合因子表达式，用 OOS IC 过滤。

**收益/成本/风险**：P1-6 提升研究复用率与实验迭代速度（当前每次加特征都要改 pipeline 代码 + 重跑全链路）；成本中；风险低（DSL 与现有 transformer 并存，向后兼容）。

### 2.5 ML/RL 工程化（滑动窗口重训、RL-回测引擎复用、Bootstrap 绩效区间、实验记录可复现）

**现状（代码佐证）**：
- walk-forward 滚动重训：`ForecastTrainer` 逐折训练（train_len/test_len/purge/embargo）；S4 rt30 影子验证"更频繁重训"（developer-guide P7/P12）。
- **RL-回测引擎复用是亮点**：`FuturesTradingEnv` 与 `BacktestEngine` 共用 `SimBroker/CostModel`，`target_frame` 重放一致性 <1e-6；风控 in-the-loop（`RiskManager.evaluate` 嵌入 `env.step()`）；与 Wt4ElegantRL（RL 训练套在 wtpy 回测引擎上）同构。
- RL 实现：纯 numpy PPO（`rl/agent.py`）+ SB3 适配（`rl/sb3_wrap.py`，torch/sb3 extra）；静态防泄漏检查 `assert_env_has_no_model_dependency`。
- **Bootstrap 绩效区间：无**——只有 DSR/PBO 点估计（`evaluation/stats.py`、pipeline `_pbo/_dsr`），无 PyBroker 式 bootstrap 置信区间。
- **实验记录可复现：部分**——pipeline 落 JSON/MD 报告（含 `config.model_dump()` 全量配置 + run_id），但无 MLflow 式 recorder（pyproject optional 已声明 mlflow，未启用）、无"数据/特征/模型/参数"四层版本对应。

**差距**：
1. 滚动重训是"手动调度 + 手工版本管理"（p23 流程），非 FreqAI 式自动滑动窗口（train_period/backtest_period/live retrain + 模型版本回滚）。
2. 无 Bootstrap 绩效区间：策略对比只有点估计，无统计显著性（PyBroker 的 `bootstrap` 缺失）。
3. 实验记录未系统化：40+ 轮实验结论散在 developer-guide 文本，无机器可查的实验库（MLflow/recorder）。

**借鉴对象**：freqtrade FreqAI（滑动窗口重训调度）、PyBroker（walkforward 一等公民 + Bootstrap）、qlib（MLflow recorder + 可复现工作流）。

**建议动作**：
- **P0-4 Bootstrap 绩效区间（可直接借鉴 PyBroker 算法）**：对权益曲线做 block bootstrap，输出 Sharpe/Calmar/MaxDD 的 95% CI；报告标注"点估计 + 区间"。
- **P1-7 RL/ML 工程化（借鉴 FreqAI + qlib recorder）**：把 p23 手工重训节奏升级为可配置调度（retrain_freq/model_registry/rollback）；启用 mlflow（optional 已声明）或自研轻量 recorder 落四层指纹。
- **P2-14 事件溯源（借鉴 NautilusTrader）**：把回测/模拟盘运行存为可重放事件流，审计与复现升级。

**收益/成本/风险**：P0-4 成本极低（numpy 实现）且直接提升"闸门 2"统计可信度；P1-7 成本中，收益是解决"模型迭代不可比"这一已反复出现的痛点（P13 F2 伪影、P14 小样本校准噪声）。

### 2.6 风控体系（分层、可插拔、熔断、组合级风控）

**现状（代码佐证）**：
- **分层是强项**：`RiskManager.evaluate` 优先级链（硬止损 > S1-S5 卖出引擎 > 波动率目标/Kelly 预算 > R1-R4 回撤恢复 > RL 意图），ATR 三档 ratchet 只增不减；`risk/` 子模块齐全（limits/budget/recovery/sell_engine/stoploss/types/manager）。
- 组合级：`group_cap=0.5`（黑色系敞口上限，P10）、组合波动率目标 17.5% EWMA、信号监控 W20（P0-1 降仓）。
- 熔断：`hard_stop`（回撤熔断清仓）；R2_HALT 暂停开仓。
- **可插拔性弱**：风控规则是代码写死的优先级链（配置驱动阈值，但规则本体不可热插拔/组合）——对比 LEAN 可组合多个风控模型、freqtrade 四层漏斗可开关。
- **无通道级风控**：自成交防护（WonderTrader 组合盘目标仓位合并）、下单流控、单笔订单量限制（NautilusTrader pre-trade）缺失——多策略组合核算时易产生自成交/超量。

**借鉴对象**：WonderTrader（资金/流控/账户三级 + 紧急离合器 + 组合盘防自成交）、freqtrade（四层风控漏斗）、LEAN（可插拔风控模型）、NautilusTrader（pre-trade 检查）。

**建议动作**：
- **P1-8 风控规则接口化（借鉴 LEAN 可插拔模型，自研轻量）**：定义 `RiskRule` ABC（evaluate(state, intent) → adjustment），把现有硬止损/S1-S5/预算/恢复重写为规则列表（默认顺序不变，保证与 549 项测试兼容），支持 yaml 增删。
- **P1-8b 组合/通道级风控（借鉴 WonderTrader 组合盘）**：在引擎 A/B 目标之上加"组合目标仓位合并层"（现有 combo_plan 已是雏形），实现自成交防护（同一品种多引擎目标合并）与下单流控。
- **P2-15 紧急离合器（借鉴 WonderTrader）**：一键全局断信号/平仓通道（现有 hard_stop 已接近，补"人工可触发"）。

**收益/成本/风险**：P1-8 收益中高（风控从"改代码"变"改配置"，且为实盘落地做准备）；成本中（重构 risk/ 需保住 549 项测试全绿）；风险低-中（重构回归风险，建议增量式：先加接口层，再逐个迁移规则）。

### 2.7 中国市场规则内建（保证金/涨跌停/夜盘/交割月/T+0）

**现状（代码佐证）**：
- 保证金：`CostModel.margin_rate`（默认 12% 统一，可配置；无分品种动态保证金率）。
- 涨跌停：`engine.py` P8 拦截（`limit_trade_allowed` + limit_up/down 列）。
- 平今双倍费：`broker._compute_is_today_close` + `CostModel.fee_close_today`。
- 夜盘跨日：`data/calendar.py` `FuturesCalendar.Session.crosses_midnight` + `paper/sessions.py` `TradingSession.day_label`（21:00 后归下一交易日）。
- 合约乘数/tick：per-symbol（CONTRACTS18 + `_SPEC_MULTIPLIER/_SPEC_MIN_TICK` 回退）。
- **交割月限制：无**；**T+0 天然支持**（期货当日可平，已有平今逻辑）；**分品种保证金率/涨跌停幅度表：无**（统一 12%/无幅度表）。

**差距**：
- 规则**零散内建**（cost.py/engine.py/calendar.py/paper），无 rqalpha 式"中国市场规则默认内建"的统一规则引擎；新策略/新品种需手工拼装规则。
- 缺交割月限制（临近交割保证金上浮/禁止开仓）、缺分品种涨跌停幅度与保证金率数据表（回测用统一值，与实盘差异）。

**借鉴对象**：rqalpha（A 股规则默认内建：T+1/涨跌停/集合竞价，Mod 插件化）、vn.py（CTP 行情含涨跌停/持仓量字段）、NautilusTrader（合约生命周期字段）。

**建议动作**：
- **P1-9 中国市场规则规则化（借鉴 rqalpha，自研）**：定义 `MarketRule` 配置表（每品种：保证金率/涨跌停幅度/交易时段/交割规则），`CostModel`/`BacktestEngine`/`RiskManager` 统一从表读取，替代硬编码常量；首批落"分品种保证金率 + 涨跌停幅度 + 交割月禁开仓"三项。
- 同步把 paper 的 `TradingSession` 时段表提升为全系统共享（目前 data/calendar 与 paper/sessions 有重复实现）。

**收益/成本/风险**：P1-9 提升回测与实盘一致性（统一 12% 保证金 vs 实盘分品种 8-15% 会造成回测偏差）；成本中；风险低。

### 2.8 工程化（CI/CD、测试覆盖、文档、性能瓶颈：纯 Python vs 编译内核）

**现状（代码佐证）**：
- CI：`.github/workflows/ci.yml` pytest（py3.11/3.12），**无 ruff/black/mypy 步骤**（配置已声明，未接线）。
- 测试：549 项全绿，覆盖维度广（防泄漏/风控/成本/一致性/RL/进化/漂移/管线/实盘守卫/paper）；`pyproject.toml` pytest 配置齐。
- 文档：README + system_design + developer-guide v3.29（40+ 轮实验史）——**优于多数开源项目**。
- 性能：纯 Python；日频/分钟级 CTA 够用（README 自述"中低频够用"）；**无编译内核**（对比 NautilusTrader Rust / WonderTrader C++ / Hikyuu C++）；**无 Numba/向量化**（对比 PyBroker/vectorbt）；Kronos 推理 CPU 可跑（fallback 设计）。
- 依赖治理：pyproject 单一事实源 + extra 分组 + 懒加载降级——工程习惯好。

**差距**：
1. CI 未含静态检查与类型检查（ruff/black/mypy 有配置未启用），代码质量门禁不完整。
2. 性能上限：纯 Python 事件驱动逐 bar（18 品种日频可接受；分钟级/Tick 级/大规模参数扫描会吃力）；无向量化研究引擎。
3. 无性能基准测试（benchmark）与内存/耗时画像。

**借鉴对象**：freqtrade（Docker 一键 + 高频发版 + 文档体系）、NautilusTrader/WonderTrader（编译内核）、vectorbt（向量化）。

**建议动作**：
- **P1-10 CI 补静态检查（低成本立刻做）**：CI 增加 `ruff check` + `black --check` + `mypy`（可选 strict 渐进），与 549 项测试并行。
- **P2-16 性能分层（借鉴 vectorbt 研究/执行分工）**：保留事件驱动回测为执行权威，新增"研究扫描向量化引擎"（targets 生成路径用 numpy 批量，供参数网格快速预筛），验证与事件引擎一致性后用于大规模扫描。
- **P2-17 编译内核（借鉴 NautilusTrader，长线）**：仅当演进到 Tick/高频或性能成为瓶颈时评估；日频 CTA 现状不建议投入。

**收益/成本/风险**：P1-10 成本极低、防回归；P2-16 成本中高、收益取决于研究规模（P9/P19 网格在 18 品种日频下尚可接受，优先级靠后）。

### 2.9 可视化/监控/运维

**现状（代码佐证）**：
- 报告：`evaluation/report.py` 文本 + JSON；pipeline 落 MD 报告（含双闸门结论）；paper `ReviewReporter` 每日复盘 md。
- 监控：P12 S4 影子监控（CLI 脚本，`p12_s4_shadow_monitor.py`）；P16 信号流水线 `a_status` 三态标注；paper 有 watchdog bat（`start_paper_trading_watchdog.bat`）+ PID lock（test_pid_lock）。
- 运维：p23_daily_run 一键化 + 防重入（apply_registry.json md5 指纹）——**日频生产节奏已可运转**。
- 可视化：matplotlib 在依赖中，但**报告无 equity 曲线图/热力图产出**；无 Web UI。

**差距**：
1. 无 Web/仪表盘监控（对比 WonderTrader WtMonSvr、freqtrade FreqUI/Telegram、vectorbt Plotly 仪表盘）。
2. 无图表化报告（equity 曲线、参数热力图、滚动 IC 图均缺——这些是 vectorbt/qlib 标配）。
3. 无告警通知（Telegram/微信/邮件）。

**借鉴对象**：freqtrade（FreqUI + Telegram + Prometheus/Grafana）、vectorbt（Plotly 交互仪表盘）、WonderTrader（WtMonSvr Web 监控）。

**建议动作**：
- **P2-18 图表化报告（低成本优先，借鉴 vectorbt Plotly）**：在报告生成链路加 equity 曲线 + 回撤 + 滚动 IC 图（matplotlib 已有依赖，先出静态图）；后续再上 Web。
- **P2-19 Web 监控 + 通知（借鉴 freqtrade）**：把 p23 每日节奏状态（数据新鲜度/计划/入账/影子监控）推送到轻量 Web 或微信/Telegram；成本中高，长线。

**收益/成本/风险**：P2 为主，不阻塞研究主线；图表化报告可先做（成本低，提升报告可读性）。

---

## 3. 优化方向建议清单（P0/P1/P2）

### P0 — 高收益低成本 / 高风险敞口

| 编号 | 方向 | 现状佐证 | 差距 | 借鉴对象 | 建议动作 | 收益/成本/风险 |
|---|---|---|---|---|---|---|
| P0-1 | 撮合模型细化 + 假设显式文档化 | `cost.py` 固定 slippage_ticks=1.0；engine.py 同 bar 成交；developer-guide §9.6"同 bar 成交为已知限制" | 无成交量约束/部分成交/跳空/下一 bar 语义；偏差未量化 | backtrader（下一根 K 线/跳空/成交量封顶）、freqtrade（14 条撮合假设） | 假设清单文档化 + `next_bar_execution`/`volume_cap` 可配置开关 + 双口径绩效对照 | 高/低/低 |
| P0-2 | 前视/递归依赖自检工具 | 训练/信号层防泄漏极强（SignalStore + assert_no_leakage），但策略/特征代码无工程化检测 | 无 lookahead/recursive 分析 CLI | freqtrade（lookahead-analysis/recursive-analysis） | 对 target 生成链做 DAG 依赖分析 + 接入 pytest/pipeline | 高/低/低 |
| P0-3 | 数据-信号-模型四层版本化记录 | DataLake 无 manifest；RAW_SCALE_FIX 靠文档；SignalStore 已半版本化（model_id/train_end） | 数据版本 ↔ 信号缓存版本 ↔ 模型版本无系统对应，40+ 轮实验反复出现口径混淆 | qlib（recorder）、zipline（bundle）、NautilusTrader（Parquet 目录） | DataLake manifest（指纹/口径常量）+ SignalStore 补四层指纹列 + 报告输出 | 高/低/低 |
| P0-4 | Bootstrap 绩效区间 | 只有 DSR/PBO 点估计（evaluation/stats.py、pipeline `_pbo/_dsr`） | 无置信区间、无统计显著性视角 | PyBroker（bootstrap） | block bootstrap 输出 Sharpe/Calmar/MaxDD 95% CI，报告双输出 | 中/低/低 |

### P1 — 中等收益

| 编号 | 方向 | 现状佐证 | 差距 | 借鉴对象 | 建议动作 | 收益/成本/风险 |
|---|---|---|---|---|---|---|
| P1-5 | CTP/SimNow 通道 | ctp_skeleton.py `_connect_gateway` 为 TODO；paper 用新浪行情；无 SimNow | 无法真实下单；无 Gateway 抽象 | vn.py（vnpy_ctp + SimNow + Gateway 插件化） | `_connect_gateway` 接 vnpy_ctp；`BrokerGateway` ABC 统一 Sim/Paper/CTP；先 SimNow 后实盘 | 高/中/中（合规门槛） |
| P1-6 | 因子库资产化 + 表达式引擎 | feature/pipeline.py 特征代码硬编码；25 特征白名单 | 无表达式 DSL、无因子注册/IC 档案 | qlib（ExpressionEngine + Alpha158/360） | 因子表达式 DSL（复用现有原语）+ 注册表 + IC 缓存；按需启用 pyqlib optional | 中/中/低 |
| P1-7 | ML/RL 滚动重训调度 + 实验记录 | ForecastTrainer 逐折重训；p23 手工调度；无 recorder | 非自动滑动窗口；无模型版本/回滚；无四层实验库 | freqtrade FreqAI、qlib（MLflow recorder） | 重训调度配置化（retrain_freq/model_registry）；启用 mlflow 或自研轻量 recorder | 中/中/低 |
| P1-8 | 风控规则接口化 + 组合/通道级风控 | RiskManager 优先级链代码写死；group_cap 已有雏形 | 不可热插拔；无自成交防护/流控 | LEAN（可插拔风控）、WonderTrader（组合盘防自成交） | `RiskRule` ABC 重构（保 549 测试绿）；组合目标合并层 + 自成交防护 | 中/中/中（回归风险） |
| P1-9 | 中国市场规则内建为规则引擎 | cost.py/engine.py/calendar.py 零散内建；统一 12% 保证金 | 无分品种保证金率/涨跌停幅度/交割月限制 | rqalpha（规则默认内建）、NautilusTrader（合约生命周期） | `MarketRule` 配置表 + 首批落保证金率/涨跌停幅度/交割月禁开仓 | 中/中/低 |
| P1-10 | CI 补静态检查 | ci.yml 只跑 pytest；ruff/black/mypy 已配置未接线 | 质量门禁不完整 | 通用实践 | CI 加 ruff/black/mypy 步骤 | 低/极低/低 |

### P2 — 锦上添花 / 长线

| 编号 | 方向 | 现状佐证 | 差距 | 借鉴对象 | 建议动作 | 收益/成本/风险 |
|---|---|---|---|---|---|---|
| P2-11 | 图表化报告 | report.py 纯文本；matplotlib 依赖未用 | 无 equity/回撤/热力图 | vectorbt（Plotly）、qlib | 报告链路加静态图表 | 中/低/低 |
| P2-12 | 合约生命周期建模 | ContractStitcher 无交割月/活跃期 | 非活跃合约可交易 | NautilusTrader | 交割月/活跃期标记 + 禁交易 | 中/中/低 |
| P2-13 | 自动因子挖掘 | 无 | 无自动挖掘 | qlib RD-Agent | Optuna 组合因子表达式 + OOS IC 过滤 | 中/高/低 |
| P2-14 | 事件溯源 | 回测/模拟盘运行无事件流 | 不可重放审计 | NautilusTrader | 交易事件流落盘 + replay | 中/高/低 |
| P2-15 | 紧急离合器 | hard_stop 有，无人工一键全局断 | 无紧急断信号 | WonderTrader | 人工可触发全局平仓/禁开仓通道 | 中/低/低 |
| P2-16 | 向量化研究引擎 | 纯事件驱动逐 bar | 参数扫描慢 | vectorbt | targets 生成路径 numpy 向量化 + 与事件引擎一致性验证 | 中/中高/中 |
| P2-17 | 编译内核 | 纯 Python 2.1 万行 | 高频/Tick 性能上限 | NautilusTrader（Rust）、WonderTrader（C++） | 仅高频演进时评估；日频不建议 | 高/极高/高 |
| P2-18 | Web 监控 + 通知 | p23 CLI + watchdog；无 Web/Telegram | 无远程可观测 | freqtrade（FreqUI/Telegram）、WonderTrader（WtMonSvr） | 每日节奏状态推 Web/微信/Telegram | 中/中/低 |

---

## 4. 对标开源项目速查（HexBroker 可直接借鉴清单）

| 开源项目 | 对 HexBroker 最有价值的 1-2 点 | 优先级 |
|---|---|---|
| vn.py | CTP Gateway 插件化 + SimNow 模拟盘 + Alpha158 因子闭环 | P1 |
| WonderTrader | 组合盘目标仓位合并防自成交；资金/流控/账户三级风控 + 紧急离合器 | P1/P2 |
| NautilusTrader | 回测/实盘同一执行语义；合约生命周期建模；Parquet 数据目录 | P1/P2 |
| QuantConnect LEAN | 可插拔风控模型（组合多个）；Algorithm Framework 五段式模块契约（与 SENTINEL 对照） | P1 |
| Microsoft qlib | point-in-time 数据层；因子表达式引擎 + Alpha158/360；MLflow recorder 实验记录 | P0/P1 |
| backtrader | 经纪商撮合细节（下一根 K 线成交/跳空/成交量封顶）；反面教材：停止维护风险 | P0 |
| zipline-reloaded | data bundle 版本化；Pipeline 因子管道 | P0 |
| rqalpha | 中国市场规则默认内建；Mod 插件化扩展 | P1 |
| freqtrade | 前视/递归自检工具；14 条撮合假设文档化；FreqAI 滑动窗口重训；四层风控漏斗 | P0/P1 |
| PyBroker | walkforward 一等公民 API；Bootstrap 绩效区间 | P0 |
| vectorbt | 研究（向量化扫描）与实盘（事件驱动）分离范式；参数热力图可视化 | P2 |
| QUANTAXIS | QIFI/MIFI/VIFI 协议化分层（多端接入） | P2 |

---

## 5. 最值得立即动手的排序结论（Top 3-5）

综合"收益/成本/风险"与项目现状（549 项测试全绿、生产基线 v8+A10/B90 已固化的研究型系统），建议按以下顺序立即立项：

1. **P0-1 撮合可信度加固（撮合假设文档化 + 双口径回测）**
   —— 理由：P18 multiplier 修复曾让历史绩效 Sharpe 系统性 -68%，**撮合假设是同等级的可信度风险**（固定 1 tick 滑点 + 同 bar 成交使绩效系统性偏乐观且未量化）。成本极低（纯 Python 增量 + 报告输出），收益是全部历史数字的可信度。
2. **P0-2 前视/递归依赖自检工具**
   —— 理由：项目最大资产是"防泄漏纪律"，但当前靠代码人工保证 + 静态断言已知模式；freqtrade 式 DAG 依赖分析能把"防泄漏"从纪律升级为工程能力，与 P0-1 同批交付。
3. **P0-3 数据-信号-模型四层版本化记录**
   —— 理由：40+ 轮实验史中反复出现"口径混淆"（v2/v8 缓存、buggy/fixed broker、RAW_SCALE_FIX 未复权/后复权拼接），根因是**版本对应关系无系统化记录**。SignalStore 已半版本化，补 DataLake manifest + 指纹列即可，成本低、直接服务 walk-forward 多模型迭代的可复现性。
4. **P0-4 Bootstrap 绩效区间**
   —— 理由：闸门 2 的 DSR/PBO 是点估计；补 block bootstrap 置信区间后，策略对比具备统计显著性视角（PyBroker 范式），成本极低。
5. **P1-5 CTP/SimNow 通道 + BrokerGateway 抽象**
   —— 理由：项目已具备研究-回测-模拟（paper）两级闭环，唯独缺"实盘"一环；SimNow 接入后可与 paper 簿记同口径交叉验证，形成 vn.py 式"回测-模拟-实盘一套代码"三级闭环。有合规门槛（穿透式监管），需与业务侧确认，但架构准备（Gateway 抽象）可以先做。

> 建议实施顺序：P0-1/2/3/4 可并行（各自独立模块，均不触碰生产基线口径）→ P1-5/6/8/9 按团队容量排期 → P2 系列列入路线图。

---

## 6. 假设与局限

1. **成熟度判断基于代码走读而非运行**：本报告未执行 `pytest` 或端到端运行验证（549 项测试绿为 README/pyproject 与文档声明口径），若运行结果与声明不符需复核。
2. **性能结论为定性判断**：未做基准测试；"日频够用、Tick 需编译内核"基于纯 Python 事件驱动架构的常识判断。
3. **开源项目对比以 PM 调研报告为唯一数据源**：未对 vn.py/WonderTrader/NautilusTrader/qlib/freqtrade 做源码级验证（PM 调研已声明此局限），建议落地 P0-2/P1-5 前对其关键机制（lookahead 分析实现、vnpy_ctp 接口）做一次源码核对。
4. **生产基线口径**：README 与 developer-guide 对生产组合权重（A30/B70 vs A10/B90）表述存在版本差异，本报告以"双引擎组合"为架构事实，不介入具体权重终裁。
5. **合规边界**：CTP 实盘涉及穿透式监管报备（AppID/AuthCode），属于业务/合规决策，架构侧只做能力准备。

---

*文档结束。关联输入：`01-pm-market-research.md`（产品经理调研）｜ 关联代码：`hexbroker/`（backtest/data/feature/forecast/risk/rl/evolution/live/paper/evaluation/pipeline.py）*
