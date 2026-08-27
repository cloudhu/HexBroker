# HexBroker 开源期货交易系统调研 · 主理人汇总裁决

**主理人**：齐活林（Qi）· 交付总监 | **日期**：2026-08-26 | **工作流**：📋 部分工作流（市场调研 + 架构评审）

---

## TL;DR

对开源社区 14 个主流开源期货/量化交易系统完成调研与架构级对比，结论：**HexBroker 训练侧成熟度高、交易侧成熟度低，主优化方向集中在「研究 → 实盘」链路**。主理人裁决：P0 四项（撮合可信度 / 前视自检 / 四层版本化 / Bootstrap 区间）立即立项，P1 五项按容量排期，P2 入路线图。

---

## 一、交付概览

| 项目 | 状态 | 产出 |
|---|---|---|
| 产品经理（许清楚）· 开源市场调研 | ✅ 完成 | 14 项目对比矩阵 + 8 条最佳实践（`01-pm-market-research.md`） |
| 架构师（高见远）· 架构对比与优化方向 | ✅ 完成 | 9 维度差距对比 + P0/P1/P2 优化清单（`02-arch-optimization.md`） |
| 主理人（齐活林）· 汇总裁决 | ✅ 完成 | 本报告 |

---

## 二、调研核心结论

### 2.1 开源格局（14 项目，PM 产出摘要）

- **中国期货原生**：vn.py（CTP 生态事实标准，万级 stars）、WonderTrader（C++ 内核 + 三级风控）、Hikyuu（组件化回测）、rqalpha（中国市场规则默认内建）、QUANTAXIS（QIFI/MIFI/VIFI 协议分层）
- **国际化期货**：NautilusTrader（Rust 内核 + 回测-实盘零改动）、QuantConnect LEAN（Alpha/Portfolio/Execution/Risk 四段式框架）、StockSharp（70+ 连接器产品化）
- **研究/回测**：qlib（Alpha158/360 因子库 + MLflow 可复现）、backtrader（撮合细节真实但已停维护）、zipline-reloaded、vectorbt（向量化扫描）、PyBroker（walk-forward 一等公民）、freqtrade（14 条撮合假设 + 四层风控 + 前视自检）

### 2.2 领域 8 大最佳实践（PM 提炼）

1. 回测-实盘同一执行语义（NautilusTrader/vn.py/WonderTrader）
2. 编译内核（Rust/C++）+ Python 控制面（NautilusTrader/WonderTrader/Hikyuu）
3. 风控分层、可插拔、可熔断的一等公民（WonderTrader/LEAN/freqtrade）
4. 前视偏差与"回测≠实盘"工程化自检（freqtrade/qlib/PyBroker）
5. 研究-回测-实盘一体化 + 可复现实验记录 Quant MLOps（qlib/NautilusTrader/LEAN）
6. 因子/特征资产化：表达式引擎 + 预置因子库（qlib/vn.py/Hikyuu）
7. 滑动窗口自适应训练为 ML 策略主流形态（PyBroker/FreqAI/Wt4ElegantRL）
8. 向量化研究引擎与事件驱动执行引擎分工 + 协议化开放架构（vectorbt/QUANTAXIS）

### 2.3 架构师对比结论

**总体判断**：HexBroker 是研究型纯 Python 期货框架。
- ✅ **训练侧成熟度高**：防泄漏红线（L1-L9）、walk-forward 66 折、RL-回测一致性 <1e-6、DSR/PBO 过拟合诊断、315 项测试 + CI + developer-guide v3.29，工程化优秀
- ⚠️ **交易侧成熟度低**：撮合同 bar 成交（已承认是已知限制）、CTP `_connect_gateway` 为 TODO、中国市场规则零散内建、无组合级风控、无可视化
- 📍 **差距集中点**：「研究 → 实盘」链路

---

## 三、主理人裁决（P0/P1/P2 采纳清单）

> 裁决依据：架构师 9 维度差距对比（含本地代码模块佐证）+ PM 开源对比数据 + 项目铁律（无证据不翻转、双闸门验收口径、315 测试全绿底线）。

### P0 — 立即立项（高收益低成本 / 高风险敞口）

| # | 方向 | 现状佐证 | 借鉴对象 | 采纳理由 |
|---|---|---|---|---|
| P0-1 | **撮合可信度加固** | 固定 1 tick 滑点 + 同 bar 成交（developer-guide 承认限制），绩效系统性偏乐观未量化 | backtrader / freqtrade 14 条撮合假设 | P18 multiplier 修复曾致 Sharpe -68%，撮合假设为同等级可信度风险；成本极低 |
| P0-2 | **前视/递归依赖自检工具** | 训练层防泄漏极强，但策略/特征代码无 lookahead/recursive-analysis | freqtrade | 把"防前视"从训练层红线扩展到策略层工程化自检 |
| P0-3 | **数据-信号-模型四层版本化** | 40+ 轮实验口径混淆（v2/v8 缓存、buggy/fixed broker、RAW_SCALE_FIX），根因是版本对应无系统记录 | qlib recorder / zipline bundle | 根治可复现性，支撑双闸门审计 |
| P0-4 | **Bootstrap 绩效区间** | 仅 DSR/PBO 点估计 | PyBroker | 补 block bootstrap 95% CI，让过线判定有区间支撑 |

