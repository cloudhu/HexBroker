# QA 独立复核报告 — HexBroker P23（基差 08-21 补拉 + 重出计划 + 生产自动化 + tail_ext/S4 对齐）

- 复核人：严过关（Yan，QA，fresh-eyes 独立复核）
- 项目：HexBroker（E:/Workspace/HexBroker）
- 阶段：P23（P20-P22 定案基础上的生产节奏自动化）
- 日期：2026-08-21
- Python：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（pandas 3.0.3 / numpy 2.5.2 / scipy 1.18.0 / pyarrow 25.0.1）
- 复核方式：降级模式（团队工具不可用），独立读取 parquet/json + 自写代码复算 + 真实复跑（未调用 p23 实现函数计算关键指标）
- 复核脚本：`deliverables/software-hexfutures-ai/qa_p23_independent_verify.py`（PASS=17 FAIL=0）
- 复跑日志：`artifacts/qa_p23_independent_verify.log`、`artifacts/qa_p23_daily_run_R1.log`、`artifacts/qa_p23_daily_run_R2.log`、`artifacts/qa_p23_daily_run_update.log`

---

## 1. 静态审查

### 1.1 p20_4_basis_update.py 扩展（--seg/--src-file/--target-date）
- 三参数默认值=既有 P20-4 段（`SEG_DEFAULT=20260818_20260820`、`SRC_FILE_DEFAULT=seg_20260820_basis.json`、`TARGET_DATE_DEFAULT=2026-08-20`），不传参即原行为 → **向后兼容成立**。
- 幂等键 `(sym, seg)`：`applied` 登记命中即 `ALREADY_APPLIED` 跳过，无重复合并。
- 备份：写前 `backup()` → `artifacts/backup_p204/basis_{sym}_prefill_{ts}.parquet`（17 份 `_201439` 实测存在）。
- 原子写回：tmp + `replace()`，无半写状态。
- VERIFY 段用 `--target-date` 与各品种 `date.max()` 比较，SC 如实标 `!! (止于 2026-04-30)`。
- 结论：✅ 通过。

### 1.2 p23_daily_run.py（生产自动化一键脚本）
- 复用方式正确：p16 `engine_a_signal / engine_b_signal / combo_plan / write_plan / check_data_integrity`、p17 `run_apply_date`、p20_4 `main / load_applied`，**未重写引擎逻辑**（源码核对，仅编排）。
- 防重入：`plan_fp = md5(plan_json 去 generated_at 后 sort_keys 规范化 JSON)`。独立验证：当前计划去 `generated_at` 后 md5 = `9fe4927eb0f2fe8b8dc6e96e874b060b`，与 `apply_registry.json` 登记一致 → **去时间戳的幂等设计正确**。测试日志证实修复前缺陷（test1/test2 均执行入账）→ 修复后 fp 稳定跳过。
- `--update-data` 边界：仅基差增量（读 `artifacts/p20_4_raw/seg_{YYYYMMDD}_basis.json` → p20_4 main 合并）；K 线增量（p6_4 流程，涉及 close_pcr 口径换算 + 外部拉取）**不内嵌、如实说明** —— 文档化依赖，非隐藏缺陷。
- `--force-apply`：强制重入（对照/审计用），registry 记录 `force=true` 如实。
- 摘要落盘：`artifacts/daily_runs/{date}_daily_summary.md`，含计划/权益/持仓/新鲜度。
- 结论：✅ 通过。

### 1.3 p23_3_tail_ext_compare.py（tail_ext/S4 对齐）
- 复用 p12 函数与常量（`load_close_panel / load_signals / cache_metrics / corr_metrics / evaluate_trigger / evaluate_drift / OOS_START / HORIZON / ROLL_WINDOW / TOP_K / MIN_SYMBOLS / CORR_ALERT ...`），原生 `--cache-a / --cache-b`（默认 v8 vs v8_tail_ext）。
- 尾部窗口判定：`tail_window_corr` 复用 overlap 合并逻辑，`<10` 对返回 NaN 并如实标注 0 重合对；`tail_quality` 用 p12 `add_fwd_returns / daily_cs_ic / daily_hit_rate` 直接刻画追加信号质量。
- 结论：✅ 通过。

