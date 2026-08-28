# P2 双治理 + 落地（六方案 P2 项）· 产品经理增量 PRD

> 链路：PM（增量 PRD）→ Architect（增量设计+任务分解）→ Engineer（实现）→ QA（回归验证）→ 主理人终裁
> 上游裁决：`03-lead-verdict.md` 六方案定案 + `用户级 MEMORY` 治理铁律
> 铁律：无证据不翻转 / 双闸门验收口径 / 549 测试全绿底线 / feat+docs 双提交 / `git fsck --no-dangling` 空

## 0. TL;DR

P2 是六方案（8+通道策略治理体系）治理控制平面的**落地**批次。P0 双闭环（量纲修复+成本敏感性）已 ✅、P1 双验证（3B+2B）均 NOT_PASS。本批次把"治理规则"从文档**落地为可强制、可审计的代码**：

- **双治理**
  - **①防误开联锁（Interlock）**：方案从 `SHADOW_ONLY` 切到 `LIVE` 前，必须经 walk-forward QA 门禁且有有效 PASS 校准记录，否则**代码级硬拒绝**，强制回退 `SHADOW_ONLY`。根除"误开未过门禁方案上实盘"。
  - **②PASS 校准记录（Calibration Ledger）**：每个方案的晋升/降级决策、门禁证据（WR/n/MaxDD/净均收 vs 往返成本/PBO/DSR）、数据根因、主理人裁决、时间戳，落**结构化可审计账本**，可回放。
- **落地**
  - **③六方案治理寄存器 + Yaml 配置**：`configs/scheme_governance.yaml` 登记 1C/2A/3A/3B/1B/2B 当前定案（status + 数据根因 + 门禁指标）。
  - **④接线（startup 自检）**：`paper` 启动期对每个方案跑联锁自检，误配 → WARNING + 强制 shadow（fail-safe，绝不崩溃/绝不误上实盘）。

**验收口径（双闸门）**：闸门1 方向准确率≥54%；闸门2 OOS计成本+PBO<0.5+DSR>0。账本须含两闸门证据，且非 PASS 方案 status 必须 `SHADOW_ONLY`/`DEPRECATED`/`NOT_PASS`，联锁对其拦截 LIVE。

**默认零变更**：本批次**不改动任何既有交易/信号/风控逻辑**；寄存器/联锁/账本为纯增量模块，接线点仅在 startup 自检（fail-safe WARNING，不影响既有 tick 行为）。549 测试必须保持全绿（0 回归）。

---

## 1. 背景与问题（为何 P2）

| 来源 | 结论 |
|------|------|
| `03-lead-verdict.md` 六方案定案 | 1C✅跨年PASS、2A✅PASS晋升门禁；3A⛔DEPRECATED、3B❌赔率瓶颈、1B❌准入差、2B❌攻防不可兼得 |
| `用户级 MEMORY` 治理铁律 | "6方案影子模式默认OFF/SHADOW_ONLY，晋升需 walk-forward回测QA门禁+防误开联锁+PASS校准记录" |
| `用户级 MEMORY` 近期动态 | "P0双闭环已✅ … P1双验证均NOT_PASS … **P2双治理+落地推进中**" |

**痛点**：六方案的定案目前只存在于文档/内存，运行系统无"硬联锁"——若有人在 `paper.yaml`/`trade_plans` 把某方案误配为 `LIVE`（或新方案默认开），无代码层阻止其进实盘。账本也缺失，无法审计"谁、何时、凭什么 PASS/否决"。

**P2 目标**：把治理规则变成**可执行、可审计**的控制平面，且默认安全（shadow-first）。

---

## 2. 需求池（P2-1 ~ P2-4）

### P2-1 防误开联锁（Interlock）— 治理机制①
- **R1** 提供 `assert_safe_to_activate(scheme_id, registry) -> None`，对 `status != PASS` 或**无有效校准记录**的方案，抛出 `MisOpenBlocked(scheme_id, reason)`。
- **R2** 提供 `resolve_scheme_mode(requested_mode, scheme_id, registry) -> ResolvedMode`：`requested_mode in {live, trade}` 且联锁拒绝 → 返回 `shadow`（含 `reason`）；`requested_mode == shadow` → 原样返回 `shadow`。
- **R3** 默认 `enforce_shadow_default=True`：未显式登记（未知方案）一律视为 `SHADOW_ONLY`，拒绝 LIVE（fail-safe）。
- **R4** 拦截原因须可机读（`reason` 字段：枚举 `no_calibration` / `not_pass` / `deprecated` / `unknown_scheme`）。

### P2-2 PASS 校准记录账本（Calibration Ledger）— 治理机制②
- **R5** `CalibrationRecord` 结构化字段：`scheme_id, status, verdict, gate1_dir_acc, gate2_oos_cost, gate2_pbo, gate2_dsr, n, maxdd, net_pnl_vs_cost, data_root_cause, lead_verdict, calibrated_at, source_doc`。
- **R6** `CalibrationLedger` 支持 `load(path)` / `save(path)` / `append(record)` / `get(scheme_id)`，JSON 落盘（原子写 `.tmp`+`replace`，复用项目既有安全写约定）。
- **R7** 记录不可变追加（append-only）；同方案重复记录 → 新版本覆盖 `current`，旧版留痕。
- **R8** 账本须能被 QA 复跑校验（≤1e-9 字节级复算一致性）。

