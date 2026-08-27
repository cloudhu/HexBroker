# 增量 PRD：HexBroker P1 六项优化（CTP 通道 / 因子资产化 / 重训调度 / 风控接口化 / 中国规则 / CI 静态检查）

- **产品经理**：许清楚（Xu）· 2026-08-26
- **文档类型**：增量 PRD（仅需求文档，不含代码）
- **范围**：P1 × 6（主理人裁决 `03-lead-verdict.md` 立项，架构师 `02-arch-optimization.md` 佐证；沿用全局编号 P1-5 ~ P1-10）
- **关联输入**：`01-pm-market-research.md`（开源对比）、`02-arch-optimization.md`（架构差距）、`03-lead-verdict.md`（裁决）、`04-p0-prd.md`（结构/风格参考）、`README.md`（验收口径/红线）
- **技术默认**：纯 Python（numpy/pandas）；增量抽象/配置层，不引入新重依赖；mlflow / pyqlib / vnpy_ctp 保持 optional 或仅留接口契约
- **测试基线说明**：P0 交付后既有测试基线已由 315 提升至 **549 项全绿**；本 PRD 所有"全绿底线"均以 **549** 为准（P0 PRD 写作时为 315，特此更新）

---

## 1. 产品目标

**一句话目标**：在**不触碰双闸门口径、防泄漏红线、549 项测试全绿**的前提下，把 HexBroker「研究 → 实盘」链路补齐到工程级——以**零依赖的 Gateway 抽象**完成 CTP 通道能力准备（真实开闸留待业务授权）、把因子资产化（表达式 DSL + IC 档案）、ML/RL 滚动重训与实验记录、风控规则可插拔化（组合/通道级）、中国市场规则规则化、以及 CI 静态检查补齐，使系统从"研究型框架"向"可插拔、可配置、可审计"演进。

**关键成功指标（KPI，全部可度量）**：

| 编号 | KPI |
|---|---|
| P1-5 | `BrokerGateway` ABC 定义且 `SimBroker`/`PaperBroker` 实现；`CTPLiveGateway` 守卫与凭证校验**不变**、`_connect_gateway` 仍为契约占位（不引 vnpy_ctp、不连真实柜台、不连需授权的 SimNow）；549 项测试全绿；新增 Gateway 契约测试 |
| P1-6 | DSL 可表达既有 25 特征中 ≥10 项且与硬编码数值一致；新增因子经配置注册（不改 `pipeline.py`）；因子 IC 档案可计算可缓存；549 项测试全绿 |
| P1-7 | 重训调度配置化替代 p23 手工节奏、可复现；模型版本可回滚；实验记录器复用 P0-3 四层指纹并落库可查；549 项测试全绿 |
| P1-8 | `RiskRule` ABC 定义且既有硬止损/S1–S5/预算/R1–R4/RL 意图迁移为规则列表（默认顺序与现状一致）；YAML 可增删/重排；组合目标合并层防自成交；**保 549 测试全绿** |
| P1-9 | `MarketRule` 配置表覆盖分品种保证金率/涨跌停幅度/交割月规则；默认 = 现状统一值（549 测试全绿不变）；分品种覆盖生效、交割月禁开仓生效 |
| P1-10 | CI 在 py3.11/3.12 下新增 `ruff check` + `black --check` + `mypy` 步骤且与 pytest 并行；549 项测试全绿 |

---

## 2. 用户故事

### P1-5 CTP/SimNow 通道 + Gateway 抽象
- 作为**架构师**，我希望定义统一的 `BrokerGateway` 抽象（connect/query/order/cancel）并把 `SimBroker`/`PaperBroker`/CTP 网关收敛到同一接口，以便未来接入 CTP/Femas/易盛时只加适配器、无需重构。
- 作为**研究员**，我希望现有 paper 簿记与未来的实盘通道共用同一 `execute` 契约，以便回测-模拟-实盘三级口径可交叉验证。
- 作为**合规官**，我希望本次里程碑只做架构能力准备（接口契约 + 零依赖抽象）、不引入 vnpy_ctp 重依赖、不连真实柜台与需授权的 SimNow，以便 CTP 实盘开闸作为独立业务/合规决策留待授权。

