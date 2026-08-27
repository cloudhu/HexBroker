# HexBroker P1 六项优化：实现纪要（11-p1-impl.md）

> 配套：09-p1-prd.md（需求池）· 10-p1-arch.md（增量设计+任务分解）· 12-p1-qa.md（QA 独立复核）
> 治理铁律：IS 段定参 / OOS 一次性终裁；QA 独立裁决；"无证据不翻转"。
> 提交纪律：feat+docs 双提交；任何 `.git` 操作后 `git fsck --no-dangling` 必须空（EXIT:0）。

---

## TL;DR

- **六项全部落地**：T01(P1-10 CI) / T02(P1-9 MarketRule) / T03(P1-6 因子 DSL) / T04(P1-5 Gateway) / T05(P1-7 重训调度+记录) / T06(P1-8 RiskRule 接口化+组合盘)。
- **全量回归**：597 个测试点 = 586 passed + 11 skipped，**0 failed / 0 error**（基线 549 → 新增 48，全绿，无回归）。
- **红线审计**：所有 `vnpy_ctp` / `mlflow` / `scipy` / `ForecastTrainer` 均为**函数内 try import**，无任何顶层硬依赖；CTP/SimNow 真实接线**受控延后**至业务/合规授权。
- **默认等价性**：P1-8 / P1-9 / P1-6 在默认（不传参 / 空规则表）下与迁移前数值一致（<1e-12），由 A8.0 回归测试钉死。
- **状态**：✅ **IS_PASS = YES**（工程师实现 + 全量回归 + 红线审计 + 默认等价验证均通过），待 QA 独立复核（12-p1-qa.md）→ 主理人终裁 → feat+docs 双提交。

---

## 1. 任务映射与批次

| 任务 | 对应项 | 归属批次 | 风险 | 新增测试 |
|---|---|---|---|---|
| T01 | P1-10 CI（ruff/black/mypy） | 批次一·低 | 低 | —（配置） |
| T02 | P1-9 MarketRule（保证金/涨跌停/交割月） | 批次一·中 | 中 | test_market_rule（11） |
| T03 | P1-6 因子 DSL + 注册表 + IC 档案 | 批次一·中 | 中 | test_factor_dsl（9） |
| T04 | P1-5 Gateway 抽象（回测/模拟/实盘三级） | 批次二·中 | 中 | test_live_gateway（9） |
| T05 | P1-7 重训调度 + 实验记录 | 批次二·中 | 中 | test_ml_schedule（5）+ test_ml_recorder（4） |
| T06 | P1-8 RiskRule 接口化 + 组合目标合并 | 批次三·高 | 高 | test_risk_rules（10） |
| **合计** | | | | **48 新增 / 0 失败** |

---

## 2. 文件清单

### 2.1 新增模块（23 文件）

**P1-5 Gateway 抽象**
- `hexbroker/live/gateway.py` — `BrokerGateway`(ABC) / `LiveBroker`(Protocol) / `Position`·`Account`·`Order` / `FakeBrokerGateway`·`SimBrokerGateway`(薄包 SimBroker)·`PaperBrokerGateway`(薄包→execute_plan)。零依赖。
- `hexbroker/live/vnpy_ctp_glue.py` — `build_vnpy_ctp_gateway()`：仅函数内 `try import vnpy_ctp`，未装即抛 `RuntimeError`，真实接线为后续里程碑（红线纪律）。

**P1-6 因子库资产化**
- `hexbroker/factor/__init__.py` / `dsl.py` / `registry.py` / `ic.py` — 自研 DSL 引擎（零依赖 qlib）、注册表、滚动 OOS RankIC 档案。

**P1-7 重训调度 + 记录**
- `hexbroker/ml/__init__.py` / `schedule.py` / `registry.py` / `recorder.py` — `RetrainScheduler`（编排 `ForecastTrainer`，零改训练逻辑）、`ModelRegistry`、`JsonRecorder`/`SqliteRecorder`/`MlflowRecorder`(可选后端)。

**P1-8 风控规则接口化**
- `hexbroker/risk/rules.py` — `RiskRule`(ABC) / `RiskContext` / `RiskAdjustment` / 5 个 concrete rule / `build_default_rules()`。
- `hexbroker/risk/combo.py` — `ComboTargetMerger` / `EngineTarget`（防自成交 + 单笔流控）。

**P1-9 中国市场规则**
- `hexbroker/market/__init__.py` / `rule.py` / `session.py` — `MarketRule` / `MarketRuleTable` / `day_label`（共享交易日历）。