### P2-3 六方案治理寄存器 + Yaml 配置 — 落地①
- **R9** `SchemeStatus` 枚举：`PASS` / `SHADOW_ONLY` / `DEPRECATED` / `NOT_PASS`。
- **R10** `SchemeRegistry.load(yaml_path)` 解析 6 方案，字段含 `status` + `data_root_cause` + `gate_metrics`（可选）+ `calibration_ref`。
- **R11** `configs/scheme_governance.yaml` 登记当前定案（值取自权威源，见 §4）：
  - 1C → PASS（跨年）；2A → PASS（大盘分档，晋升门禁）；3A → DEPRECATED（买点时效）；3B → NOT_PASS（放量突破，n=183/WR49.7%/MaxDD劣化）；1B → NOT_PASS（试错，准入差）；2B → NOT_PASS（个股开关，攻防不可兼得）。
- **R12** 寄存器加载即 `verify_all()`：非 `SHADOW_ONLY` 方案必须有对应 `CalibrationRecord`（联锁前置不变量）。

### P2-4 接线（startup 自检）— 落地②
- **R13** 在 `paper` 启动路径（建议 `paper_trading_main.py` 或 `scheduler` 初始化）对每个已登记方案跑 `resolve_scheme_mode`；若请求 LIVE 被拦 → `log.warning` + 强制 `shadow`，**不抛异常、不阻断启动**。
- **R14** 自检结果汇总一行日志（PASS/N 可上实盘、SHADOW/M 强制影子），便于运维核对。
- **R15** 接线**零侵入**既有 tick 逻辑：仅影响"启动时方案 mode 决议"，不改信号/风控/撮合任何数值路径。

---

## 3. 接受标准（Definition of Done）

| 项 | 标准 |
|----|------|
| 联锁 | R1-R4 全满足；单测覆盖 4 类拦截 reason + 未知方案 fail-safe |
| 账本 | R5-R8 全满足；JSON 往返 + 追加幂等 + 字节级复算一致 |
| 寄存器 | R9-R12 全满足；6 方案正确加载；`verify_all` 对 NOT_PASS/DEPRECATED 要求校准记录 |
| 接线 | R13-R15 全满足；误配 → WARNING+shadow，启动不崩，tick 行为 0 变化 |
| 回归 | 全量 pytest 549→≥549 全绿（0 fail/0 error）；红线零顶层重依赖 |
| 默认零变更 | 既有交易/信号/风控代码 git diff 为空（仅新增 `governance/` + 配置 + 接线 WARNING 日志） |

---

## 4. 六方案定案数据（权威源，填账本）

| 方案 | 名称 | status | 数据根因（摘要） | 门禁指标（已知） |
|------|------|--------|------------------|------------------|
| 1C | 跨年 | PASS | 跨年持有结构通过 walk-forward | 晋升门禁已录（详见 2A 同类口径） |
| 2A | 大盘分档 | PASS | 大盘分档晋升门禁通过 | WR≥54% & OOS计成本+PBO<0.5+DSR>0 |
| 3A | 买点时效 | DEPRECATED | 买点时效维度失效，弃用 | — |
| 3B | 放量突破 | NOT_PASS | 赔率瓶颈：2025全年 n=183 OOS，WR 49.7% 达标但 MaxDD+均收益劣化 | n=183, WR=49.7%, MaxDD=劣化, 净均收=劣化, 维持 SHADOW_ONLY |
| 1B | 试错 | NOT_PASS | 准入差（entry 资格不达标） | 待 walk-forward QA 重跑补 WR/n |
| 2B | 个股开关 | NOT_PASS | 攻防不可兼得（offense/defense 互斥） | 待 walk-forward QA 重跑补 WR/n |

> 注：1B/2B 精确 WR/n 待 QA 重跑补全（R8 账本留 `pending_qa` 标记），不阻塞本批次（其 status 已为 NOT_PASS，联锁本就拦截 LIVE）。

---

## 5. 开放问题（Q1~Q5，待 Architect/主理人裁决）

- **Q1** 联锁拒绝时是"启动时 WARNING+强制shadow"（fail-safe）还是"启动即崩溃（fail-fast）"？→ 推荐 fail-safe（R13），避免误配致整系统停摆。
- **Q2** 账本落盘路径？→ 推荐 `data/governance/calibration_ledger.json`（与 `data/paper` 同根，运维可见）。
- **Q3** 寄存器 Yaml 与既有 `paper.yaml` 的 `symbols.mode` 是否合并？→ 不合并（维度不同：symbols.mode=合约级；scheme_governance=策略级）。本批次仅策略级。
- **Q4** 1B/2B 缺 WR/n，是否阻塞 P2？→ 不阻塞（status 已 NOT_PASS，联锁天然拦截）；账本标记 `pending_qa`。
- **Q5** 是否需要"降级"能力（PASS→SHADOW 运行时切换）？→ 本批次仅静态登记+启动自检；运行时动态降级留作后续批次（避免热路径侵入）。

---

## 6. 批次与排期（建议）

- 批次一（低风险提示，纯增量）：P2-3 寄存器+Yaml → P2-2 账本 → P2-1 联锁
- 批次二：P2-4 接线（仅 startup WARNING，零侵入）
- 验收：QA 全量回归 + 红线审计 + 默认零变更 diff 核对

---

## 7. 文件清单（预期产出）

- 代码：`hexbroker/governance/__init__.py`、`scheme.py`、`interlock.py`、`ledger.py`
- 配置：`configs/scheme_governance.yaml`
- 测试：`tests/test_scheme_governance.py`
- 接线：`paper_trading_main.py`（或 `scheduler.py`）startup 自检
- 文档：`14-p2-prd.md`（本）、`15-p2-arch.md`、`16-p2-impl.md`、`17-p2-qa.md`
