# QA 独立复核报告 — HexBroker P24（apply_plan 防重入增强 + rt30 正式升级评估）

- 复核人：严过关（Yan，QA，fresh-eyes 独立复核）
- 项目：HexBroker（E:/Workspace/HexBroker）
- 阶段：P24（P23 QA L2 修正项 + L3 升级项闭环）
- 日期：2026-08-21
- Python：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（pandas 3.0.3 / numpy 2.5.2）
- 复核方式：降级模式（团队工具不可用），源码静态审查 + 自写代码复算（**不调 P24 任何函数计算关键指标**）+ 真实复跑
- 复核脚本：`deliverables/software-hexfutures-ai/qa_p24_independent_verify.py`（独立 PASS=11 FAIL=0）
- 复核日志：`deliverables/software-hexfutures-ai/qa_p24_independent_verify.log`、`artifacts/qa_p24_independent_verify.log`
- 复核对象：工程师报告 `sentinel2-p24-apply-retry-rt30-eval-2026-08-21.md`（寇豆码/Kou）

---

## 1. 静态审查

### 1.1 p23_daily_run.py 防重入增强（P24-1）
- **`_should_skip_apply(prev, fp, force) -> (skip, reason)`**：逻辑逐行核对，判定顺序
  `force → 无记录 → fp 不一致 → applied+同fp 跳过 → pending+同fp 重试 → 未知 state 允许重试`。
  修复核心正确：**仅 `state=applied` 且 fp 一致才跳过；`state=pending` 允许重试** —— 直接闭环 P23 QA L2
  指出的缺陷（旧逻辑 fp 一致即跳过、pending 无法自动入账）。
- **`_legacy_state(record)`**：无 `state` 字段 → 返回 `"applied"`（保守跳过）。核对原始 HEAD 版本
  （`git show HEAD:scripts/p23_daily_run.py` L255）：**原始 apply_plan 自始就写 `state` 字段**，
  故实际 registry 记录均带 state；legacy 无-state 仅为防御性迁移。保守方向正确：对"可能已入账"的记录
  跳过（避免重复入账污染账户）优先于"可能漏入账"（后者有 `--force-apply` 逃生门）。**恰当。**
- **状态流转闭环**：`apply_plan` 读 `p17` 快照 `pending` 标志 → registry `state=pending/applied`；
  数据到达后重跑同 date → `_should_skip_apply` 允许重试 → p17 可成交 → 转 applied；仍无数据 → 保持 pending。
  无死锁、无重复入账路径。**闭环成立。**
- 变更范围：`git diff scripts/p23_daily_run.py` = +54/−7（仅 `_legacy_state`/`_should_skip_apply` 新增 +
  `apply_plan` 调用点替换 + docstring）；**`hexbroker/` 0 diff；v8/rt30 缓存 mtime 为 08-19/08-20（早于 P24）未动**。

### 1.2 rt30 评估脚本（P24-2）
- **生产口径显式接线**：`load_config("configs/base.yaml")` + `engine_a_targets_cs(top_k=ea.top_k, min_symbols=ea.min_symbols,
  cache_path=path, group_cap=ea.group_cap, group_map=ea.group_map)`（cap0.5 + ferrous_all 映射 + top30% + min3 全部显式）；
  引擎 B `engine_b_targets(win=252, thr=0.70)`；组合 `combo_stats_row(ra, rb, w_a=0.30, vol_target=False)`。
  **与 P22/P23 同口径**（p22_tail_ext.py 同函数同传参）。
- **块自助实现**：`moving_block_bootstrap` block=21、B=5000、seed=42，**仅 OOS 段**（`_oos_rets` 过滤 2024-07-18 后）——
  嵌套零泄漏成立。块构造 = ceil(n/block) 块拼接截断至 n（尾块尾部截断），与标准移动块自助一致。
