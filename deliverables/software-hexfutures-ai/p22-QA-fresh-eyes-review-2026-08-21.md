# P22 QA Fresh-Eyes 独立复核报告：尾折扩展研究实验 + --apply-date 增量入账模式

- **日期**：2026-08-21
- **复核人**：QA Engineer（Edward / 严过关，fresh-eyes 独立复核）
- **项目**：HexBroker（E:/Workspace/HexBroker）
- **复核对象**：`scripts/p22_tail_ext.py`（尾折扩展）+ `scripts/p17_shadow_account.py --apply-date`（增量入账）
- **上游交付**：`deliverables/software-hexfutures-ai/sentinel2-p22-tail-ext-apply-date-2026-08-21.md`（工程师 Kou）
- **模式**：降级模式（团队工具不可用），独立复核并如实报告
- **独立验证脚本**（自写，未调用 p22 任何函数）：
  - `deliverables/software-hexfutures-ai/qa_p22_independent_verify.py`（共享窗口逐字节 + 同窗口重估）
  - `deliverables/software-hexfutures-ai/qa_p22_zero_leakage.py`（零泄漏扰动测试）

---

## 0. 执行摘要（TL;DR）

| 复核项 | 结果 | 结论 |
|---|---|---|
| 静态审查 p22_tail_ext.py / p17 --apply-date | ✅ 通过 | 末折训练窗与 v8 一致；输出路径 v8_tail_ext；--apply-date 状态续接/MTM/T+1/pending 逻辑正确 |
| **v8 == tail_ext 共享窗口逐字节** | ✅ **PASS** | 8,624 共享行 p_up/exp_ret/is_effective **max\|diff\|=0.000e+00**，v8 行零缺失零重复 |
| 尾 665 行覆盖 | ✅ 通过 | 17（06-22..06-29 恢复）+ 18（06-30）+ 396（07 月/22 日）+ 234（08 月/13 日），07-01..08-21 每日 18 品种 |
| **同窗口重估（自建目标，独立代码）** | ✅ **VERIFIED** | 引擎 A S2 OOS 1.064207→**1.096311**（Δ+0.032104）；组合 0.716875→**0.725649**（Δ+0.008774）；OOS n=505 同窗口 |
| 零泄漏检查（扰动测试） | ✅ PASS | 未来特征置乱 → ts<扰动点全部预测逐字节不变；独立复现 rb0/sc0 尾行与缓存逐字节一致 |
| 分解实验 | ✅ VERIFIED | 07-01→08-21 v8 前向填充 +2.9061% vs tail_ext 每日再平衡 +2.9502%；仅恢复 17 行单独看 1.0246（略降） |
| --apply-date 独立复跑 | ✅ VERIFIED | 08-20 计划 2 笔成交、权益 1,147,968.68、现金 Δ+10,099.81、持仓 al0 2/ta0 3/zn0 1；08-21 空计划 pending 0 成交；auto 链式幂等；--replay 复跑 1,150,947.65 不变 |
| 工件核对 | ✅ 一致 | p22_tail_ext_coverage.csv / engineA.csv / verdict.json / apply log 全部与独立复算一致 |
| **最终 QA 裁决** | — | **VERIFIED**（tail_ext 登记"信号恢复候选"不升级生产；--apply-date 就绪可投产） |

---

## 1. 静态审查

### 1.1 scripts/p22_tail_ext.py（尾折扩展）

**训练窗与 v8 一致（参数逐项核对）**：
- `WF_SPLITTER = dict(train_len=250, test_len=60, purge=5, embargo=2, mode="rolling")` — 与 v8 构建（p6_4/p21_cache_rebuild）一致 ✅
- `label_mode=cross_z / label_pool=all`：`_build_fwd_cs_panel(features, bars, horizon, "cross_z", "all")` ✅
- `cal_split=0.5`：末折测试窗前 50% 拟合 Platt（`k = int(len(sigs)*0.5)`），后 50% 应用 ✅
- `champion HP`：`load_best_params()` 应用到 cfg.forecast ✅
- 训练切片 `t_max = fold.train_max_pos - horizon`，`build_windows(train_feat, lookback)` 与 v8 walk_forward_lightgbm 同源 ✅

**评估窗延长逻辑（无前视）**：
- `tail_input = feat.iloc[fold.test_end - lookback + 1 :]` — 第一个窗口末端 = test_end（即末折测试窗后第一个 bar），窗口只用 `[t-lookback+1, t]` 全部 ≤t 数据；模型训练数据远早于 test_end ✅
- **绝不覆盖 v8**：输出 `signals_cache18_grouped_v8_tail_ext.parquet`；`V8_PATH` 只读 ✅（mtime 验证：v8 = Aug 20 13:14 未动）