### 1.4 git diff 范围
- 修改（tracked）：仅 `scripts/p20_4_basis_update.py` + `trade_plans/2026-08-21_sentinel2_plan.{json,csv}` + 公众号文章（非 P23 范围，历史遗留）。
- 新增（untracked）：`scripts/p23_daily_run.py`、`scripts/p23_3_tail_ext_compare.py`、P23 报告、本文档。
- `hexbroker/` 包：**0 diff**；`signals_cache18_grouped_v8.parquet` 等缓存：**无任何变更**（git status 无 signals_cache 条目）。
- 结论：✅ 变更范围符合声明（新脚本 + p20_4 扩展 + 报告；v8 缓存/hexbroker 未动）。

---

## 2. 独立复跑验证（真实输出）

### 2.1 基差面板独立确认（读 parquet）
```
[PASS] 17 品种基差最新=2026-08-21  | {'sc0': '2026-04-30'}
[PASS] SC 基差最新=2026-04-30（源端缺失保留）
[PASS] TA 08-21 行与 MCP 返回值一致  | basis=358.0000/358.0 ratio=5.878489/5.878489327 spot=6090.0000/6090.0
[PASS] CU 08-21 行与 MCP 返回值一致  | basis=260.0000/260.0 ratio=0.241232/0.24123214 spot=107780.0000/107780.0
```
- 源端 `seg_20260821_basis.json` 17 条（SC 缺失）；`p20_4_basis_applied.json` 34 条 = `{20260818_20260820: 17, 20260821: 17}`；备份 17 份。✅

### 2.2 08-21 计划独立复算（自写代码，未调 p23/p16 函数）
```
[PASS] ta0 br_rank@2026-08-21 ≈ 0.9921  | 独立计算 br_rank=0.9921
[PASS] cu0 br_rank@2026-08-21 ≈ 0.7698  | 独立计算 br_rank=0.7698
ta0: floor(210000/58,986)=3 手
cu0: floor(210000/793,413)=0 手
[PASS] ta0 名义 ≈ 176,958 CNY  | notional=176,957.86
[PASS] 总名义/权益 = 17.7%
[PASS] 计划退化纯B=True（a_status=cache_end, cache_max=2026-06-29）
[PASS] 计划组合=ta0 3 手/来源B
```
- 组合口径：`notional_b = 1e6 × 0.30 × 0.70 = 210,000`（生产 cap 口径 A30/B70）→ 复现一致。✅

### 2.3 p17 增量入账边界（真实复跑 `--apply-date 2026-08-21`）
```
T+1 开盘成交: 无次日交易日 → **pending**（计划不入账，不污染账户）
账目日期   : 2026-08-21 | 现金 634,476.24 | 持仓市值 512,600.64
权益(复利) : 1,147,076.88 | 已实现盈亏 147,312.64 | 未实现盈亏 -235.76
持仓       : {'al0': 2, 'ni0': 1, 'ta0': 3}
成交笔数   : 0 | pending=True
```
- 与声明逐位一致；账户无污染（pending 不产生成交）。✅

### 2.4 生产自动化真实复跑
- 第 1 次：`入账: 防重入：2026-08-21 计划内容（fp=9fe4927eb0f2…）与已登记一致（state=pending，applied_at=2026-08-21T20:17:41）→ 跳过`（耗时 3.7s）。
- 第 2 次：同样跳过（fp 稳定，即使计划 `generated_at` 每次重出变化）。
- `--update-data`：17 品种 `已应用`（ALREADY_APPLIED）→ `合并 0 个品种` → 继续出计划/摘要完整。
- 摘要 `daily_runs/2026-08-21_daily_summary.md`：计划 ta0 3 手 / 176,958 / 17.7%；权益 1,147,076.88；持仓 al0 2·ni0 1·ta0 3 —— 与声明一致。✅

