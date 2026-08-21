# Sentinel-2 P24 — apply_plan 防重入增强 + rt30 正式升级评估

- 工程师：寇豆码（Kou）
- 项目：HexBroker（E:/Workspace/HexBroker）
- 阶段：P24（P23 QA L2 修正项 + L3 升级项闭环）
- 日期：2026-08-21
- Python：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（pandas 3.0.3 / numpy 2.5.2 / scipy 1.18.0）
- 执行模式：降级模式（团队工具不可用），独立实现 + 如实报告

---

## 一、P24-1 apply_plan 防重入增强（P0，QA L2）—— 已完成 ✅

### 1.1 问题定位（源码核对）

防重入（apply_registry 检查）的实际位置是 **`scripts/p23_daily_run.py::apply_plan`**（任务描述写的
p17_shadow_account.py 为入口语义——`apply_plan` 内部调用 `p17.run_apply_date` 执行入账；registry 登记/
跳过判定在 p23 编排层）。旧逻辑：

```python
if not force and prev and prev.get("plan_fp") == fp:
    return {"applied": False, "reason": "防重入：... → 跳过", ...}
```

**缺陷**：按 fp 一致即跳过，**不区分 `state`（applied/pending）** → 08-21 pending 计划（fp 与登记一致）
在 08-24 数据到达后无法自动入账（P23 QA L2 修正项）。

### 1.2 增强实现（`scripts/p23_daily_run.py`）

新增判定函数 `_should_skip_apply(prev, fp, force) -> (skip, reason)`，规则：

| 场景 | 判定 |
|---|---|
| `force=True` | 不跳过（强制重入） |
| `prev` 为空 | 不跳过（首次） |
| fp 不一致 | 不跳过（计划内容变化，重新入账） |
| `state == "applied"` 且 fp 一致 | **跳过**（幂等） |
| `state == "pending"` 且 fp 一致 | **不跳过**（数据到达后允许重试入账）← 修复核心 |
| 无 `state`（legacy） | 按 `applied` 保守跳过（迁移规则见下） |

状态流转（沿用既有 snapshot.pending 语义，无需改动 p17）：
- 首次 apply 无 T+1 数据 → p17 记 pending → registry `state=pending`
- 数据到达后重试 → p17 可成交 → 执行入账 → registry `state=applied`
- 仍无数据 → 保持 `state=pending`

向后兼容迁移（`_legacy_state`）：旧 registry 记录可能无 `state` 字段 → 按 `applied` 处理（保持旧版
"fp 一致即跳过"语义，避免对可能已入账的计划重复入账污染账户）；**带 `state` 的记录（含 08-21 pending）
按实际状态处理**。现有 `apply_registry.json` 的 08-21 记录 `state=pending` → 新逻辑正确允许重试。

### 1.3 验证输出（`scripts/p24_1_apply_retry_test.py` → `artifacts/p24_apply_test.log`）

**Part A — `_should_skip_apply` 单元测试（9/9 PASS）**
```
[PASS] 首次入账（无记录）                        → skip=False
[PASS] applied+同fp → 跳过                        → skip=True
[PASS] applied+异fp → 重入                        → skip=False
[PASS] pending+同fp → 重试（修复核心）             → skip=False
[PASS] pending+异fp → 重入                        → skip=False
[PASS] legacy无state+同fp → 保守跳过              → skip=True
[PASS] legacy无state+异fp → 重入                  → skip=False
[PASS] force+applied → 强制重入                   → skip=False
[PASS] force+pending → 强制重入                   → skip=False
```

**Part B — 状态机流转（临时 registry + mock p17，不改真实数据；5/5 PASS）**
```
[PASS] B1 首次apply(无T+1) → state=pending (expect pending)
[PASS] B2 数据到达重试 → state=applied (expect applied)      ← pending→applied 流转
[PASS] B3 applied+同fp → 跳过（幂等防重入）
[PASS] B4 force 强制重入 applied → 执行
[PASS] B5 legacy无state+同fp → 保守跳过
```

**Part C — 真实生产路径（`p23_daily_run.py --date 2026-08-21`，修复生效）**
```
运行前 registry[2026-08-21] = {"plan_fp": "9fe4927eb0f2…", "applied_at": "2026-08-21T20:17:41",
                               "state": "pending", "force": true}
→ 入账: 已执行增量入账（state=pending）        ← 不再"防重入跳过"
运行后 registry[2026-08-21] = {"plan_fp": "9fe4927eb0f2…", "applied_at": "2026-08-21T20:36:50",
                               "state": "pending", "force": false}
允许重试（非防重入跳过）: True | 仍保持 pending（无 08-24 数据）: True
```
- 无 08-24 数据 → pending 保持（正确：不入账、不污染账户；权益 1,147,076.88 不变、0 成交）
- 直接 `p17 --apply-date 2026-08-21` 两次：均 pending、0 成交（幂等）
- 计划 fp 复跑后稳定 `9fe4927e…`（内容未变）