- **覆盖率对比方法**：groupby(date) 计数 + 2026-07/08 尾部过滤 + 逐品种末信号日（tail_signal_diff）——方法合理。
- **git diff 范围**：tracked 修改仅 `scripts/p23_daily_run.py` + `trade_plans/2026-08-21_*` + 公众号文章（非 P24 范围）；
  新增 `scripts/p24_1_apply_retry_test.py`、`scripts/p24_rt30_upgrade_eval.py`、报告。`hexbroker/` 0 diff、缓存未动。✅

### 1.3 注意事项（非缺陷，如实登记）
1. **组合名义口径**：`engine_a_targets_cs`/`engine_b_targets` 用 p3 模块常量 `NOTIONAL_FRAC=0.20`
   （非 base.yaml 的 engine B `notional_frac=0.30`）。即本评估框架下**两引擎名义均按 0.20 权益/标的**，
   与 P19 生产记账（M1/M5，B 名义 0.30）口径不同。由于 v8/rt30 用**同一框架同一函数**，相对比较（rt30 vs v8）
   不受影响；且 v8 基线 1.064/0.717 与 P21/P22/P23 逐位一致（该框架为既定生产评估口径）。已如实标注。
2. **compare CSV 的 config 列显示 "A0B1"**：`f"A{combo.w_engine_a:.0f}B{combo.w_engine_b:.0f}"` 把 0.30/0.70
   格式化成了 0/1 —— 展示瑕疵，报告正文（A30/B70）与数值均正确，不影响结论。
3. **trade_plans/2026-08-21 fp 与 HEAD 不同**：HEAD 提交版（basis_latest 08-20、plan_note="数据不完整"）fp=`e8cada03…`；
   当前工作版（P23 基差补拉后 basis 08-21、plan_note 空）fp=`9fe4927e…`。registry 登记的是**当前版** fp（正确）。
   fp 变化来源是 P23 基差补拉的**真实计划内容变化**（对应"fp 变化 → 重新入账"语义），非 P24 缺陷。

---

## 2. 独立复跑验证（真实输出）

### 2.1 P24-1 防重入增强
**复跑 `scripts/p24_1_apply_retry_test.py` → PASS（Part A 9/9 + Part B 5/5 + Part C）**，关键输出：
```
[PASS] pending+同fp → 重试（修复核心）   → skip=False
[PASS] legacy无state+同fp → 保守跳过     → skip=True
[B2] 数据到达重试 → state=applied         ← pending→applied 流转
[C] 运行前 registry[2026-08-21] state=pending
    → 入账: 已执行增量入账（state=pending）  ← 不再"防重入跳过"
    权益(复利): 1,147,076.88 | 成交 0 笔 | pending=True
```

**独立真实路径（临时 registry + mock p17，不碰生产数据）**：
```
[A] legacy无state+同fp apply_plan → applied=False（保守跳过）   PASS
[B] pending+同fp apply_plan → applied=True（重试，state=pending） PASS
[C] applied+同fp apply_plan → applied=False（幂等跳过）          PASS
```

**生产路径重跑两次幂等**（`p23_daily_run.py --date 2026-08-21` ×2）：
```
RUN1: 入账: 已执行增量入账（state=pending）| 权益 1,147,076.88 | 成交 0 | pending=True
RUN2: 入账: 已执行增量入账（state=pending）| 权益 1,147,076.88 | 成交 0 | pending=True
registry 保持 fp=9fe4927e… state=pending（无 08-24 → 不入账、不污染账户）✅
```
- 计划 fp 独立复算：`trade_plans` 与 `artifacts/trade_plans` 两份均 = `9fe4927eb0f2fe8b8dc6e96e874b060b`，与 registry 一致 ✅

### 2.2 rt30 评估独立交叉验证（重点，自写代码不调 P24 函数）

**覆盖率对比（独立读 parquet）**：
```
[v8  ] 8624 行 / 662 日 | 2019-04-01~2026-06-29 | 日均 13.03 | OOS 176 日/日均 13.09 | S2资格日 160 | 07月 0 行 | 08月 0 行
[rt30] 6066 行 / 837 日 | 2019-03-04~2026-07-27 | 日均 7.25 | OOS 310 日/日均 4.49  | S2资格日 143 | 07月 18 行/7日 [ag0,au0,m0] | 08月 0 行
```