**Platt 校准器沿用（非重拟合）**：
- 校准器仅用末折测试窗前 50%（≤ 末折 test_end 且远早于追加窗口）拟合；追加信号 `_apply_calibrator_one(s, scaler, "platt")` 应用同一校准器，不重新拟合 ✅

**内置一致性校验**：每品种末折复现 vs v8（max_diff 校验）+ merge 时逐品种重叠校验（ts > 该品种 v8 末信号日）+ 同窗口 targets 差异统计 ✅

### 1.2 p17 --apply-date（git diff +240 行）

- **状态续接**：`_load_account_state` anchor=auto 优先 `p17_shadow_account_apply.json`（链式）否则基线；anchor=base 强制基线（对照用）✅
- `_rebuild_account` 从 `final_state` 重建持仓/均价/现金/已实现盈亏 ✅
- **MTM 推进**：`last_date 之后 → apply_date（含）` 逐日 `account.mark(ds, prices)`（仅估值不成交）✅
- **T+1 开盘成交**：`execute_plan(plan, exec_date=t1, price_col="open")`，t1 = apply_date 后第一个交易日；无次日 → pending ✅
- **基于当前日期而非 OOS_END**：日期推进只依赖锚点日期 + `--apply-date` + 数据实际日历，`OOS_END=08-17` 保持不变（全量回放基线稳定）✅
- **--replay 向后兼容**：`--apply-date` 分支在 main 顶部 early-return，全量回放路径不受影响；复跑验证见 §4 ✅
- **--skip-consistency 空摘要 KeyError 修复**：`print_summary` 中 `if not consistency_summary: print(...); return` 在访问 `consistency_summary["paths"]` 之前短路 ✅（复跑 `--reuse-plans --skip-consistency` 正常完成，未再抛 KeyError）
- **落盘隔离**：apply 状态快照 `p17_shadow_account_apply_{date}.json` + 最新 `p17_shadow_account_apply.json`，不覆盖基线 ✅

### 1.3 git diff 范围

- 修改：`scripts/p17_shadow_account.py`（+240）、`docs/developer-guide.md`（P21/P22 文档）、P21 交付报告（QA 归因修正）、公众号日志/进度
- 新增（未跟踪）：`scripts/p22_tail_ext.py`、P22 交付报告、QA 复核文件
- **v8 缓存 mtime 未动**（Aug 20 13:14），P22 产物独立命名（_tail_ext / p22_*）✅

---

## 2. 独立复跑验证（重点：v8 == 尾折共享窗口）

### 2.1 v8 == tail_ext 共享窗口逐字节（核心）

自写代码读两个缓存比对（真实输出）：

```
tail_ext 共享行（ts<=该品种 v8 末信号日）: 8624 （应=8624）
  v8 匹配行: 8624 （应=8624）
  p_up max|diff| = 0.000e+00 | exp_ret max|diff| = 0.000e+00 | is_effective 全等 = True | 重复行 = 0
  ==> 共享行逐字节一致: True
v8 行在 tail_ext 中缺失: 0
```

- **8,624 共享行 = v8 原 8,624 行，p_up/exp_ret/is_effective 逐字节一致（max\|diff\|=0.000e+00）** ✅
- 尾 665 行与 v8 零重叠（逐品种 ts > 该品种 v8 末信号日）✅

### 2.2 尾 665 行覆盖（真实输出）

```
尾行数: 665 （应=665）
尾行日期范围: 2026-06-22 ~ 2026-08-21
2026-07 尾行: 396 行 / 22 日 | 2026-08: 234 行 / 13 日
尾行品种覆盖: 18 品种全
06-22..06-29 恢复行: 17（ag0/au0/m0/sc0 尾折更早，各 4-7 行）
07 月每日品种数唯一值: [18] | 08 月每日品种数唯一值: [18]
```

- 分解：665 = 17（06-22..06-29 恢复 4 品种）+ 18（06-30，全 18 品种首日）+ 396（07 月 22 交易日）+ 234（08 月 13 交易日）✅ 与交付报告 §1.3（648 ≥06-30 + 17）一致
- 07-01..08-21 每个交易日均有 18 品种信号（引擎 A 不再空转）✅

### 2.3 同窗口重估（独立代码交叉验证）