**结果：PASS（Part A 9/9 + Part B 5/5 + Part C 真实验证）**

---

## 二、P24-2 rt30 正式升级评估（P1，QA L3，P12 规则闭环）—— 已完成 ✅

### 2.1 评估口径（严格当前生产口径）

- 配置：`load_config("configs/base.yaml")` + 显式传参 `top_k=0.30 / min_symbols=3 / group_cap=0.5 /
  group_map(base.yaml)`（§9.18 部署接线标准）
- 修复后 broker：BacktestEngine + CostModel（滑点1tick + 费0.005% + 保证金12% + CONTRACTS18）
- 复利口径，OOS 严格 `2024-07-18` 后
- 引擎 B：win252/thr0.7；组合：A30/B70（vol_target=False）
- 块自助只比较 OOS 段（嵌套零泄漏）
- 产出：`artifacts/p24_rt30_compare.csv` + `artifacts/p24_rt30_bootstrap.json`

### 2.2 覆盖率对比（关键发现：rt30"覆盖更长"仅 3 个品种）

| 指标 | v8 | rt30 |
|---|---:|---:|
| 信号行数 | 8,624 | 6,066 |
| 唯一信号日 | 662 | 837 |
| 起止 | 2019-04-01 ~ 2026-06-29 | 2019-03-04 ~ 2026-07-27 |
| 日均品种（全样本） | 13.03 | 7.25 |
| OOS 信号日 | 176 | 310 |
| OOS 日均品种 | **13.09** | **4.49** |
| OOS S2 资格日（≥3 品种） | **160** | 143 |
| 2026-07 信号 | 0 行 / 0 日 | 18 行 / 7 日（仅 ag0/au0/m0） |
| 2026-08 信号 | 0 行 / 0 日 | 0 行 / 0 日 |

**逐品种末信号日（rt30 vs v8）**：rt30 仅 **ag0/au0/m0** 三个品种延伸到 07-24/27（晚于 v8 的 06-23/24，
+31~33 天）；其余 **15 个品种 rt30 末信号日集中在 01-06 ~ 02-06（比 v8 早 140~174 天）**。

**如实结论**：rt30 缓存"止 07-27 覆盖更长"仅在**缓存 max-date 层面**成立，**逐品种层面其覆盖优势极窄**
（3/18 品种），整体 OOS 覆盖率显著低于 v8（日均 4.49 vs 13.09 品种；S2 资格日 143 vs 160）。因此
"恢复 07/08 月信号"的说法**不成立**——rt30 在 07 月仅 3 个品种 18 行信号，08 月 0 行，与 v8 同为真空。

**尾折扩展（P22 方法）是否执行**：任务条件为"若其覆盖优势显著可考虑"——经逐品种量化，覆盖优势
**不显著**（仅 3 品种），且主评估已给出明确负向结论（见 2.3/2.4），故**不执行** rt30 尾折扩展；评估
口径保持清晰：**rt30 原始缓存 vs v8**（未引入 rt30+tail_ext 变体）。此决定如实登记。

### 2.3 完整回测对比（生产 cap 口径，同窗口）

| 项目 | v8 | rt30 | Δ(rt30−v8) |
|---|---:|---:|---:|
| 引擎 A S2 全样本 Sharpe | 0.875 | 0.709 | −0.166 |
| 引擎 A S2 **OOS Sharpe** | **1.064** | **0.544** | **−0.520** |
| 引擎 A S2 OOS 收益 | +11.59% | +5.85% | −5.74pp |
| 引擎 A S2 OOS seg1（2024H2） | +0.068 | **−0.741** | −0.809 |
| 引擎 A S2 OOS seg2（2025+） | +2.079 | +1.671 | −0.408 |
| 组合 A30/B70 全样本 Sharpe | 1.081 | 1.033 | −0.047 |
| 组合 A30/B70 **OOS Sharpe** | **0.717** | **0.545** | **−0.172** |
| 组合 A30/B70 OOS 收益 | +6.74% | +5.07% | −1.67pp |
| 纯 B 参考 OOS Sharpe | 0.482 | — | — |