**逐品种末信号日（独立计算）——15 品种早 140~174 天确认**：
```
ag0 +31(07-24) au0 +31(07-24) m0 +33(07-27)   ← 仅此 3 品种晚于 v8
al0/cf0/hc0/j0/jm0/ni0/p0/sr0/ta0/y0/zn0 −143(02-06)
i0 −146(02-03) sc0 −140(01-29) cu0/rb0 −174(01-06)
→ 15/18 品种 rt30 末信号日早于 v8 140~174 天；仅 ag0/au0/m0 延至 07-24/27；
  2026-07 = 3 品种 18 行；2026-08 = 0 行（两缓存同为真空）✅
```

**完整回测（生产 cap 口径，独立构建 targets）——v8/rt30 全部逐位复现**：
```
[v8  ] 引擎A S2: full 0.8750 | OOS 1.064207（期望1.064207）| 组合: full 1.0808 | OOS 0.716549（期望0.716549）
[rt30] 引擎A S2: full 0.7087 | OOS 0.544207（期望0.544207）| 组合: full 1.0334 | OOS 0.544852（期望0.544852）
→ Δ(rt30−v8): 引擎A OOS −0.520；组合 OOS −0.172 ✅
```

**块自助（自写移动块实现，block=21, B=5000, seed=42，仅 OOS 段）——p=0.7574 精确复现**：
```
[engineA] 均值日差=−0.0105%/d | se=0.0144 | t=−0.73 | p_one=0.7574（期望0.7574）| p_two=0.4674 | n=506
[combo  ] 均值日差=−0.0031%/d | se=0.0043 | t=−0.73 | p_one=0.7574 | p_two=0.4674 | n=506
```

**滚动 60 日 Sharpe 胜率**：rt30 胜 **43.50%**（446 窗）—— 期望 43.5%/446 窗 ✅

### 2.3 p12 基线对照验证
- **源码确认**：`p12_s4_shadow_monitor.py` L63-64 `V2_PATH=signals_cache18_grouped_v2.parquet`，
  `RT30_PATH=signals_cache18_grouped_v2_rt30.parquet`；`evaluate_trigger` 比较 rt30 vs **v2**（docstring 亦明确
  "S4 影子跟踪对象仍是 P7 当时的对比基线 v2——v8 不在 S4 对比范围"）。
- **基线快照确认**：`artifacts/p12_s4_shadow_baseline.csv` cache=v2（7952 行，止 2026-06-11）。
- **复跑 `p12 --monitor`**：`主触发: UPGRADE_TRIGGER (IC连续胜=7/5, 命中率连续胜=3/5, 对齐日=44, fresh确认=YES)`；
  对比对象 [v2] 滚动IC=-0.3131 vs [rt30] -0.0257 → **触发确实对照 v2（非 v8）** ✅
- **P7 先例数字核对**（p7-engineA-signal-fix 报告）：基线 引擎A S2 OOS −0.337、S4(rt30) +0.053（改善）；
  组合 A15/B85 基线 0.999 → S4 1.138（改善）——与 P24 报告引用的"旧数据 rt30 改善（+0.053/1.138）"一致 ✅

### 2.4 产物核对
- `artifacts/p24_apply_test.log`：Part A 9/9 + Part B 5/5 + Part C 真实路径 PASS（含我复跑追加段）✅
- `artifacts/p24_rt30_compare.csv`：v8/rt30 引擎 A/组合/纯 B 数值与我的独立计算逐位一致 ✅
- `artifacts/p24_rt30_bootstrap.json`：engineA/combo 块自助 obs_mean/se/t/p/CI、滚动胜率 43.4978%（446 窗）与独立复算一致 ✅

---

## 3. 关键裁决

### 3.1 P24-1 修复质量
**裁决：修复正确，真实解决 08-24 自动入账问题（VERIFIED）。**
- **pending 允许重试真解决**：`state=pending` + fp 一致 → 不跳过 → 数据到达后重跑同 date 即执行入账转 applied，
  **无需 `--force-apply`**。真实复跑确认不再"防重入跳过"。✅