**独立实现**（未调用 p22 任何函数）：按文档口径自建引擎 A S2 目标（日截面 exp_ret rank top30% / min=3 / cap=0.5 / group_map=base.yaml / floor 手数）+ 自建引擎 B（basis_ratio 滚动分位 win252/thr0.70）+ BacktestEngine（生产代码，同口径）；并把我自建 targets 与生产 p5 `engine_a_targets_cs` 逐行比对。

**真实输出**：

```
[引擎B] 自建 vs p3 targets 不一致行: 0（应 0）
[引擎B] 全样本 Sharpe 1.0316 | OOS Sharpe 0.4824 | OOS ret +4.68%

[引擎A-v8] 自建 vs p5 targets 不一致行: 0（应 0；共比对 8624 行）
[引擎A-v8] 全样本 Sharpe 0.8750 | OOS Sharpe 1.0642 | OOS ret +11.5873% | 做多行 1953
[combo A30/B70-v8] OOS Sharpe 0.7169 | OOS ret +6.7471%

[引擎A-tail_ext] 自建 vs p5 targets 不一致行: 0（应 0；共比对 9289 行）
[引擎A-tail_ext] 全样本 Sharpe 0.8786 | OOS Sharpe 1.0963 | OOS ret +11.8084% | 做多行 2078
[combo A30/B70-tail_ext] OOS Sharpe 0.7256 | OOS ret +6.8100%

对比摘要：
  v8: 引擎A S2 OOS Sharpe 1.064207 | OOS ret +11.5873% | 做多行 1953 | combo OOS Sharpe 0.716875
  tail_ext: 引擎A S2 OOS Sharpe 1.096311 | OOS ret +11.8084% | 做多行 2078 | combo OOS Sharpe 0.725649
  Δ 引擎A S2 OOS Sharpe: +0.032104（工程师声称 +0.0321）
  Δ 引擎A S2 OOS ret: +0.2211pp（工程师声称 +0.22pp）
  Δ combo OOS Sharpe: +0.008774（工程师声称 +0.0088）
  OOS n 相同（同窗口）: v8=505 tail_ext=505
```

| 指标 | 工程师声称 | QA 独立复算 | 判定 |
|---|---|---|---|
| 引擎 A S2 OOS Sharpe v8 | 1.064 | 1.064207 | ✅ |
| 引擎 A S2 OOS Sharpe tail_ext | 1.096 | 1.096311 | ✅ |
| Δ S2 OOS Sharpe | +0.032 | +0.032104 | ✅ |
| 引擎 A S2 OOS 复利 tail_ext | +11.81% | +11.8084% | ✅ |
| 组合 OOS Sharpe v8 | 0.717 | 0.716875 | ✅ |
| 组合 OOS Sharpe tail_ext | 0.726 | 0.725649 | ✅ |
| Δ 组合 | +0.009 | +0.008774 | ✅ |
| 纯 B OOS Sharpe | 0.482 | 0.4824 | ✅ |
| OOS n（同窗口） | 505 | 505 / 505 | ✅ |

- **同窗口确认**：两缓存均在 prices→08-21 同一窗口评估，OOS n 均为 505 ✅
- **基线复核**：v8 行与 P21 基线（p21_engineA_compare.csv: 1.064206885818831 / 0.7168751155564482 / 0.4824155624281257）逐项一致（Δ=0.000）✅
- **目标构造独立验证**：QA 自建 targets 与生产 p5 函数 0 行不一致 → 缓存到目标的过程无歧义、无操纵 ✅

### 2.4 分解实验独立验证（真实输出）

```
07-01→08-21 窗口：
  v8 前向填充被动持有 : +2.9061%
  tail_ext 每日再平衡 : +2.9502%
  日收益 std: v8 0.3023% vs tail_ext 0.2246%
  窗口内 Sharpe: v8 4.449 vs tail_ext 6.064
恢复 17 行（06-22..06-29，4 品种 ag0/au0/m0/sc0）
v8 + 仅恢复17行 → 引擎A S2 OOS Sharpe: 1.0246（工程师声称 1.025）| OOS ret +11.03%
```

- 07-01→08-21：v8 +2.9061% vs tail_ext +2.9502%（声称 +2.91% / +2.95%）✅
- 日收益 std 更低（0.2246% vs 0.3023%）、窗口内 Sharpe 更高（6.064 vs 4.449）→ **"总量接近、路径更平滑"成立** ✅
- 仅恢复 17 行（06-22..06-29，ag0/au0/m0/sc0）单独看 OOS Sharpe 1.0246（< 基线 1.0642）→ **"改善来自 07/08 新信号日收益路径而非末折边界恢复"成立** ✅