### 2.5 tail_ext/S4 对齐（复跑 + 独立复算）
- 复跑 `p23_3_tail_ext_compare.py`：
```
全窗口池化63窗 Spearman = 1.0000 (n_pairs=900) | 日截面滚动均值 = 1.0000
尾部窗口(>2026-06-29)池化 Spearman = NaN —— 0 重合对（v8 尾部无信号行）
tail_ext 追加窗口质量: 648 行 / 36 日 | 已实现 IC 日 31（2026-06-30~2026-08-14）| 日均IC=0.083325 | 日均命中率=0.677419
[fresh] K线新: YES | 信号新: YES | 主触发 NO_TRIGGER | 漂移 DRIFT_OK
```
- 独立复算（读 tail_ext parquet + close panel 直接算）：`648 行/36 日 / 31 IC 日 / mean IC 0.083325 / mean hit 0.677419` —— 与声明逐位一致。✅
- 独立抽样：共享窗口 8,624 行 `exp_ret` 与 v8 逐字节一致；尾部 0 重合对。✅

### 2.6 附注 rt30 UPGRADE_TRIGGER（真实复跑 `p12 --monitor`）
```
主触发: UPGRADE_TRIGGER (IC连续胜=7/5, 命中率连续胜=3/5, 对齐日=44, fresh确认=YES)
K 线最新: 2026-08-21 (基线 2026-08-17) | 新 K 线: YES
```
- 与附注声明一致。✅

---

## 3. 关键裁决

### 3.1 08-21 计划非空（ta0 3 手）是否可接受为生产计划
**裁决：可接受（VERIFIED）。**
- 信号真实性：ta0/cu0 的 br_rank 由更新后的 08-21 基差面板独立复算一致（0.9921/0.7698）；组合按生产 cap 口径（A30/B70、notional_b=210k、floor）复现 3 手/176,958/17.7%。
- 纯 B 标注如实：引擎 A `cache_end`（v8 末信号日 2026-06-29，fold 结构性截断，P6-3b 登记）→ `degraded_to_pure_b=True`，计划 JSON 含完整 note。
- WARN 18→2 正确：仅 sc0 基差过期（113 天）+ 断层，其余 17 品种基差=08-21。
- **pending 边界合规**：T+1 约定（p17 既有语义），08-21 后无次日交易日 → pending 不入账、账户不污染（权益 1,147,076.88 不变、0 成交、如实标注）。✅

### 3.2 生产自动化是否可投产
**裁决：可投产（YES，附 1 项轻微操作修正）。**
- 防重入正确性：去 `generated_at` 后指纹稳定（独立 md5 复算一致；R1/R2 复跑均跳过；修复前缺陷有测试日志佐证）。
- 幂等：`--update-data` 17 品种 ALREADY_APPLIED、merged=0，重复运行不产生新备份/新登记。
- K 线增量不内嵌**不阻塞**：p6_4 为独立流程（close_pcr 口径 + 外部拉取），脚本 WARN 提示 + 如实说明；每日节奏=先跑 p6_4（或手动拉取）再 `p23_daily_run.py`。
- **轻微操作指引不准确（L2）**：报告称"08-24 数据到达后由 `p23_daily_run.py --date 2026-08-21` 自动 T+1 入账"——实测 `apply_plan` 防重入按 fp 跳过（state=pending 与 applied 不区分），同 date 重跑会 SKIP，**pending 计划不会自动执行**。正确路径：直接 `p17 --apply-date 2026-08-21`（绕开 registry）或 `p23_daily_run.py --date 2026-08-21 --force-apply`。建议：`apply_plan` 增强为仅 `state=="applied"` 才跳过、`state=="pending"` 允许重试；或修正操作指引。不阻塞当前交付（当前 pending 边界处理本身正确且如实）。