### P1 — 按容量排期（中等收益）

| # | 方向 | 现状佐证 | 借鉴对象 | 采纳理由 |
|---|---|---|---|---|
| P1-5 | **CTP/SimNow 通道 + Gateway 抽象** | `ctp_skeleton.py` 的 `_connect_gateway` 为 TODO，无法真实下单 | vn.py（vnpy_ctp）+ BrokerGateway ABC | 补"研究→实盘"最后一环，与 paper 簿记形成回测-模拟-实盘三级闭环 |
| P1-6 | **因子库资产化 + 表达式 DSL** | 特征代码硬编码 25 特征，无因子注册/IC 档案 | qlib ExpressionEngine / Alpha158 | 特征资产化，支撑消融与组合验证 |
| P1-7 | **ML/RL 滚动重训调度 + 实验记录** | p23 手工调度 | FreqAI / qlib recorder | 滑动窗口重训自动化 |
| P1-8 | **风控规则接口化 + 组合/通道级** | RiskManager 优先级链写死，不可热插拔、无自成交防护 | LEAN 可插拔风控 + WonderTrader 组合盘 | 重构为 RiskRule ABC，底线：315 测试全绿 |
| P1-9 | **中国市场规则规则化** | 统一 12% 保证金无分品种表、无交割月限制 | rqalpha 规则默认内建 | 提升回测真实性 |
| P1-10 | **CI 补 ruff/black/mypy** | 配置已声明未接线 | — | 成本极低 |

### P2 — 入路线图（长线）

图表化报告（vectorbt）· 合约生命周期建模（NautilusTrader）· 自动因子挖掘（RD-Agent）· 事件溯源 · 紧急离合器 · 向量化研究引擎 · 编译内核（仅高频演进时评估）· Web 监控+通知（FreqUI/Telegram）

---

## 四、最值得立即动手 Top 3（架构师排序 → 主理人确认）

1. **P0-1 撮合可信度加固** — 撮合假设显式文档化 + next_bar_execution/volume_cap 开关 + 双口径对照。历史证据：P18 multiplier 修复曾致 Sharpe -68%，撮合假设是同等级可信度风险，成本极低。
2. **P0-3 四层版本化记录** — 根治 40+ 轮实验口径混淆（v2/v8 缓存、buggy/fixed broker），成本低。
3. **P1-5 CTP/SimNow 通道 + Gateway 抽象** — 补"研究→实盘"最后一环，形成回测-模拟-实盘三级闭环。

**建议实施顺序**：P0-1/2/3/4 可并行（不触碰生产基线口径）→ P1-5/6/8/9 按容量排期 → P2 入路线图。

---

## 五、决策依据与边界声明

1. **铁律约束**：所有优化不得触碰双闸门验收口径（方向准确率≥54%、OOS 计入成本、PBO<0.5、DSR>0）与防泄漏红线；任何缺陷状态翻转必须 fresh-eyes 重新独立验证。
2. **口径差异待仲裁**：README（A30/B70）与 developer-guide（A10/B90）对生产组合权重表述有版本差异，本次调研未介入，建议后续单独立项仲裁（与 P0-3 版本化联动）。
3. **合规为业务决策**：CTP 实盘涉及 AppID/AuthCode 穿透式监管报备，架构侧仅做能力准备（P1-5），是否开闸由业务方裁决。
4. **调研局限**：开源项目对比基于公开信息（stars 为量级判断）；架构师成熟度判断基于代码走读未运行验证；建议对 vn.py/WonderTrader/NautilusTrader/qlib/freqtrade 拉源码核对事件引擎、撮合模型、walk-forward 实现后再立项。

---

## 六、文件清单

| 文件 | 说明 |
|---|---|
| `deliverables/software-company/futures-research/01-pm-market-research.md` | 产品经理：14 项目调研 + 对比矩阵 + 8 条最佳实践 |
| `deliverables/software-company/futures-research/02-arch-optimization.md` | 架构师：9 维度差距对比 + P0/P1/P2 优化清单 |
| `deliverables/software-company/futures-research/03-lead-verdict.md` | 主理人：汇总裁决（本报告） |

**状态**：✅ 调研+对比+裁决全部完成 | 下一步：按 P0-1/2/3/4 立项（建议走标准 SOP，工程师+QA 闭环）。