### P1-6 因子库资产化 + 表达式 DSL
- 作为**研究员**，我希望用表达式 DSL（如 `Ref(close,5)/close-1`）配置化定义因子，以便新增特征无需改 `pipeline.py` 并重跑全链路。
- 作为**研究员**，我希望每个因子自动产生 IC 档案（滚动 OOS IC）并缓存，以便支撑消融实验与组合验证。
- 作为**QA**，我希望 DSL 与既有 transformer 并存、向后兼容，以便 549 项既有测试与新因子产出数值一致。

### P1-7 ML/RL 滚动重训调度 + 实验记录
- 作为**ML 工程师**，我希望把 p23 手工重训节奏升级为可配置调度（retrain_freq / train_period / backtest_period），以便滑动窗口重训自动化、可复现。
- 作为**主理人 / 审计者**，我希望每次实验自动落四层指纹（数据/特征/模型/参数）+ 指标到实验记录器，并可回滚模型版本，以便 40+ 轮实验"口径混淆"问题不再复发。
- 作为**研究员**，我希望记录器是轻量且可选的（mlflow 或自研），以便不增加部署负担即可获得可查实验库。

### P1-8 风控规则接口化 + 组合/通道级
- 作为**量化研究员**，我希望风控规则以 `RiskRule` 接口化、可从 YAML 增删/重排，以便调整风控从"改代码"变为"改配置"，且为实盘落地做准备。
- 作为**风控官**，我希望组合目标合并层对多引擎同一品种目标做合并（自成交防护）并施加下单流控，以便多策略组合核算不产生自成交/超量。
- 作为**运维**，我希望规则迁移增量式（先加接口层、再逐条迁移）且每步保 549 测试全绿，以便重构回归风险可控。

### P1-9 中国市场规则规则化
- 作为**回测工程师**，我希望保证金率/涨跌停幅度/交割规则以 `MarketRule` 配置表按品种表达，以便回测口径对齐实盘分品种差异。
- 作为**研究员**，我希望 `CostModel`/`BacktestEngine`/`RiskManager` 统一从规则表读取，替代 cost.py/engine.py/calendar.py 的零散硬编码，以便新策略/新品种不再手工拼装规则。
- 作为**合规官**，我希望交割月禁开仓规则可配置生效，以便临近交割的策略风险在回测侧被显式约束。

### P1-10 CI 补 ruff/black/mypy
- 作为**工程师**，我希望 CI 接通已声明的 ruff/black/mypy 静态检查，以便每次回归自动拦截风格/类型回归。
- 作为**CI 维护者**，我希望静态检查与 pytest（549 项）在同一流水线并行，以便质量门禁完整且不拖慢测试反馈。

---

## 3. 需求池（P1 × 6）

> **通用红线约束（每项验收均须满足）**：
> - **不触碰双闸门口径**：方向准确率≥54%（闸门1）/ OOS 计入成本 + PBO<0.5 + DSR>0（闸门2）判定逻辑与判定结果不变；
> - **不触碰防泄漏红线**：SignalStore 只落 OOS 信号、RL 环境只读、L1–L9 防泄漏机制不变；
> - **549 项测试全绿底线**：改动后既有 549 项测试必须全绿，新增功能须补对应测试（总数 >549）。
>
> **P1-5 合规铁律（单独强调）**：本里程碑**明确 CTP 实盘开闸 = 业务/合规决策**；本 PRD 仅做架构能力准备——`BrokerGateway` ABC + `LiveBroker` 接口 + `SimBroker`/`PaperBroker` 接入抽象。**不引入 vnpy_ctp 重依赖、不接真实柜台、不接需账户授权的 SimNow**；真实 CTP/SimNow 接入列为后续业务授权项（见 §5 待确认问题 ① ②）。