（v8 基线复现：引擎 A S2 OOS 1.064 / 组合 0.717，与 P22/P23 登记一致 ✅）

### 2.4 块自助检验（block=21，B=5000，seed=42，OOS 段日收益差）

| 对象 | 均值日差 (rt30−v8) | se | t | p_one_sided | p_two_sided | 95%CI (%/d) |
|---|---:|---:|---:|---:|---:|---:|
| 引擎 A S2 | **−0.0105 %/d** | 0.0144 | −0.73 | 0.757 | 0.467 | [−0.0393, +0.0175] |
| 组合 A30/B70 | −0.0031 %/d | 0.0043 | −0.73 | 0.757 | 0.467 | [−0.0118, +0.0053] |

- 方向为**负**（rt30 均值日差低于 v8），95% CI 覆盖 0 → rt30 不显著优于 v8
- 滚动 60 日 Sharpe 胜率：rt30 仅胜 **43.5%** 窗口（446 窗）
- 日波动：引擎 A rt30 0.345%/d vs v8 0.331%/d（无 P7 时期的"降波动"优势）

### 2.5 与 P7 先例的关系（如实对比）

| 维度 | P7（旧数据/旧配置） | P24-2（当前生产口径） |
|---|---|---|
| 对比基线 | v2 缓存 + A15/B85 | **v8 缓存 + A30/B70 + cap 0.5 + min3** |
| 引擎 A OOS | −0.337 → +0.053（rt30 改善） | **1.064 → 0.544（rt30 大幅恶化）** |
| 组合 OOS | 0.999 → 1.138（rt30 改善） | **0.717 → 0.545（rt30 恶化）** |
| 块自助 p | p≈0.49（不显著，方向正） | **p=0.757（不显著，方向负）** |
| 覆盖率 | rt30 低于 v2（−24% 行） | **rt30 远低于 v8（日均 4.49 vs 13.09）** |
| 结论 | 不投产，仅影子候选 | **不通过（当前口径）→ 维持 v8** |

**机制解释**：P7 的 rt30 优势建立在 v2 基线（覆盖差、OOS IC 弱）之上，且当时 A15/B85 组合权重对 A 引擎
暴露小；当前生产基线 v8 覆盖率与 OOS 表现均大幅优于 v2，rt30 在 v8 面前无信号覆盖优势、OOS 分段
（尤其 seg1 −0.741）显著恶化 → 升级不成立。

**关于 p12 UPGRADE_TRIGGER 的重要澄清**：p12 S4 monitor 的触发是 **rt30 vs v2**（P7 影子基线）的
滚动 IC/命中率比较，**不是 vs v8**。触发成立只说明"rt30 在近期窗口优于 v2"，而生产相关比较
（rt30 vs v8，即本 P24-2 评估）结论为**负向**。故该触发**不支持**升级；按 P12 规则完成正式评估后
登记"rt30 评估不通过（当前口径）"，关闭该触发议题（后续若重建 rt30 缓存或 v8 出现信号恢复，
可重新评估——登记为未来项）。

### 2.6 升级裁决建议

**维持 v8，登记"rt30 评估不通过（当前口径）"**（需主理人终裁确认）：
- rt30 引擎 A S2 OOS 0.544 < v8 1.064（Δ−0.520）；组合 A30/B70 OOS 0.545 < v8 0.717（Δ−0.172）
- 块自助均值日差方向为负（p_one_sided=0.757，双侧 p=0.467）→ 无统计显著改善
- 覆盖率不占优（OOS 日均品种 4.49 vs 13.09；15/18 品种末信号日早于 v8 5 个月）
- 不执行尾折扩展（覆盖优势不显著 + 主结论已明确负向）

---

## 三、全局一致性复核（交叉文件）

- **P24-1**：`p23_daily_run.py` 新增 `_legacy_state` / `_should_skip_apply`，`apply_plan` 调用判定函数；
  `p24_1_apply_retry_test.py` 通过 mock + monkeypatch 验证，不改 hexbroker/p17/v8/rt30。
- **P24-2**：`p24_rt30_upgrade_eval.py` 复用 p5/p3/p22 既有函数（`engine_a_targets_cs`、
  `run_engine_row`、`combo_stats_row`、`seg_sharpe`），显式 base.yaml + 显式传参，与 P22 生产口径一致；
  v8 基线复现逐位一致（1.064 / 0.717）。
