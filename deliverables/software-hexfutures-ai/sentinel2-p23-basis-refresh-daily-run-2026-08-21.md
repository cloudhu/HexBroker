# Sentinel-2 P23：基差 08-21 补拉 + 重出 08-21 计划 + 生产节奏自动化 + tail_ext/S4 对齐

- 项目：HexBroker（E:/Workspace/HexBroker）
- 阶段：P23（P20-P22 定案，QA VERIFIED 基础上的生产节奏自动化）
- 日期：2026-08-21
- 执行：工程师寇豆码（Kou），降级模式独立完成
- Python：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
- 状态：**IS_PASS: YES**

---

## 0. 执行摘要

| 任务 | 结果 | 说明 |
|---|---|---|
| P23-1 基差 08-21 补拉 | ✅ 17 品种 +1 日（SC 保留 04-30） | PandaData get_future_basis 实测成功，合并写回 + 备份 + applied 登记 |
| P23-1 重出 08-21 计划 | ✅ 空 → 非空 | 引擎 B 恢复信号 cu0/ta0 → ta0 3 手 / 176,958 CNY |
| P23-1 增量入账 | ✅ 边界如实标注 | 08-21 计划 pending（无 08-24 数据），账户不污染 |
| P23-2 生产自动化 | ✅ 一键跑通 + 幂等 | scripts/p23_daily_run.py，防重入按计划内容指纹 |
| P23-3 tail_ext/S4 对齐 | ✅ 共享窗口 corr=1.0，尾部为覆盖补充 | 追加信号质量 IC +0.083 / 命中率 0.677 |
| 落盘 | ✅ 全部就位 | 日志/计划/摘要/对比结果 |

---

## 1. P23-1 基差 08-21 补拉 + 重出计划 + 增量入账

### 1.1 基差 08-21 拉取与合并

- 数据源：PandaData `get_future_basis`（参数名 `symbol`，网关实测 CU/AU/M 等返回成功），
  请求 `symbol=[18 品种], start_date=20260821, end_date=20260821` → **17 品种返回，SC 源端缺失**。
- 持久化：`artifacts/p20_4_raw/seg_20260821_basis.json`（与 P20-4 同 schema：`{ok, method, params, result[]}`）。
- 合并：扩展 `scripts/p20_4_basis_update.py`（新增 `--seg/--src-file/--target-date` 参数，默认值=既有 P20-4 段，向后兼容）
  → `--seg 20260821 --src-file seg_20260821_basis.json --target-date 2026-08-21`
  - 17 品种各 +1 行（如 AU 1924→1925），写前备份到 `artifacts/backup_p204/`（`_prefill_20260821_201439.parquet`）。
  - SC 无 08-21 数据，保持 04-30，如实标注。
  - applied 登记：`artifacts/p20_4_basis_applied.json` 追加 17 条 `seg=20260821`（幂等键 (sym, seg)）。
- 完整性检查：17 品种基差最新 = **2026-08-21**（OK）；SC = **2026-04-30**（如实标注，源端缺失）。

日志：`artifacts/p23_basis_update.log`

### 1.2 重出 08-21 计划（空 → 非空）

`python scripts/p16_daily_signal.py --date 2026-08-21`

| 维度 | P21/P22 版本（旧） | P23 本次（新） |
|---|---|---|
| plan_note | `数据不完整：引擎B基差止于 2026-08-20（缺失 17 品种）…非真实空仓判断` | `''`（有持仓） |
| 引擎 B selected | `[]` | `['cu0', 'ta0']` |
| 引擎 B 明细 | - | cu0 br_rank=0.7698（名义 floor 0 手）；ta0 br_rank=0.9921（3 手） |
| 组合 positions | `[]` | `[ta0: 3 手 / 176,958 CNY / 组 chem_energy / 来源 B]` |
| 合计 | 0 手 / 0 CNY | 3 手 / 176,958 CNY（17.7% 权益） |
| 黑色系敞口 | 0%（PASS） | 0%（PASS，ta0 属 chem_energy） |
| 数据完整性 WARN | 18 条（17 品种基差过期 + sc0 断层） | 2 条（仅 sc0 基差过期 113 天 + sc0 断层——SC 源端历史缺数据） |

- 引擎 A 仍为 `cache_end`（fold 结构性截断，v8 缓存末信号日 2026-06-29）→ 组合退化为纯引擎 B（P6-3b 登记，如实标注）。
- 落盘：`artifacts/trade_plans/2026-08-21_sentinel2_plan.json/csv` + 同步 `trade_plans/2026-08-21_sentinel2_plan.json/csv`（P21/P22 约定位置）。
- 旧空计划备份：`artifacts/backup_p23/2026-08-21_sentinel2_plan_empty_prev.{json,csv}`。

### 1.3 p17 增量入账