- **legacy 迁移保守恰当**：原始代码自始写 state，实际无 legacy 记录；对理论上的无-state 记录按 applied
  保守跳过（避免重复入账污染账户）方向正确，且 `--force-apply` 兜底。✅
- **幂等**：pending 状态下重复执行不产生成交、权益 1,147,076.88 不变（无 08-24 数据时 0 笔成交）；applied 后跳过。✅

### 3.2 P24-2 rt30 不通过
**裁决：成立（VERIFIED）。当前生产口径下 rt30 不优于 v8，维持 v8 正确。**
- **主判据**：引擎 A S2 OOS v8 1.064 vs rt30 0.544（Δ−0.520）；组合 A30/B70 0.717 vs 0.545（Δ−0.172）——
  独立逐位复现。块自助均值日差为**负**（−0.0105%/d），p_one=0.7574（单侧 H0: 均值≤0 不被拒绝），
  CI 覆盖 0 → 无显著改善，方向为负。滚动 60 日 Sharpe 胜率 43.5%（<50%）。✅
- **P7 先例反转机制理解正确**：P7 的 rt30"改善"是对**弱基线 v2**（OOS −0.337）的比较；生产基线 v8
  （OOS 1.064）在覆盖率（OOS 日均 13.09 vs 4.49）与 OOS 表现上均远超 v2/rt30，rt30 在 v8 面前无任何优势，
  且 OOS seg1（2024H2）−0.741 显著恶化。同一缓存、同一方法，结论由"改善"反转为"恶化"，机制解释成立。✅
- **"覆盖更长不成立"关键发现正确**：逐品种末信号日独立复算——15/18 品种 rt30 比 v8 **早 140~174 天**
  （01-06~02-06）；仅 ag0/au0/m0 延至 07-24/27（+31/+33 天）；07 月仅 3 品种 18 行、08 月 0 行（与 v8 同为真空）。
  rt30"止 07-27 覆盖更长"仅在缓存 max-date 层面成立，逐品种层面覆盖优势极窄 → 不执行尾折扩展的决定合理。✅
  该发现解释了为何 rt30 影子触发（近期窗口 vs v2 滚动 IC 占优）但生产比较（vs v8 全窗口）负向。✅

### 3.3 p12 影子基线 v2→v8 建议
**裁决：应采纳。** p12 S4 影子跟踪/UPGRADE_TRIGGER 当前对照 v2（P7 期基线，非生产基线）。影子监控的意义在于
"候选 vs 生产"——生产已为 v8（P8-4/P19 固化），对照 v2 会（1）对已不相关的旧基线产生误导性触发
（本次 UPGRADE_TRIGGER 即为例证），（2）无法监测候选 vs 生产的真实差异。**v2→v8 后触发才与生产相关。**
（注意：v8 末信号日 06-29，与 rt30 07-27 的尾部对齐窗口窄，v2→v8 后 n_common_dates 可能进一步下降，
建议切换时同步评估触发阈值/对齐日充足性。）

### 3.4 "重建后 rt30"后续
**裁决：值得登记为未来项，但不急于立项；建议 v13 自然重建时一并评估。**
- 本评估基于 P7 期 rt30 缓存（test_len=30/lb20，止 07-27，15/18 品种信号早停）——其负向结论包含
  "覆盖率劣势"成分，而非纯粹的模型频率效应。重建（`p7._build_rt30_cache` 级，耗时长）后覆盖拉齐，
  结论可能变化。
- 但：生产无信号恢复需求（v8 08 月亦真空），重建 rt30 仅具研究价值；且 v13 重建（既有路线）会自然
  引入同频重训评估窗口。**建议：登记后续项，v13 重建时一并评估，不单独立项。**（触发条件：v8 出现
  信号恢复或数据端到 08-24 后仍有 UPGRADE_TRIGGER 且已切换 v2→v8 基线。）

---

## 4. 最终 QA 裁决