### P1-5 CTP/SimNow 通道 + Gateway 抽象

| 项 | 内容 |
|---|---|
| **需求描述** | 定义 `BrokerGateway` ABC（connect/disconnect/query_position/query_account/submit_order/cancel_order）与 `LiveBroker` 协议（与 `SimBroker`/`PaperBroker` 同 `execute` 契约），把现有 `SimBroker`/`PaperBroker` 收敛为 Gateway 实现；`CTPLiveGateway` 保留既有守卫（`--i-understand-the-risk` + 环境变量凭证）与默认拒绝真实下单行为，`_connect_gateway` 维持"契约占位"（显式抛出"能力准备 / 未授权"错误）。**本次仅做零依赖的接口契约与抽象，不引入 vnpy_ctp、不连真实柜台、不连需授权的 SimNow**；真实接入列为后续业务授权项。借鉴 vn.py Gateway 插件化模式。 |
| **功能清单** | F5.1 `BrokerGateway` ABC：定义 connect/disconnect/query_position/query_account/submit_order/cancel_order 抽象方法契约。<br>F5.2 `LiveBroker` 协议：`execute(order)` 语义与 `SimBroker`/`PaperBroker` 一致，使回测-模拟-实盘三级共用同一执行契约。<br>F5.3 `SimBroker`/`PaperBroker` 适配实现 `BrokerGateway`（仅接口收敛，簿记/成本/保证金/平今/资金约束行为零改动）。<br>F5.4 `CTPLiveGateway`：`_connect_gateway` 维持契约占位（明确抛 `NotImplementedError` 或 "capability-prep only" 错误）；凭证校验与默认拒单守卫**原样保留**；模块顶层**不 import vnpy_ctp**。<br>F5.5 内存版 `FakeBrokerGateway`：供单测做 submit/query/cancel 往返验证，行为对齐 `SimBroker` 已知场景。<br>F5.6 可选 glue 占位：声明 `vnpy_ctp` 适配模块骨架，依赖缺失时显式 raise，仅作能力准备、不接线。 |
| **验收标准** | A5.1 `BrokerGateway` ABC 已定义；`SimBroker` 与 `PaperBroker` 实现该接口；既有 paper 相关测试**行为不变**、549 项全绿。<br>A5.2 `CTPLiveGateway` 仍需 `--i-understand-the-risk` + 环境变量凭证，默认拒绝真实下单（守卫保留）；模块顶层无 `import vnpy_ctp`（import 测试 / grep 检查通过）。<br>A5.3 `_connect_gateway` 为非功能性占位：调用时明确抛"未授权 / 能力准备"错误；**不建立任何真实柜台连接、不发起 SimNow 登录（无账户授权）**。<br>A5.4 单测：`FakeBrokerGateway` 的 submit→query→cancel 往返对已知场景与 `SimBroker` 输出一致。<br>A5.5 `pyproject.toml` [core] 无新增硬依赖；`vnpy_ctp` 仍仅声明于 optional 且不被 import。<br>A5.6 新增 Gateway 契约测试；既有 549 项测试全绿（总数 >549）；改动仅限 live/gateway 抽象层，SignalStore / OOS 物理隔离逻辑零改动——防泄漏红线不变。 |
| **借鉴对象** | vn.py（Gateway 插件化 + SimNow 模式）、NautilusTrader（适配器模式 + 回测/实盘同一执行语义） |

### P1-6 因子库资产化 + 表达式 DSL