`python scripts/p17_shadow_account.py --apply-date 2026-08-21`（从当前 apply 状态续接）

- 锚点：`p17_shadow_account_apply.json`（账目日期 2026-08-21，持仓 al0 2 / ni0 1 / ta0 3，现金 634,476.24）。
- MTM 推进：0 日（锚点已到 08-21）。
- 计划：08-21 现为非空 `[('ta0', 3)]`（3 手 / 1 品种）。
- T+1 开盘成交：**无次日交易日 → pending**（08-24 数据未到，计划不入账，不污染账户）。
- **增量入账边界（如实报告）**：08-21 非空计划已生成并登记为 pending，待 08-24 数据到达后 T+1 开盘执行
  （ta0 +3 手 @ 08-24 open；若 08-24 仍未到数据则持续 pending）。当前账户摘要不变：
  - 账目日期 2026-08-21 | 现金 634,476.24 | 持仓市值 512,600.64 | **权益(复利) 1,147,076.88**
  - 已实现盈亏 147,312.64 | 未实现盈亏 -235.76 | 持仓 al0 2 / ni0 1 / ta0 3

---

## 2. P23-2 生产节奏自动化一键脚本

### 2.1 交付：`scripts/p23_daily_run.py`

复用既有函数（p16 的 `engine_a_signal/engine_b_signal/combo_plan/write_plan/check_data_integrity`、
p17 的 `run_apply_date`、p20_4 的 `main/load_applied`），**不重写引擎逻辑**。流程：

1. **[1/5] 配置 + 数据**：`load_config("configs/base.yaml")` + `load_prices()` + `load_basis_panel()`。
2. **[2/5] --update-data（可选）**：执行基差增量合并（`p20_4_basis_update`，读取
   `artifacts/p20_4_raw/seg_{YYYYMMDD}_basis.json`）。K 线增量（p6_4 流程）因涉及 close_pcr
   口径换算与原始价换算系数、且依赖外部拉取，**不内嵌**（如实说明；在线拉取需 PandaData 网关
   token，CLI 无法直连，源端 JSON 需先经 MCP get_future_basis 持久化）。
3. **[3/5] 数据新鲜度检查**：18 品种 K 线 + 基差最新 vs 目标日（`--date` 或默认最新交易日），
   缺失/过期提示（不自动拉取）。
4. **[4/5] 出计划 + 增量入账**：p16 同口径出计划落盘（artifacts/trade_plans + 同步 trade_plans/）；
   p17 `run_apply_date` 增量入账（T+1 约定 + pending 标注）。
5. **[5/5] 摘要报告**：当日计划（合约/手数/名义/黑色系敞口）+ 模拟盘最新权益/持仓 + 数据新鲜度
   → stdout + `artifacts/daily_runs/{date}_daily_summary.md`。

### 2.2 防重入（幂等设计）

- 基差合并：p20_4 以 (sym, seg) 登记 applied → 天然幂等。
- 计划生成：覆盖写盘 → 天然幂等。
- **增量入账**：`artifacts/daily_runs/apply_registry.json` 登记
  `{plan_date: {plan_fp, applied_at, state, note}}`；`plan_fp` = 计划 JSON **去掉 `generated_at`
  易变字段**后的规范化 md5（否则每次重出时间戳都会变，破坏幂等——已在测试中发现并修复）。
  同 fp 重复运行 → 跳过入账（防重入）；计划内容变化（如基差刷新空→非空）→ 重新入账；
  `--force-apply` 强制重入（对照/审计用）。

### 2.3 验证（--date 2026-08-21）

- **完整跑通**：数据检查（WARN×2 仅 sc0）→ 计划（ta0 3 手 / 176,958 CNY）→ 入账（pending）→
  摘要落盘。耗时 ~3.9s/次。
- **幂等**：第 2 次运行 `入账: 防重入：2026-08-21 计划内容（fp=9fe4927eb0f2…）与已登记一致 → 跳过`，
  不重复入账。
- **--force-apply**：强制重新执行（仍 pending，账户不变）。
- **--update-data**：17 品种 `ALREADY_APPLIED` 跳过（幂等）+ SC 保持，随后正常出计划/入账。

摘要 md：`artifacts/daily_runs/2026-08-21_daily_summary.md`
（权益(复利) 1,147,076.88 / 持仓 al0 2·ni0 1·ta0 3 / 计划 ta0 3 手，均正确展示）。

---

## 3. P23-3 tail_ext 与 S4 对齐

### 3.1 p12 扩展成本评估

`p12_s4_shadow_monitor.py` 的 `build_baseline/run_monitor` 硬编码 `V2_PATH/RT30_PATH`，
但信号层函数（`load_signals/cache_metrics/corr_metrics/evaluate_trigger/evaluate_drift` 等）
均以 DataFrame/Path 为参，可复用。直接改 p12 需重构已 QA 的基线/复核逻辑（成本中），
且会引入与 P12 登记基线不一致的风险 → **选择独立对比脚本复用 p12 函数**（低风险），
并原生支持 `--cache-a/--cache-b` 任意两缓存对比。