---

## 3. 零泄漏检查（独立扰动测试）

**方法**（qa_p22_zero_leakage.py，未调用 p22 函数）：独立复刻尾折方法（共享流水线函数）训练 rb0 末折模型 → 追加窗口预测 → 将 **2026-07-15 之后（未来）的特征全部置乱**（standard_normal×1e3）→ 重新预测 → 验证 ts < 扰动点 的预测逐字节不变。

**真实输出**：

```
[rb0] 尾折 fold_end=2026-06-29 | tail 36 行 2026-06-30~2026-08-21
扰动点: 2026-07-15（之后特征置乱）
  ts < 2026-07-15（应逐字节不变）: 11 行 | p_up max|diff|=0.000e+00 | exp_ret max|diff|=0.000e+00 | is_effective 全等=True
  ts >= 2026-07-15（允许改变）: 25 行 | p_up max|diff|=5.667e-01
  ==> 零泄漏（未来扰动不影响过去预测）: True
独立复现 rb0 尾行 vs tail_ext 缓存：共享 36 行 | p_up max|diff|=0.000e+00 | exp_ret max|diff|=0.000e+00 | 一致=True
```

**补充（sc0，最早折边界品种，含 06-22..06-29 恢复行）**：

```
[sc0] 独立复现 tail vs 缓存: 42 行 | p_up max|diff|=0.000e+00 | exp_ret max|diff|=0.000e+00 | 一致=True
  tail 首/末: 2026-06-22~2026-08-21
```

- **零泄漏 PASS**：未来特征扰动不影响任何过去预测（max\|diff\|=0）✅
- **独立复现一致**：QA 从零训练 rb0/sc0 尾折模型，尾行与 tail_ext 缓存 **逐字节一致** → 缓存确由该方法生成（非伪造），且训练/校准/预测链路因果 ✅
- 特征管线本身严格因果：`RollingNormalizer`（rolling z-score 窗口含当前 bar）、`build_windows`（窗口末端 = bar t）——静态审查确认无全局统计量跨样本泄漏 ✅

---

## 4. --apply-date 独立验证（复跑 + 数值核对）

复跑前已备份 `artifacts/shadow_account/` 与 `p22_apply_date_test.log`；复跑后已恢复原状（md5 一致），交付工件保持 pristine。

### 4.1 apply 2026-08-20（anchor=base）— 复跑真实输出

```
MTM 推进（2026-08-17 之后 → 2026-08-20）: 3 日
  2026-08-18: equity=1,152,633.36 | 2026-08-19: equity=1,146,167.17 | 2026-08-20: equity=1,146,782.67
计划 2026-08-20: [('al0', 2), ('ta0', 3), ('zn0', 1)] （共 6 手 / 3 个品种）
T+1 开盘成交: exec_date=2026-08-21（open 价 + 滑点 1.0 tick）
成交 2 笔（买 1 手 / 卖 1 手）
  2026-08-21 ni0 卖 1 手 @ 157,958.79 fee=7.90
  2026-08-21 zn0 买 1 手 @ 29,568.74 fee=7.39
T+1 收盘 MTM 2026-08-21: equity=1,147,968.68 cash=644,576.05
账目日期: 2026-08-21 | 现金: 644,576.05 | 权益(复利): 1,147,968.68
已实现盈亏: 148,814.17 | 持仓: {'al0': 2, 'ta0': 3, 'zn0': 1}
成交笔数: 2 | pending=False | no_fill=0
[参考] fresh 账户（空仓）同计划 → 成交 3 笔（买 6 手 / 卖 0 手）
```

- **权益 1,147,968.68 ✅、现金 Δ = 644,576.05 − 634,476.24 = +10,099.81 ✅、持仓 al0 2/ta0 3/zn0 1 ✅、2 笔成交 ✅**
- **现金算术全精度核对**：ni0 08-21 open=157,968.791156 卖 −1 tick（min_tick=10）→ fill 157,958.791156；zn0 open=29,563.737463 买 +1 tick（min_tick=5）→ fill 29,568.737463。ni0 卖入 +157,958.791156−7.90=+157,950.89；zn0 买入 −29,568.737463×5−7.39=−147,851.08；净 Δ=**+10,099.81**（644,576.05−634,476.24）✅ 分毫不差
- **"成交 3 笔 vs 2 笔"归因合理**：08-20 计划 3 品种/6 手中 al0/ta0 已由锚点持有且目标不变（al0 2→2、ta0 3→3）→ 实际成交 ni0 清仓 + zn0 开仓 2 笔；fresh 空仓同计划成交 3 笔（买 6 手）为对照。归因如实 ✅