**配置 + 测试**
- `configs/`：factors.yaml · market_rules.yaml · retrain.yaml · risk_rules.yaml
- `tests/`：test_factor_dsl.py · test_live_gateway.py · test_market_rule.py · test_ml_recorder.py · test_ml_schedule.py · test_risk_rules.py

### 2.2 修改文件（11 文件）

| 文件 | 项 | 改动性质 |
|---|---|---|
| `.github/workflows/ci.yml` | P1-10 | 修复重复 `lint:` 键；单 `lint` 任务跑 `ruff/black/mypy`（scope: factor/market） |
| `pyproject.toml` | P1-10 | `[tool.ruff.lint]` select E9/F/I001；`[[tool.mypy.overrides]]`：因子/market 严格，其余豁免 |
| `hexbroker/live/ctp_skeleton.py` | P1-5 | `CTPLiveGateway(BrokerGateway)`；新增 `CTPCapabilityPrepOnly`；抽象方法抛 `CTPGuardError` |
| `hexbroker/live/__init__.py` | P1-5 | 导出 BrokerGateway/LiveBroker/各网关/build_vnpy_ctp_gateway |
| `hexbroker/market/`（接入点） | P1-9 | `backtest/cost.py`（保证金读规则表）、`backtest/engine.py`（涨跌停+交割月禁开仓）、`paper/sessions.py`（day_label 委托 market.session） |
| `hexbroker/feature/pipeline.py` | P1-6 | `__init__` 增 `factor_registry=None`；非 None 时按名求 DSL 列拼接 `f_<name>`；默认 None → 零变化 |
| `hexbroker/risk/manager.py` | P1-8 | `evaluate` 改为遍历 `self.rules` 填 `RiskContext`；硬止损 veto 提前返回；ATR ratchet 按品种隔离记忆 |
| `hexbroker/risk/__init__.py` | P1-8 | 导出 RiskRule/RiskContext/RiskAdjustment/build_default_rules/ComboTargetMerger/EngineTarget |

---

## 3. 各任务实现要点

### T01 / P1-10 — CI 静态检查（低风险·批次一先行）
- 修复 `ci.yml` 中**重复 `lint:` 作业键**（YAML 重复键会静默覆盖，属缺陷）；保留单 `lint` 作业，安装 `ruff black mypy`，仅对 `hexbroker/factor` `hexbroker/market` 严格检查。
- `pyproject.toml`：全量 `hexbroker.*` `ignore_errors=true`（存量豁免）；`factor`/`market` 及子包 `ignore_errors=false` + `strict=true`（增量严检）。
- **渐进式**：首轮仅 `select = ["E9","F","I001"]`，避免存量噪声阻断；后续批次（live/ml/risk）按需纳入 scope。

### T02 / P1-9 — MarketRule（默认等价）
- `MarketRuleTable` 默认 = `MarketRule(symbol="*", margin_rate=0.12)`；缺规则文件 → `default()` 空表 → 全部回退统一 0.12。
- 接入点：`CostModel.margin()` → `market_rules.margin_rate(sym)`（缺表用 `self.margin_rate`）；`BacktestEngine` 按 `limit_up/limit_down` 对 prev_close 拦截 + `allows_open(ts)` 交割月禁开仓。
- **等价证明**：默认路径下 `margin_rate` 恒为 0.12、limit 恒 None、allows_open 恒 True → 与迁移前数值完全一致（<1e-12）。

### T03 / P1-6 — 因子 DSL（默认不生效）
- 自研 AST 引擎（借鉴 qlib `ExpressionEngine` 思路，**零依赖 qlib**）：白名单算子、`Ref(x,n)` 拒绝负 shift（未来函数）、滚动窗口右端对齐。
- 原语复用 `feature/technical.py` 语义（`_mp` 窗口阈值、RSI Wilder 近似、MACD ema12-ema26），保证与 25 硬编码特征口径对齐。
- `feature/pipeline` 默认 `factor_registry=None` → **不加载任何 DSL 因子**，行为零变化；仅显式传入才拼接 `f_<name>`。

### T04 / P1-5 — Gateway 抽象（零依赖）
- `BrokerGateway` ABC 统一 connect/disconnect/query_position/query_account/submit_order/cancel_order。
- `LiveBroker` Protocol 约定 `execute(symbol, target_qty, ref_price, ts)`，与 `SimBroker.execute`/`PaperBroker.execute_plan` 同语义 → 回测-模拟-实盘可互换。
- `SimBrokerGateway`/`PaperBrokerGateway` 为**薄包**（不改被包对象代码）；`FakeBrokerGateway` 内存实现供单测往返。
- `CTPLiveGateway`/`build_vnpy_ctp_gateway` 真实接线**受控延后**（穿透式监管报备为后续业务授权项），现仅能力预备骨架。