交付：`scripts/p23_3_tail_ext_compare.py`（`--cache-a/--cache-b/--window/--json`）。

### 3.2 v8 vs v8_tail_ext 对比结果（63 窗滚动，OOS 2024-07-18 后）

| 指标 | v8 | v8_tail_ext | 解读 |
|---|---|---|---|
| 行数 / 覆盖 | 8,624 / 662 日（至 06-29） | 9,289 / 698 日（至 08-21） | tail_ext OOS 212 日 vs v8 176 日（+36 日） |
| 滚动截面 IC 末值 | -0.1990（@06-29） | -0.0502（@08-14） | tail_ext 尾部滚动 IC 明显改善（含 07/08 已实现） |
| 滚动命中率末值 | 0.416 | 0.496 | 同上 |
| 全窗口池化 Spearman | — | **1.0000**（n_pairs=900） | 共享窗口逐字节一致（P22 结论复现，无漂移） |
| 日截面滚动均值 | — | 1.0000 | 同上 |
| 尾部窗口（>06-29）池化相关 | — | **NaN（0 重合对）** | v8 尾部无信号行（fold 截断）→ 无法用相关性度量 |
| fresh | — | fresh_kline=YES, **fresh_signal=YES** | tail_ext 有 07/08 新信号 |
| 主触发 | — | NO_TRIGGER（对齐日 98，IC/命中连续胜 0/5） | 共享日期上两缓存指标完全相同 → 触发逻辑不适用 |
| 漂移判定 | — | **DRIFT_OK**（corr 1.0 ≫ 0.50） | 无漂移 |

**追加窗口质量（tail_ext 独有信号，已实现 fwd5）**：
648 行 / 36 日；31 个已实现 IC 日（2026-06-30 ~ 2026-08-14）；
**日均截面 IC = +0.0833；日均命中率 = 0.677**（≫ 0.50）→ 07/08 月追加信号实际质量良好，
与 P22 引擎 A OOS 1.064→1.096（+0.032）一致。

### 3.3 结论：tail_ext 在 S4 影子跟踪框架下的定位

1. **不是"分化候选"**：rt30（重训节奏变化）与 v2 池化相关仅 ~0.60，是真信号分化，需要漂移监测；
   tail_ext 共享窗口与 v8 **逐字节一致（corr=1.0）**，无任何漂移风险。
2. **是"覆盖恢复候选"**：tail_ext 的尾部不是与 v8"分化"，而是 v8 **结构性缺失**（fold 截断，
   06-29 后无信号）的**纯扩展补充**——尾部 0 重合对是构造使然，非漂移。
3. **监测定位建议**：
   - **纳入监测对象，但采用轻量模式**：以 `p23_3_tail_ext_compare.py` 的「追加窗口质量
     （tail IC / 命中率）」为核心指标，每次数据刷新后重跑即可（~1s）。
   - **不参与 rt30-vs-v2 的升级触发判定**（trigger 逻辑在共享日期上两缓存相同 → 天然 NO_TRIGGER，
     无信息量）。
   - tail_ext 升级决策仍走 P22 登记路径（QA 复核 + 主理人终裁），影子框架负责持续观测其
     追加信号质量是否保持正向。
4. **附注（P23 范围外观察）**：p12 `--monitor` 复核显示 rt30 在 fresh K 线（08-21 > 基线 08-17）
   下 IC 连续胜 7/5 → **UPGRADE_TRIGGER（fresh 确认=YES）**。此为 P12 框架的既有候选信号，
   不在 P23 决策范围，按 P12 规则应交 QA 复核 + 主理人终裁（如实记录，未做任何缓存修改）。

日志/结果：`artifacts/p23_s4_tail_ext.log` + `artifacts/p23_s4_tail_ext_result.json`

---

## 4. 最终裁决建议

1. **生产自动化可投产（YES）**：`scripts/p23_daily_run.py` 完整跑通 08-21、幂等（fp 防重入）、
   复用 p16/p17/p20_4 既有函数不重写引擎、摘要落盘完整。建议每日数据刷新后执行
   `python scripts/p23_daily_run.py`（基差源端 JSON 已持久化时可加 `--update-data`）。
   K 线增量拉取不在脚本内（需 p6_4 + 外部拉取），保留独立流程，如实说明。
2. **tail_ext 监测定位（建议）**：纳入影子跟踪监测对象（轻量模式，核心指标=追加窗口 IC/命中率），
   **不参与** rt30-vs-v2 的升级触发判定；升级决策继续走 P22 候选路径（QA + 主理人终裁）。