### 4.2 apply 2026-08-21（anchor=base + auto）— 复跑真实输出

```
anchor=base: MTM 推进 4 日（08-18..08-21）| 计划 08-21: []（0 手）
  T+1 开盘成交: 无次日交易日 → **pending** | 成交 0 笔
  账目日期 08-21 | 权益(复利): 1,147,076.88 | 持仓 al0 2/ni0 1/ta0 3（不变）
anchor=auto: 锚点 p17_shadow_account_apply.json（账目日期 08-21）
  MTM 推进 0 日 | 计划空 + T+1 pending → 0 成交 | 结果与 base 一致
```

- 08-21 空计划（引擎 A fold 截断 + 基差 08-21 源端未发布）→ 0 成交 + T+1 pending ✅
- auto 链式续接幂等（从自身快照再跑结果不变）✅

### 4.3 --replay 向后兼容（复跑）

```
成交 1000 笔 | 阻断 50 | 期末权益 1,150,947.65
[DONE] P17 内部簿记模拟盘完成。
```

- 全量回放复跑 `--reuse-plans --skip-consistency` → **期末权益 1,150,947.65 不变** ✅（P20/P21 基线一致）
- --skip-consistency 空摘要修复生效（未再抛 KeyError）✅

---

## 5. 工件核对

| 工件 | 核对结果 |
|---|---|
| `artifacts/p22_tail_ext_coverage.csv` | v8 8,624/662 日/末 06-29；tail_ext 9,289/698 日/末 08-21；07 月 396/22 日、08 月 234/13 日 — 与独立复算一致 ✅ |
| `artifacts/p22_tail_ext_engineA.csv` | v8/tail_ext 全部指标与 QA 独立回测一致（见 §2.3）✅ |
| `artifacts/p22_tail_ext_verdict.json` | shared_rows_identical=true、shared_window_target_delta_rows=17、S2 1.064207→1.096311、combo 0.716875→0.725649、candidate=true ✅ |
| `artifacts/p22_apply_date_test.log` | 复跑三节输出与交付日志逐行一致 ✅ |
| `artifacts/signals_cache18_grouped_v8.parquet` | mtime Aug 20 13:14 未动（P22 未覆盖 v8）✅ |
| `artifacts/p22_tail_checkpoints/*.parquet` | 8 组齐全 ✅ |
| `artifacts/shadow_account/p17_shadow_account_apply_*.json` | 08-20（cash 644,576.05/pos al0 2/ta0 3/zn0 1/2 fills）、08-21（pending/0 fills）与日志一致 ✅ |

---

## 6. 关键裁决（QA 专业意见）

### 6.1 tail_ext 是否应升级为引擎 A 生产信号源？

**QA 意见：不建议正式升级生产；维持"引擎 A 信号恢复候选"（研究证据）登记，可作"数据刷新后临时信号源"观察。** 理由：

1. **改善真实但幅度小**：S2 OOS Sharpe +0.032（1.064→1.096）、组合 +0.009（0.717→0.726），样本仅 ~35 个新增交易日，统计显著性不足。
2. **非 v8 生产口径**：尾折扩展是研究变体（v8 折叠网格固定；延长评估窗），升级意味着在信号生产链路落地"尾折机制"，属于口径变更，需与 v8 生产缓存并行评估而非替换。
3. **恢复信号的生产意义是真实价值**：引擎 A 06-30..08-21 空转（无信号）是 fold 截断结构性属性；tail_ext 让 07/08 月每日 18 品种信号回归，避免了"引擎 A 在计划中缺席"。作为**数据刷新后到自然跨折边界（~20-24 交易日）之前的临时信号源**有其价值——但需明确标注研究口径、独立缓存、且不干扰 v8 基线。
4. **终裁前建议补充**：① 自然跨折边界后 v13 重建的同向对比；② 更长样本的 07/08 信号稳定性（IC/换手）；③ 与 S4 fresh_signal 判据对齐。

**结论**：与工程师裁决一致 — tail_ext 登记候选不升级生产，方向为正但幅度小，等待主理人终裁。

### 6.2 分解实验结论