| 项 | 内容 |
|---|---|
| **需求描述** | 借鉴 qlib `ExpressionEngine` 的轻量子集，定义 `factor_expr` 配置 DSL（复用现有 transformer 原语：ret/vol/ma/macd/rsi 等），支持"表达式 → 特征列"注册表 + 因子 IC 缓存；建立因子注册/IC 档案能力。既有 25 特征代码 transformer 与 DSL **并存、向后兼容**，不强制全量 DSL 化（范围见待确认 ⑦）。不引入完整 qlib 依赖（optional 已有 pyqlib，按需启用）。 |
| **功能清单** | F6.1 `factor_expr` DSL：用现有原语表达因子（如 `Ref(close,5)/close-1`、`Mean(volume,20)`），解析为与 `FeaturePipeline` 同语义的特征列。<br>F6.2 因子注册表：经配置（yaml/py）注册表达式因子，新增因子自动进入特征集，无需改 `pipeline.py`。<br>F6.3 因子 IC 档案：按品种计算滚动 OOS RankIC / IC，缓存到 sidecar（如 Parquet/JSON），可跨实验复用。<br>F6.4 向后兼容：既有 `FeaturePipeline` 白名单（`keep_features`）与 25 硬编码特征行为不变；DSL 因子可作为补充接入。<br>F6.5 表达式校验：非法表达式/未来函数原语（负 shift、center 滚动）在注册期报错或显式豁免。 |
| **验收标准** | A6.1 DSL 可表达既有 25 特征中 ≥10 项代表性特征，且与 `FeaturePipeline` 硬编码实现**数值一致**（<1e-9 等价测试）。<br>A6.2 因子注册表：仅经配置新增一个因子（不编辑 `pipeline.py`）即出现于特征集并参与管线。<br>A6.3 因子 IC 档案可对给定因子计算滚动 OOS IC 并缓存；固定输入下可复现（重算一致）。<br>A6.4 默认配置下既有 `FeaturePipeline` 行为与现状一致；549 项测试全绿（总数 >549）；DSL 出口不引入任意未来函数——防泄漏红线不变。<br>A6.5 无 qlib 硬依赖（pyqlib 仅 optional）；未安装时 DSL 不依赖 qlib 运行。 |
| **借鉴对象** | qlib（ExpressionEngine + Alpha158/360）、vn.py vnpy.alpha（Alpha158+LGBM 闭环）、Hikyuu（组件化因子） |

### P1-7 ML/RL 滚动重训调度 + 实验记录

| 项 | 内容 |
|---|---|
| **需求描述** | 把 p23 手工重训节奏升级为可配置调度（retrain_freq / train_period / backtest_period），实现滑动窗口自动重训；新增模型版本管理（registry + rollback）；新增实验记录器，复用 P0-3 四层指纹（数据/特征/模型/参数）落库，使实验可复现、可查、可回滚。记录器取 mlflow（optional 已声明）或自研轻量实现（见待确认 ④）。 |
| **功能清单** | F7.1 重训调度配置：从配置驱动 `ForecastTrainer` 逐折重训节奏（替代 p23 手工），支持 retrain_freq / train_period / backtest_period。<br>F7.2 模型版本管理：每轮产出带版本号与四层指纹的模型 artifact；支持 list/load/rollback。<br>F7.3 实验记录器：持久化四层指纹 + 关键指标（Sharpe/Calmar/DSR/PBO 等）；mlflow 或自研轻量存储（sidecar JSON/SQLite）。<br>F7.4 依赖门禁：mlflow 仅在 optional extra 安装时启用；未安装则自动回落自研轻量记录器，无硬依赖。<br>F7.5 报告联动：调度运行产出实验清单，可被双闸门报告引用（只读复用，不改性判定）。 |
| **验收标准** | A7.1 调度配置可驱动滚动重训在 demo/合成数据上自动完成，且与手工等价结果可复现（固定 seed 一致）。<br>A7.2 模型 registry 记录版本并支持 rollback：回滚后加载的模型与历史版本字节/数值一致（回滚测试）。<br>A7.3 记录器持久化四层指纹 + 指标，可按 run_id 查询；复用 P0-3 指纹结构（兼容测试）。<br>A7.4 未安装 mlflow 时自动用自研轻量记录器、流程不报错；安装 mlflow 时可切换（可选 extra）；无 mlflow 硬依赖。<br>A7.5 549 项测试全绿（总数 >549）；记录器为只读元数据层，不改信号语义——防泄漏红线与双闸门口径不变。 |
| **借鉴对象** | freqtrade FreqAI（滑动窗口重训调度）、qlib（MLflow recorder + 可复现工作流） |