3. **08-21 计划执行边界**：非空计划（ta0 3 手）已登记 pending，待 08-24 数据到达后由
   `p23_daily_run.py --date 2026-08-21`（或 p17 --apply-date）自动 T+1 入账。

---

## 5. 运行输出与耗时

| 步骤 | 命令 | 耗时 |
|---|---|---|
| 基差 08-21 拉取 | PandaData MCP get_future_basis（17 品种返回） | ~1s |
| 基差合并 | p20_4_basis_update --seg 20260821 | ~2s |
| 重出 08-21 计划 | p16_daily_signal --date 2026-08-21 | ~8s |
| 增量入账 | p17_shadow_account --apply-date 2026-08-21 | ~5s |
| 自动化一键 | p23_daily_run --date 2026-08-21（含幂等验证 2 次 + force + update-data） | ~4s/次 |
| tail_ext 对比 | p23_3_tail_ext_compare（含 p12 monitor 复核） | ~2s |

### 落盘清单

- `artifacts/p23_basis_update.log`（基差合并日志）
- `artifacts/backup_p204/basis_*_prefill_20260821_201439.parquet`（17 份写前备份）
- `artifacts/p20_4_basis_applied.json`（+17 条 seg=20260821 登记）
- `trade_plans/2026-08-21_sentinel2_plan.json/csv`（更新：非空计划）+ `artifacts/trade_plans/` 同步
- `artifacts/backup_p23/2026-08-21_sentinel2_plan_empty_prev.{json,csv}`（旧空计划备份）
- `artifacts/daily_runs/2026-08-21_daily_summary.md`（自动化摘要）
- `artifacts/daily_runs/apply_registry.json`（防重入登记）
- `artifacts/p23_s4_tail_ext.log` + `artifacts/p23_s4_tail_ext_result.json`（tail_ext/S4 对比）
- `artifacts/shadow_account/p17_shadow_account_apply_2026-08-21.json`（增量入账快照，未覆盖基线）

### 变更文件

- `scripts/p20_4_basis_update.py`（修改：新增 --seg/--src-file/--target-date，向后兼容）
- `scripts/p23_daily_run.py`（新增：生产自动化一键脚本）
- `scripts/p23_3_tail_ext_compare.py`（新增：tail_ext/S4 对齐对比脚本，复用 p12 函数）

### 约束遵守

- 数据更新用既有脚本口径（备份 + applied 登记）✅；未改 hexbroker 包、未改 v8 缓存 ✅
- 自动化复用 p16/p17/p20_4 既有函数，不重写引擎逻辑 ✅；防重入 ✅
- 如实报告（SC 04-30 保留、08-21 计划 pending 边界、tail_ext 共享窗口 corr=1.0 尾部为覆盖补充而非分化）✅
- 口径铁律：复利口径 / OOS 2024-07-18 后（仅上下文标注）/ 修复后 broker / 生产 cap 口径 / 嵌套零泄漏 ✅

---

**IS_PASS: YES** — 代码与数据就绪，可交付 QA 复核。

---

## 7. QA 复核与主理人终裁（2026-08-21，QA VERIFIED）

QA 独立复核（`p23-QA-fresh-eyes-review-2026-08-21.md`）：**VERIFIED（17/17 独立复算 PASS）**——基差面板 17 品种 08-21（与 MCP 逐位一致）、08-21 计划 ta0 3 手/176,958 精确复现、增量入账 pending 权益 1,147,076.88、自动化防重入幂等、tail_ext 共享窗口 corr=1.0000/追加 IC +0.083。

### 主理人终裁（P23）

1. **P23 验收通过**：08-21 计划由空转非空（**ta0 3 手/176,958 CNY 纯 B**，WARN 18→2）；生产自动化 `p23_daily_run.py` 可投产（防重入幂等）；tail_ext 纳入轻量监测（不参与 rt30-vs-v2 升级触发）
2. **L2 修正登记（P24-1）**：防重入把 pending 计划也跳过——增强 `apply_plan`（仅 applied 跳过、pending 允许重试），否则 08-24 数据到达后 08-21 pending 计划无法自动入账
3. **L3 升级登记（P24-2）**：rt30 UPGRADE_TRIGGER（fresh K 线、IC 连续胜 7/5）正式升级为 QA+主理人终裁评估议题（P12 规则完整闭环）——**本轮仅登记，不做匆忙升级**（P20 暂缓裁决 + fresh_signal=NO 仍成立）
4. **生产节奏确认**：每日运行 = `p23_daily_run.py --date <最新数据日> --update-data`（基差增量）→ 摘要；K 线增量走 p6_4 独立流程

---
*P23 交付：sentinel2-p23-basis-refresh-daily-run + QA review；开发者指南 v3.27（§9.32）*