### T05 / P1-7 — 重训调度 + 记录（不重造指纹/训练）
- `RetrainScheduler.run` 委托既有 `ForecastTrainer.run`（**零改训练/防泄漏逻辑**），仅做编排与资产化：注册模型到 `ModelRegistry` + 经 `ExperimentRecorder` 记录。
- 指纹复用 P0-3 `compute_four_layer`；调用方传入 `fingerprint` 则直接用，否则首模型 `_params` 最佳努力推导。
- `JsonRecorder`/`SqliteRecorder` 自研 sidecar（原子写 tmp+`os.replace`）；`MlflowRecorder` 仅函数内 `try import mlflow`，无硬依赖。

### T06 / P1-8 — RiskRule 接口化（高回归风险·批次三）
- `evaluate` 重构为遍历 `self.rules` 填同一 `RiskContext`；默认 `build_default_rules(cfg)` 顺序 = **硬止损 → RL 意图 → 回撤恢复 → 风险预算 → S1–S5 卖出**。
- 硬止损 `veto=True` 提前 return（等价迁移前 early return）；S1–S5 置 target=0。
- **数值等价证明**（已逐行比对迁移前代码）：
  - 迁移前：`target = position_within_limit(intent*scalar, min(abs(budget), max_pct))`；卖出信号 → `target=0`；`kelly_fraction=budget`。
  - 迁移后：RLIntent(target=intent)→Recovery(target=intent*scalar)→Budget(cap=min(abs(budget),max_pct)→`position_within_limit`)→SellEngine(target=0)。
  - 两路径对 `position_within_limit` 的入参**完全同值**，且 `kelly_fraction` 同为 `budget_target(...)` 调用 → 数学等价。
- `ComboTargetMerger`：同品种多引擎净目标（反向抵消），物理上杜绝同品种自成交；`check_flow` 单笔 `|qty|` 流控。

---

## 4. 偏差记录（vs 10-p1-arch.md）

| 偏差 | 描述 | 处置 |
|---|---|---|
| ci.yml 重复 `lint:` 键 | 原稿存在两处 `lint:` 作业（YAML 重复键静默覆盖） | 删除冗余块，保留单 lint 作业（已实现时修复） |
| 训练逻辑未重写 | arch 明确"折叠逻辑在 `ForecastTrainer.run` 内" | 遵守：调度器仅编排，零改训练业务，双闸门口径不动 |
| ATR ratchet 按品种隔离 | 迁移时一并修复 ag0/rb0 交替 evaluate 的止损串扰 | 新增 `_ratchets`/`_prev_stops` 按 `state.symbol` 隔离，回归测试通过 |

> 其余均按 10-p1-arch.md 与 09-p1-prd.md 验收口径落地，无功能性偏离。

---

## 5. 验证结果（证据）

| 验证项 | 命令/来源 | 结果 |
|---|---|---|
| 全量回归 | `pytest -q`（addopts=-q，仅进度点） | 597 点 = 586 passed + 11 skipped，0 failed/error |
| 新测试逐文件 | 6 个 test_*.py 单独跑 | 9+9+11+4+5+10 = 48/48 ✅ |
| 红线 audit | grep `import vnpy_ctp\|import mlflow\|import scipy` 顶层 | 全部为函数内 try import，无顶层硬依赖 ✅ |
| 默认等价（P1-8） | test_risk_rules 含迁移前后同输入同输出断言 | 通过（A8.0 钉死） |
| 默认等价（P1-9） | test_market_rule 默认表 = 0.12/None/True | 通过 |
| 默认等价（P1-6） | pipeline 不传 factor_registry → 输出不变 | 通过（549 基线全绿佐证） |
| CTP 受控 | `test_present_appid_authcode_allows_start` 仍绿 | ✅（骨架不破坏既有守卫） |

---

## 6. 主理人裁决占位

- **IS_PASS：✅ YES**
- 理由：六项全部实现 + 全量 597 测试绿（0 失败）+ 红线（零顶层重依赖/受控 CTP）+ 默认等价（P1-8/9/6）三方验证均通过。
- 待办：QA 独立复核（12-p1-qa.md）→ 主理人终裁 → feat+docs 双提交 + `git fsck --no-dangling` 空。
- **超出范围（明确延后）**：P1-5 真实 CTP/SimNow 接线（需期货账户/AppId + 穿透式监管报备，业务/合规授权后另立里程碑）。

---
*文档结束。本纪要记录 P1 工程师实现与验证，不含 QA 裁决；最终入库以主理人终裁 + 双提交为准。*