### 3.3 tail_ext 监测定位是否合理
**裁决：合理（VERIFIED）。**
- 共享窗口池化 Spearman=1.0000（n=900）→ 无漂移 DRIFT_OK；尾部 0 重合对是构造使然（v8 fold 截断无尾部信号行），"分化"不可度量 → 定位为**覆盖补充而非分化**正确。
- 追加窗口质量：日均 IC +0.0833、命中率 0.677（独立复算一致，≫ 0.50）→ **支持**纳入轻量监测（核心指标=追加窗口 IC/命中率，每次数据刷新重跑 ~1s）。
- 不参与 rt30-vs-v2 升级触发判定：共享日期两缓存指标完全相同 → 触发逻辑无信息量，判定正确。

### 3.4 附注 rt30 UPGRADE_TRIGGER 处置
**裁决：工程师"未做修改"恰当，但应按 P12 规则正式升级评估，而非仅报告附注。**
- 附注属实（复跑确认：IC 连续胜 7/5、fresh=YES）。
- 未做修改恰当：P23 范围纪律（不碰 v8/hexbroker）；且 P20 fresh-OOS 已对该触发作出"**形式上触发但增量数据未提供新信号证据 → rt30/F2 不升级，等待信号缓存重建或更多 fresh-OOS 日**"的前置裁决，本轮无新信号证据，不改变暂缓结论。
- 流程建议：该触发属 live 候选，建议正式列为 QA + 主理人终裁议题（确认 P20 暂缓裁决是否延续 / 何时重估），而非停留在报告附注。此项为流程升级建议，**非数据缺陷**。

---

## 4. 最终 QA 裁决

**VERIFIED（附 1 项轻微操作修正建议 + 1 项流程升级建议，均不阻塞）。**

| # | 声明项 | 独立验证 | 结论 |
|---|---|---|---|
| 1 | 基差 08-21 补拉（17 品种 +1 行，SC 保留 04-30，备份 + applied 登记） | 读 parquet 确认 17×08-21 / SC=04-30；TA/CU 与 MCP 值逐位一致；34 条 applied；17 份备份 | ✅ |
| 2 | 08-21 计划空→非空（ta0 3 手/176,958/17.7%，纯 B，WARN 18→2） | 自写代码复算 br_rank/floor/名义/纯B 全一致 | ✅ |
| 3 | 增量入账 pending（无 08-24 → 不入账；权益 1,147,076.88） | 复跑 p17 --apply-date 逐位一致 | ✅ |
| 4 | 生产自动化（防重入 md5 指纹、幂等、摘要） | 复跑 R1/R2 均跳过（fp 稳定）；--update-data 17×ALREADY_APPLIED | ✅（1 项操作指引修正见 3.2） |
| 5 | tail_ext/S4 对齐（Spearman=1.0000、尾部 0 重合、IC+0.083/命中 0.677） | 复跑 + 独立复算逐位一致 | ✅ |
| 6 | 附注 rt30 UPGRADE_TRIGGER（fresh K 线 IC 7/5） | 复跑 p12 --monitor 确认 | ✅（按 3.4 升级流程建议） |
| 7 | git diff 范围（新脚本 + p20_4 扩展 + 报告；v8/hexbroker 未动） | git status/diff 确认 hexbroker 0 diff、缓存无变更 | ✅ |

### Known Issues / 后续项（不阻塞 VERIFIED）
1. **L2 操作指引不准确**：pending 计划不会经 `p23_daily_run --date 2026-08-21` 自动 T+1 入账（防重入跳过）；需 `p17 --apply-date` 或 `--force-apply`。建议增强 `apply_plan`（pending 允许重试）或修正文档。
2. **L3 流程建议**：rt30 UPGRADE_TRIGGER 建议正式升级为 QA + 主理人终裁议题（延续 P20 暂缓裁决，明确重估条件）。
3. **L3 备注**：`apply_registry.json` 当前记录 `force=true`（工程师终验 --force-apply 产生），后续正常每日运行不受影响。

---

## 附：复核产物
- `deliverables/software-hexfutures-ai/qa_p23_independent_verify.py`（自写独立复核脚本，PASS=17/FAIL=0）
- `deliverables/software-hexfutures-ai/qa_p23_independent_verify.log`
- `artifacts/qa_p23_daily_run_R1.log` / `R2.log` / `qa_p23_daily_run_update.log`（真实复跑输出）