### P1-8 风控规则接口化 + 组合/通道级

| 项 | 内容 |
|---|---|
| **需求描述** | 借鉴 LEAN 可插拔风控模型与 WonderTrader 组合盘，定义 `RiskRule` ABC（`evaluate(state, intent) → adjustment`），把现有优先级链（硬止损 > S1–S5 > 预算 > R1–R4 > RL 意图）重写为规则列表（**默认顺序与现状一致**），支持 YAML 增删/重排；新增组合目标合并层（在引擎 A/B 目标之上合并同一品种目标，防自成交）+ 下单流控。迁移策略：**先加接口层、再逐条迁移规则，每步保 549 测试全绿**（见待确认 ③）。 |
| **功能清单** | F8.1 `RiskRule` ABC：`evaluate(state, intent) → adjustment`，统一规则契约。<br>F8.2 规则迁移：将硬止损/S1–S5/波动率目标·Kelly 预算/R1–R4 恢复/RL 意图重写为 `RiskRule` 实例，默认顺序与原优先级链一致。<br>F8.3 YAML 配置：增/删/重排规则，默认配置保持现状决策等价。<br>F8.4 组合目标合并层：引擎 A/B 同一品种目标合并为组合目标（防自成交），复用于现有 `combo_plan` 雏形。<br>F8.5 通道级风控：单笔订单量限制（pre-trade 流控）；自成交防护（合并后无同品种反向自成交）。<br>F8.6 增量迁移脚手架：接口层先行，规则逐条迁移并配回归测试门禁（每步要求 549 绿）。 |
| **验收标准** | **A8.0（底线）保 549 测试全绿**：默认配置下 `RiskManager` 对既有风控测试集的决策与迁移前**完全一致**（数值/布尔等价回归测试），既有 549 项测试全绿。<br>A8.1 `RiskRule` ABC 已定义；既有硬止损/S1–S5/预算/R1–R4/RL 意图已迁移为规则列表，默认顺序与现状一致；**保 549 测试全绿**。<br>A8.2 YAML 增删/重排规则生效：新增 no-op 规则决策不变；删除/重排某规则行为按文档变化（配置测试）。<br>A8.3 组合目标合并层：多引擎同品种目标正确合并、无自成交；单笔下单单量受流控上限约束（自成交防护 + 流控测试）。<br>A8.4 迁移过程逐条保绿：每迁移一条规则即运行 549 全绿门禁；最终 549 项测试全绿（总数 >549）。<br>A8.5 默认配置下 RL in-the-loop 风控语义不变（env.step 行为一致）；SignalStore / OOS 隔离逻辑零改动——防泄漏红线不变。 |
| **借鉴对象** | QuantConnect LEAN（可插拔风控模型）、WonderTrader（资金/流控/账户三级 + 组合盘防自成交）、freqtrade（四层风控漏斗）、NautilusTrader（pre-trade 检查） |

### P1-9 中国市场规则规则化