- **数据流**：compare CSV / bootstrap JSON / apply_test.log 均落盘 `artifacts/p24_*`；报告完整引用。
- **变更范围**：`scripts/p23_daily_run.py`（+54/−7）；新增 `scripts/p24_1_apply_retry_test.py`、
  `scripts/p24_rt30_upgrade_eval.py`；`hexbroker/` 0 diff；v8/rt30 缓存无变更；`trade_plans/2026-08-21_*`
  复跑内容与既有一致（fp 稳定）。

## 四、交付清单

| 文件 | 说明 |
|---|---|
| `scripts/p23_daily_run.py` | P24-1 防重入增强（pending 允许重试；legacy 迁移） |
| `scripts/p24_1_apply_retry_test.py` | P24-1 三层验证（单元 + 状态机 + 真实路径） |
| `scripts/p24_rt30_upgrade_eval.py` | P24-2 完整回测 + 块自助 + 裁决 |
| `artifacts/p24_apply_test.log` | P24-1 验证日志 |
| `artifacts/p24_rt30_compare.csv` | P24-2 对比表 |
| `artifacts/p24_rt30_bootstrap.json` | P24-2 块自助 + 裁决 JSON |
| `artifacts/p24_rt30_upgrade_eval.log` | P24-2 运行日志 |
| `deliverables/software-hexfutures-ai/sentinel2-p24-apply-retry-rt30-eval-2026-08-21.md` | 本报告 |

## 五、IS_PASS 判定

**IS_PASS: YES**

- P24-1：pending 允许重试（修复核心）✅ | pending→applied 流转 ✅ | applied 幂等跳过 ✅ |
  legacy 迁移保守 ✅ | 真实生产路径验证 ✅
- P24-2：完整回测对比（v8 基线复现 1.064/0.717）✅ | 覆盖率对比（逐品种真相）✅ |
  块自助 p=0.757 方向负 → rt30 不通过 ✅ | 升级裁决建议（维持 v8，登记不通过）✅
- 约束遵守：不改 hexbroker 包、不改 v8/rt30 缓存；嵌套零泄漏（自助仅 OOS 段）；如实报告 ✅

### Known Issues / 后续项（不阻塞）
1. **rt30 未重建**：本评估基于 P7 期缓存（止 07-27，且 15/18 品种末信号日早于 v8）。若主理人希望
   评估"重建后的 rt30"（test_len=30 全量覆盖至数据端），需触发 `p7._build_rt30_cache` 级重训
   （耗时较长），届时重新评估。
2. **p12 monitor 基线口径**：建议后续把 S4 影子跟踪基线从 v2 更新为 v8（生产相关比较），避免
   UPGRADE_TRIGGER 对非生产基线产生误导性触发。
3. **08-24 数据到达后**：`p23_daily_run.py --date 2026-08-21`（或 --date 2026-08-24 含 08-21 计划）
   将自动把 08-21 pending 计划执行入账并转 applied（本 P24-1 修复已生效）；无需 --force-apply。

---

## 7. QA 复核与主理人终裁（2026-08-21，QA VERIFIED）

QA 独立复核（`p24-QA-fresh-eyes-review-2026-08-21.md`）：**VERIFIED（11/11 独立验证 PASS，无 DISCREPANCY）**——防重入增强逐位验证（legacy 迁移/pending 重试/applied 幂等三场景）；rt30 交叉验证精确复现（引擎 A 1.064 vs 0.544、组合 0.717 vs 0.545、块自助 p=0.757）；覆盖率发现独立确认（15/18 品种 rt30 末信号日早 140-174 天、仅 ag0/au0/m0 延至 07-24/27）。

### 主理人终裁（P24）

1. **P24-1 验收通过**：防重入增强生效——**08-24 数据到达后 `p23_daily_run --date 2026-08-21` 自动入账，无需 --force-apply**；legacy 迁移保守跳过恰当
2. **P24-2 rt30 评估不通过（当前生产口径）**：维持 v8 生产；**"覆盖更长不成立"是关键发现**（rt30 15 品种反而早于 v8，影子触发对照的是弱基线 v2）
3. **P25 待办登记**：① **p12 影子基线 v2→v8**（影子监控对照生产基线才有意义；切换时留意对齐日减少）② "重建后 rt30"并入 v13 自然重建评估（不单独立项）③ v13 自然跨边界缓存（数据累计 ~20-24 交易日后）
4. **方法论沉淀**：影子触发须对照生产基线（v8）而非历史影子基线（v2）；候选升级评估必须当前生产口径（P7 先例反转的教训）

---
*P24 交付：sentinel2-p24-apply-retry-rt30-eval + QA review；开发者指南 v3.28（§9.33）*