**成立**。独立验证：07-01→08-21 窗口 v8 前向填充 +2.9061% vs tail_ext 每日再平衡 +2.9502%（总量接近）；日收益 std 0.3023%→0.2246%（路径更平滑）、窗口内 Sharpe 4.449→6.064；仅恢复 06-22..06-29 的 17 行（4 品种）单独看 OOS Sharpe 1.0246（略降）。→ **改善主要来自 07/08 新信号日的日收益路径（更平滑），而非末折边界恢复**。

### 6.3 --apply-date 就绪性

**就绪可投产**（生产每日节奏 p16 出计划 → p17 --apply-date 入账）：

1. 复跑全部数值分毫不差（权益 1,147,968.68、现金 Δ+10,099.81 全精度核对、持仓、2 笔成交；08-21 pending 0 成交；auto 链式幂等；--replay 1,150,947.65 不变）。
2. 成交笔数差异归因合理（锚点已持有 al0/ta0 且目标不变 → 2 笔 vs fresh 空仓 3 笔，代码内参考对照印证）。
3. 生产注意点（非阻塞）：
   - `--apply-date` = 最新数据日时计划必为 pending（T+1 无次日）——属文档化约定，生产节奏应"当日跑昨日计划"；
   - 计划文件需存在于 `artifacts/trade_plans/`（或回退根 `trade_plans/`）；
   - apply 日志为追加写，长期运行会增长（审计友好，无功能影响）。

### 6.4 DISCREPANCY 说明

**最终工件无功能性 DISCREPANCY。** 过程/文档层面如实记录：

- **D1（过程痕迹，已修复）**：`p22_tail_ext_build.log` 显示首次 merge 尝试 FAIL（"追加尾信号与 v8 重叠 17 行"）——早期版本 merge 校验误伤 06-22..06-29 合法恢复行；最终代码改为逐品种重叠校验后 merge 成功（9,289 行）。属迭代调试痕迹，最终状态正确。
- **D2（过程痕迹，已修复）**：`p22_tail_ext_run2.log` 末尾 `UnboundLocalError: verdict` ——中间版本 run_eval 中 verdict 在"同窗口校验"之前未初始化；最终代码 `verdict: dict = {}` 提前初始化，最终 engineA.csv/verdict.json（19:53）与 QA 独立复算完全一致。
- **D3（文档措辞）**：交付报告 §2.2 现金核对公式"Δ+10,099.81 = ni0 卖入 +157,950.89 − zn0 买入 147,851.09 − 手续费 15.29"中，前两项已分别含手续费（+157,950.89 = 157,958.79−7.90；−147,851.09 = 29,568.737463×5+7.39），再减 15.29 会重复计数；但**头行 Δ+10,099.81 正确**（QA 全精度复算分毫不差）。建议修正公式措辞，不影响结论。
- **D4（观察）**：交付日志 anchor=auto 演示的链式续接实际链到的是 08-21 base（pending）快照而非 08-20 apply 快照（因运行顺序 08-20→08-21 base→08-21 auto）；行为正确（幂等），但"链式续接"演示场景与 §2.2③ 描述存在顺序差异，建议文档注明运行顺序。

---

## 7. 最终 QA 裁决

**VERIFIED**

- P22-1 尾折扩展：共享窗口 8,624 行与 v8 逐字节一致（max\|diff\|=0.000e+00）；尾 665 行 07/08 月 18 品种全覆盖；零泄漏（扰动测试 PASS + rb0/sc0 独立复现逐字节一致）；同窗口重估 1.064→1.096 / 0.717→0.726 独立复算分毫不差；分解结论成立。**tail_ext 登记"引擎 A 信号恢复候选"（研究证据），不升级生产** — 与工程师裁决一致，待主理人终裁。
- P22-2 --apply-date：增量入账模式复跑全数一致（权益 1,147,968.68、现金 Δ+10,099.81、持仓、2 笔成交、08-21 pending、auto 幂等、--replay 1,150,947.65 不变），**就绪可投产**；成交笔数差异归因合理。
- 无功能性 DISCREPANCY；D1-D4 为过程痕迹/文档措辞，已在 §6.4 如实记录。

**QA 建议**：主理人终裁 tail_ext 升级与否时，参考 §6.1 的三项补充证据；投产 --apply-date 时按"当日跑昨日计划"节奏运行，并跟踪日志增长。

---

## 附：独立验证脚本

- `deliverables/software-hexfutures-ai/qa_p22_independent_verify.py` — 共享窗口逐字节 + 自建目标同窗口重估
- `deliverables/software-hexfutures-ai/qa_p22_zero_leakage.py` — 零泄漏扰动测试 + rb0 独立复现