| 项 | 内容 |
|---|---|
| **需求描述** | 借鉴 rqalpha「中国市场规则默认内建」模式，定义 `MarketRule` 配置表（每品种：保证金率 / 涨跌停幅度 / 交易时段 / 交割规则），使 `CostModel`/`BacktestEngine`/`RiskManager` 统一从表读取、替代 cost.py/engine.py/calendar.py 的零散硬编码常量。首批落三项：**分品种保证金率 + 涨跌停幅度 + 交割月禁开仓**（见待确认 ⑤）；默认 = 现状统一值（12% 保证金、无幅度覆盖），确保 549 测试全绿不变。同步将 paper `TradingSession` 时段表提升为全系统共享。 |
| **功能清单** | F9.1 `MarketRule` 配置表：每品种含 margin_rate / limit_up / limit_down / trading_session / delivery_rule 等字段；缺省回退现状统一值。<br>F9.2 `CostModel` 读取分品种保证金率（替代 `margin_rate` 统一常量）。<br>F9.3 `BacktestEngine`（engine.py P8 拦截）读取分品种涨跌停幅度。<br>F9.4 `RiskManager`/下单流读取交割规则：交割月禁开仓。<br>F9.5 时段表共享：paper `TradingSession` 提升为系统级，消除 data/calendar 与 paper/sessions 重复实现。 |
| **验收标准** | A9.1 `MarketRule` 表存在且含分品种保证金率/涨跌停幅度/交割规则；**默认配置 = 现状统一值**，549 项测试全绿不变（数值一致 <1e-12）。<br>A9.2 分品种覆盖生效：`CostModel` 对 au 与 cu 取不同保证金率时，持仓成本/保证金计算随之不同（差异测试）。<br>A9.3 涨跌停幅度：engine.py 拦截按品种幅度生效（分品种幅度测试）。<br>A9.4 交割月禁开仓：处于交割月的品种开仓请求被拒绝（交割月规则测试）；非交割月不受影响。<br>A9.5 549 项测试全绿（总数 >549）；规则化为只读配置读取，不改信号语义——防泄漏红线与双闸门口径不变。 |
| **借鉴对象** | rqalpha（A 股/期货规则默认内建、Mod 插件化）、vn.py（CTP 行情含涨跌停/持仓量字段）、NautilusTrader（合约生命周期字段） |

### P1-10 CI 补 ruff/black/mypy

| 项 | 内容 |
|---|---|
| **需求描述** | 在 `.github/workflows/ci.yml` 接通已声明但未接线的静态检查：`ruff check` + `black --check` + `mypy`，与现有 pytest（py3.11/3.12 × 549 项）并行。严格度策略见待确认 ⑥（black 宽松 `--check`、mypy 渐进 strict）。成本极低、防回归。 |
| **功能清单** | F10.1 CI 步骤：新增 `ruff check` / `black --check` / `mypy` 三步，复用 pyproject 已声明配置。<br>F10.2 严格度策略：black 以 `--check` 宽松（仅校验格式，不强制重构历史代码）；mypy 渐进（非全量 strict，先覆盖增量模块）。<br>F10.3 门禁：静态检查失败则 CI 失败；与 pytest 549 项并行不互相阻塞。<br>F10.4 基线处理：首轮接通时通过配置（per-file-ignores / 豁免）使存量代码 CI 绿，避免一次性大改。 |
| **验收标准** | A10.1 CI 在 py3.11/3.12 下运行 `ruff check` + `black --check` + `mypy`，且流水线可绿（首轮基线豁免策略下）。<br>A10.2 静态检查与 pytest 并行；549 项测试全绿仍由 CI 守护。<br>A10.3 新增代码被静态检查覆盖（增量模块 mypy 无新增 error）；存量豁免范围有记录（config 可读）。<br>A10.4 不引入新运行依赖；仅启用已声明工具，CI 时长增量可控（<原有 pytest 时长量级）。 |
| **借鉴对象** | 通用工程实践（freqtrade 等 CI 质量门禁） |

---

## 4. 优先级与依赖关系

- **优先级**：六项均为 **P1**（主理人裁决中等收益档）；其中 **P1-10 成本最低、风险最低**，建议最先动手；**P1-8 回归风险最高**，需逐规则保绿，建议后置批次。
- **六项间依赖**：
  - **无硬依赖**：P1-5（Gateway 抽象）、P1-6（因子 DSL）、P1-8（风控接口）、P1-9（中国规则）、P1-10（CI）模块边界独立，**可并行开发**。
  - **软依赖**：P1-7（重训调度 + 记录器）**复用 P0-3 四层指纹**（P0 已交付），属只读复用，无改动冲突；记录器也可被 P1-6（因子 IC 档案）间接受益，但非阻塞。
  - P1-8 内部存在**强顺序**：先加 `RiskRule` 接口层 → 再逐条迁移规则，每步保 549 绿（非与其他项依赖，但与自身回归门禁强绑定）。