**VERIFIED（附 2 项如实说明 + 1 项流程建议，均不阻塞）。**

| # | 声明项 | 独立验证 | 结论 |
|---|---|---|---|
| 1 | P24-1 `_should_skip_apply`（applied 跳过/pending 重试/fp 变化/force） | 源码核对 + 独立 7 例 + 复跑 9/9 | ✅ |
| 2 | P24-1 legacy 迁移保守（无 state → applied 跳过） | `_legacy_state` 核对 + 真实 apply_plan 场景 A | ✅ |
| 3 | P24-1 状态流转 pending→applied→幂等 | mock 状态机 5/5 + 真实路径重跑两次幂等 | ✅ |
| 4 | P24-1 真实路径：08-21 重跑不再防重入跳过、仍 pending、权益 1,147,076.88 | 复跑 Part C + 双跑确认 | ✅ |
| 5 | P24-2 引擎 A S2 OOS v8 1.064 / rt30 0.544（Δ−0.520） | 独立构建 targets 逐位复现 | ✅ |
| 6 | P24-2 组合 A30/B70 OOS 0.717 / 0.545（Δ−0.172） | 独立逐位复现 | ✅ |
| 7 | P24-2 块自助 p=0.757（方向负、不显著） | 自写移动块自助精确复现（obs_mean/se/t/CI 全一致） | ✅ |
| 8 | P24-2 滚动 60 日 Sharpe 胜率 43.5% | 独立复算 43.50%/446 窗 | ✅ |
| 9 | P24-2 "覆盖更长不成立"（15 品种早 140-174 天；仅 ag0/au0/m0 延至 07-24/27；07 月 3 品种 18 行、08 月 0 行） | 逐品种末信号日独立计算全一致 | ✅ |
| 10 | p12 UPGRADE_TRIGGER 对照 v2（非 v8） | 源码 + 基线 CSV + 复跑确认 | ✅ |
| 11 | P7 先例反转（旧数据 +0.053/1.138 → 当前 −0.520/−0.172） | P7 报告核对 + 当前复现 | ✅ |
| 12 | git diff 范围（p23_daily_run.py + 新脚本 + 报告；hexbroker 0 diff；缓存未动） | git status/diff + mtime 确认 | ✅ |

### 如实说明 / Known Issues（不阻塞 VERIFIED）
1. **组合名义口径**（说明）：本评估框架（p3/p5 共享函数）两引擎名义均按 `NOTIONAL_FRAC=0.20`，未反映
   P19 的 engine B `notional_frac=0.30`；因 v8/rt30 同框架同函数，相对结论不受影响，且 v8 基线 1.064/0.717
   为既定生产评估口径（P21/P22/P23 逐位一致）。
2. **compare CSV config 列 "A0B1"**（说明）：`:.0f` 格式化瑕疵，报告正文与数值正确。
3. **08-24 自动入账表述微调**（说明）：`--date 2026-08-21` 在 08-24 数据到达后会自动把 08-21 pending 计划
   执行入账并转 applied（已证实）；报告提及的"`--date 2026-08-24` 含 08-21 计划"表述不精确
   （`run_apply_date` 入账的是该 date 计划，08-21 pending 由 `--date 2026-08-21` 或 08-21 路径处理），
   不影响主路径结论。
4. **p12 基线 v2→v8**（流程建议）：应采纳切换，使影子触发对照生产基线；切换后留意对齐日减少对触发阈值的影响。
5. **"重建后 rt30"**（后续项）：登记未来项，建议 v13 自然重建时一并评估，不单独立项。

---

## 附：复核产物
- `deliverables/software-hexfutures-ai/qa_p24_independent_verify.py`（自写独立复核脚本，PASS=11/FAIL=0）
- `deliverables/software-hexfutures-ai/qa_p24_independent_verify.log`（= `artifacts/qa_p24_independent_verify.log`）
- 复跑输出：`artifacts/p24_apply_test.log`（追加 QA 复跑段）、`artifacts/qa_p24_independent_verify.log`