- **是否可并行**：是。六项为独立模块，可多工程师并行；唯一协同点是 P1-10（CI）建议先行以尽早给其余五项提供静态检查门禁。
- **建议分批（低风险先行、CTP 合规项后置）**：
  - **批次一（低风险先行）**：P1-10（CI 静态检查）→ P1-9（MarketRule，纯配置化）→ P1-6（因子 DSL，并存兼容）。
  - **批次二（中等、零依赖能力准备）**：P1-5（Gateway 抽象，零依赖，**真实 CTP/SimNow 接入明确后置**）→ P1-7（重训调度 + 记录器，复用 P0-3）。
  - **批次三（回归风险较高，逐规则保绿）**：P1-8（RiskRule 接口化 + 组合盘）。
  - **CTP 合规后置**：真实 CTP/SimNow 接入（含 vnpy_ctp 依赖、穿透式监管报备）**不纳入本里程碑**，列为后续业务授权项（见 §5 ① ②）。
- **共享约束**：六项均遵守「不触碰双闸门口径 / 防泄漏红线 / 549 测试全绿」铁律（见 §3 顶部）；P1-8 另设 A8.0 保绿底线。

---

## 5. 待确认问题

1. **P1-5 是否引入 vnpy_ctp 外部依赖？** 建议本次**仅做 Gateway 抽象与接口契约（零依赖）**，真实 CTP/SimNow 接入待业务授权——是否同意？（影响 pyproject 是否新增 hard/optional 依赖与 `_connect_gateway` 是否留 TODO）。
2. **P1-5 是否需要本次接入 SimNow 仿真账号？** SimNow 需期货公司开户与账号授权，涉及真实网络联调——是否同意本次**不接 SimNow**，仅以 `FakeBrokerGateway` 做契约验证？
3. **P1-8 RiskRule 迁移顺序？** 建议"先加接口层、再逐条迁移规则、每步保 549 测试全绿"——是否同意该增量策略与保绿底线？是否允许首轮仅迁移硬止损/S1–S5 两条做样板？
4. **P1-7 记录器选型？** 用 mlflow（optional 已声明）还是自研轻量 recorder（sidecar JSON/SQLite）？建议默认自研轻量、mlflow 作为可选后端——是否同意？
5. **P1-9 MarketRule 首批落哪些字段？** 建议首批 = 分品种保证金率 + 涨跌停幅度 + 交割月禁开仓；是否还需首轮纳入交易时段/夜盘跨日（已由 paper `TradingSession` 覆盖）？
6. **P1-10 lint 严格度？** black 建议 `--check` 宽松（仅格式）、mypy 建议渐进 strict（增量模块先覆盖、存量 per-file 豁免）——是否同意该严格度，或要求首轮即全量 strict？
7. **P1-6 DSL 表达范围？** 是否要求 25 特征**全部** DSL 化，还是仅"新增因子走 DSL + 既有 25 保留代码"并存（推荐并存，向后兼容、降低风险）？
8. **P1-5 行情 Gateway 是否纳入？** 本次 Gateway 抽象聚焦"交易通道"（BrokerGateway）；paper 新浪实时行情是否同步抽象为可插拔 `MarketDataGateway`（与 `DataSource` 对齐），或留待后续？建议本次仅交易通道、行情源维持现状。

---

*文档结束。本 PRD 为需求文档，不含实现代码；交付后由工程师按 §4 分批排期，QA 按 §3 验收标准逐项验收。所有项均不触碰双闸门验收口径、防泄漏红线、549 项测试全绿。CTP 实盘开闸为独立业务/合规决策，本里程碑仅做架构能力准备。*
