# P13 多模型融合（XGB/HGB/F1/F2/F3）— QA 独立复核报告（fresh-eyes）

- **QA**：严过关（software-qa-engineer）
- **日期**：2026-08-20
- **对象**：P13 引擎 A 多模型融合（LightGBM + XGBoost + HistGradientBoosting）
- **范围**：静态审查 `scripts/p13_models.py` + `scripts/p13_multimodel.py`；独立复跑 v8 基线 / F1/F2/F3；独立 F2 交叉验证；零泄漏检查；关键裁决（F2 采纳与否、生产切换与否）
- **独立脚本**：`scripts/qa_p13_independent_verify.py`（28/28 PASS，逐位复现；F2/F3 融合与落盘缓存 max|Δ|=0）
- **口径**：复利口径、OOS 2024-07-18 后、嵌套零泄漏（F2 权重只用 IS）、完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）、A10/B90

---

## 0. 结论摘要（TL;DR）

| 维度 | 结论 |
|---|---|
| 工程/数据/零泄漏 | ✅ PASS（全部独立复现逐位一致；无泄漏；HGB 修复确定性） |
| 工程师数字真实性 | ✅ 真实可复现（0.626 / +8.68% / 1.612 / -0.0640 四项 v8 基线全复现；F2 0.679 / 1.617 / -0.0606 复现；F3 0.585 / 1.609 / -0.0385 复现） |
| **DISCREPANCY-1（关键）** | ⚠️ **P13 评估实际未加载生产风控配置**：`p13_multimodel.py` 用 `load_config()`（无 path）→ `configs/base.yaml` 未加载 → `engine_a_targets_cs` 内部读到 `group_cap=None` → **评估在无 cap / GROUPS_V2 8 组映射下进行**，与任务简报"生产 config 含 group_cap=0.5"不符 |
| **生产口径重跑（cap=0.5+ferrous_all）** | ❌ **F2 相对 v8 的优势反转**：单引擎 OOS Sharpe v8 0.646 vs F2 0.597；OOS 复利 v8 +9.01% vs F2 +7.87%；组合 v8 1.614 vs F2 1.612；仅全样本 MaxDD 仍优（-30.3% vs -19.0%，且主要在 IS 段；OOS MaxDD -6.7% vs -6.6% 基本持平） |
| **最终 QA 裁决** | **不采纳 F2 切换生产 signal_cache（v8→v9_f2）**。F2 作为候选进入 P12 S4 影子跟踪 / 在 cap 口径下重新评估后再定；工程师需修正"生产 config 含 group_cap=0.5"的表述 |
| 诚实结论评价 | ✅ 合理：三模型 exp_ret 相关性 0.80~0.87（OOS 0.83~0.87），多样性有限；F2 OOS 截面 IC 仍为负（-0.0606），模型多样性不是截面 IC 为负的唯一瓶颈 |

---

## 1. 静态审查表

| 审查项 | 结论 | 证据/说明 |
|---|---|---|
| XGB/HGB 与 LGB 同构（均值+残差方差双模型、MC 路径采样 p_up/exp_ret） | ✅ | `XGBoostForecast/HGBForecast` 均实现 `fit`（mean 模型 + `resid**2` 方差模型）、`predict`（`build_windows → _next_return → sample_paths → _signals_from_paths`）、`_mean_model` 属性、`family` 标记；与 `LightGBMForecast` 逐函数同构 |
| walk_forward 契约（model_cls 参数化） | ✅ | `walk_forward_lightgbm(model_cls=...)` 契约：`model_cls(cfg, model_id=...) → fit(X,y) → predict(X) → s.ts/s.exp_ret/s.p_up/s.is_effective`；两模型签名匹配；`collect_models=False` 不访问 HGB 缺失的 `feature_importances_` |
| 同口径证明（行数/品种/ts/网格/分组） | ✅ | 全部 6 缓存 8624 行/18 品种/ts 2019-04-01~2026-06-29；**（symbol,ts）索引集合与 v8 全同（only=0）**；网格默认 train_len=250/test_len=60/purge=5/embargo=2/rolling（`HexConfig` 默认，p13 未 override）；分组 GROUPS_V2 8 组；cal_split=0.5/cal_return_all=False 与 v8 训练路径一致 |
| F2 IC 加权只用 IS（零泄漏） | ✅ | `ic_weights_is` 逐品种按 `ts <= 2022-04-21` 过滤后算品种内时序 Spearman IC → `max(ic,0)` 按品种归一化 → 全 ≤0/NaN 回退等权；OOS 行不参与权重。独立重算：截断 OOS 后权重逐位不变（max|Δ|=0） |
| HGB 修复审查（重点） | ✅（附观察项） | ① `build_windows` 源码确认：`select_dtypes().fillna(0.0)` 只影响列选择，`gf = grp[col_order]` 取原表 → **NaN 确实进窗口**（工程师声明属实）；v8 LGB 同链路、LGB 原生处理 NaN，故与 v8 生产一致。② `_sanitize`：NaN→0、±inf→±1e6，fit/predict 均用。③ 恒定列 1e-9 确定性微扰仅 `fit`（`_prepare_train`）内做；`predict` 不做 → **不影响 XGB/LGB 路径**（XGB `_next_return` 源码无 sanitize，已用 inspect 验证；LGB 在 hexbroker 未被 P13 改动）。④ 确定性：同输入两次 fit/predict exp_ret max|Δ|=0（合成 NaN/inf/恒定列数据，n_const_jittered=20）。**观察项**：微扰是行索引单调 ramp，严格说非"字面零信息"（与训练窗时间弱相关），但幅值 1e-9 远低于特征尺度、确定性、仅 HGB 分支；且 train 微扰/predict 不微扰存在理论分布偏移，实测可复现、影响可忽略——登记为观察项，非阻塞 |
| checkpoint/resume/完整性守卫 | ✅ | 每完成一组 checkpoint 落盘（drop_duplicates keep=last）；resume 按已完品种跳过；Stage B 前校验各缓存与 v8 品种集合+行数一致，缺失/不完整直接 FAIL |
| git diff 范围 | ✅ | P13 仅新增 `scripts/p13_models.py` + `scripts/p13_multimodel.py`（git status 均为 untracked）；`hexbroker` 包零改动；公众号文章等为历史未提交文件，与 P13 无关 |

---

## 2. 独立复跑验证（自写 target 构建 + BacktestEngine）

独立实现说明：不调用 p13 融合函数（fuse_ic/ic_weights_is/per_day_rank_frame 均未用）；不调用 p5/p8_3 重估函数（engine_a_targets_cs/run_engine_row/combo_stats_row/cs_ic_summary 未用于主复现）；target 构建（每日截面 rank + min=3 + group_cap 算法）与融合权重全部自写；仅用核心引擎（BacktestEngine/CostModel/compute_metrics）与数据加载。

| 指标 | v8（报告） | v8（QA 独立） | F2（报告） | F2（QA 独立） | F3（报告） | F3（QA 独立） |
|---|---|---|---|---|---|---|
| 单引擎 OOS Sharpe | 0.626 | **0.626** ✅ | 0.679 | **0.679** ✅ | 0.585 | **0.585** ✅ |
| OOS 复利 | +8.68% | **+8.68%** ✅ | +9.35% | **+9.35%** ✅ | +8.02% | **+8.02%** ✅ |
| 组合 A10/B90 OOS Sharpe | 1.612 | **1.612** ✅ | 1.617 | **1.617** ✅ | 1.609 | **1.609** ✅ |
| OOS 截面 IC | -0.0640 | **-0.0640** ✅ | -0.0606 | **-0.0606** ✅ | -0.0385 | **-0.0385** ✅ |
| 全样本 MaxDD | -26.1% | -26.1% | -18.2% | -18.2% | -19.0% | -19.0% |
| OOS MaxDD | -6.7% | -6.7% | -6.2% | -6.2% | -6.6% | -6.6% |

生产函数路径复跑（engine_a_targets_cs + run_engine_row，核对 artifacts）亦逐位一致：v8 0.626/0.087/1.612、xgb 0.261/0.032/1.581、hgb 0.544/0.058/1.611、F1 0.585/0.089/1.608、F2 0.679/0.094/1.617、F3 0.585/0.080/1.609（OOS Sharpe/OOS 复利/组合 OOS）。

**结论：工程师报告数字在其实际评估口径下全部真实可复现。**

---

## 3. 独立 F2 交叉验证（自写 IC 加权，不调用 p13 融合函数）

流程：读 v8/xgb/hgb 三缓存 exp_ret → 每日截面 rank → IS 段（≤2022-04-21）逐品种时序 Spearman IC → max(ic,0) 按品种归一化 → 加权融合 → 与落盘 `v9_f2.parquet` 逐行对比。

**独立 F2 与落盘 F2 逐行一致：rows=8624，max|Δ|=0.000e+00，mean|Δ|=0.000e+00。独立 F3 同样逐行一致（max|Δ|=0）。**

IS 权重示例（w_v8 / w_xgb / w_hgb，按品种归一化）：
- 等权回退（IS IC 全 ≤0/NaN）：ag0、au0、sr0、ta0 → 各 1/3
- 新品种主导：i0 v8=0.326/xgb=0.674/hgb=0；jm0 v8=0/xgb=0.844/hgb=0.156；j0 v8=0.185/xgb=0/hgb=0.815；sc0 v8=1.0/xgb=0/hgb=0
- 均衡：rb0 0.352/0.331/0.317、cf0 0.385/0.296/0.319

模型多样性：三模型 exp_ret 相关性全样本 v8-xgb 0.804 / v8-hgb 0.865 / xgb-hgb 0.828；OOS 段 0.827 / 0.873 / 0.861 → 相关性高、多样性有限（与工程师诚实结论一致）。

---

## 4. 零泄漏检查

| 检查项 | 结果 | 证据 |
|---|---|---|
| F2 权重只用 IS 段 | ✅ | 截断 OOS 后重算权重与全量权重逐位一致（max|Δ|=0） |
| OOS 只用固定权重 | ✅ | 用 IS 权重+全量 rank 融合 = 落盘 F2（max|Δ|=0），即 OOS 行未回流影响权重 |
| HGB 微扰确定性 | ✅ | 同输入两次 fit/predict exp_ret max|Δ|=0（合成 NaN/±inf/恒定列，n_const_jittered=20） |
| HGB 修复不影响 XGB/LGB 路径 | ✅ | XGB `_next_return` 无 sanitize（原始 NaN 直通）；LGB 在 hexbroker 包内未被改动 |

---

## 5. 关键裁决（对 4 个问题逐项）

### 5.1 F2 是否应采纳为生产信号缓存（signal_cache 切 v9_f2）？—— **否（当前口径下不建议）**

**核心问题（DISCREPANCY-1）**：任务简报称"结果（复利口径，**生产 config 含 group_cap=0.5**）"，但实测 P13 Stage B 评估路径为：

- `p13_multimodel.py` 调用 `load_config()`（无 path）→ `OmegaConf` 空配置 + 代码默认 → **`configs/base.yaml` 未加载**；
- `engine_a_targets_cs(cache_path=...)` 内部 `_resolve_group_cap(None)` 读配置得 `None` → **未启用 group_cap；group_map 回退 GROUPS_V2 8 组**（非 ferrous_all 合并映射）。
- 复现佐证：QA 无 cap 独立复现与生产函数路径均逐位复现工程师 CSV；而 base.yaml 的 cap 值（0.5）从未参与评估。

**在真实生产口径（显式 group_cap=0.5 + ferrous_all，即 base.yaml 声明值）重跑：**

| 指标（cap=0.5 + ferrous_all） | v8 | F2 | Δ |
|---|---|---|---|
| 单引擎 OOS Sharpe | 0.646 | 0.597 | **-0.049（反转）** |
| OOS 复利 | +9.01% | +7.87% | **-1.14pp（反转）** |
| 组合 A10/B90 OOS Sharpe | 1.614 | 1.612 | -0.002（基本持平） |
| 全样本 MaxDD | -30.3% | -19.0% | 仍优（但主要在 IS 段） |
| OOS MaxDD | -6.7% | -6.6% | ~0 |

→ **F2 相对 v8 的 +0.053 单引擎/ +0.005 组合优势只存在于无 cap 口径；在生产风控口径下反转/消失**。原因机制：F2 的 IS 权重把 i0/j0/jm0 等黑色系品种大幅倾斜给 xgb/hgb（i0 w_xgb=0.674、jm0 w_xgb=0.844、j0 w_hgb=0.815），其增量收益部分来自黑色系敞口——恰被生产 group_cap=0.5+ferrous_all 约束。这与 QA 风控登记"盈利靠黑色系敞口"一致。

**收益/风险权衡**：全样本 MaxDD -26.1%→-18.2%（无 cap）看似明显，但分段诊断显示 **IS MaxDD -26.1%→-18.2%、OOS MaxDD -6.7%→-6.2%（无 cap）/-6.7%→-6.6%（cap）**——改善集中在 IS 段，OOS 风险改善 <0.1pp。生产采纳论证不应以 IS 段 MaxDD 为主要卖点。

### 5.2 OOS 截面 IC 未转正（-0.0606）——F2 采纳与 QA 风控条件是否冲突？—— **冲突未解除**

- QA 风控登记：OOS 截面 IC 为负（v8 -0.064）、盈利靠黑色系敞口。
- F2 OOS 截面 IC = -0.0606，仍为负，仅 +0.0034 微改善（F3 -0.0385 改善更大但单引擎/组合更差）。
- 预提交规则只要求"IC 改善"（> 基线），不要求"IC 转正"——机械规则 F2 通过；但**风控条件的本质（截面排序与未来收益负相关、依赖黑色系敞口）未被解除**。若采纳 F2，必须如实登记"IC 仍负、盈利仍靠黑色系敞口"。

### 5.3 单 xgb 截面 IC 最好（-0.014）但 Sharpe 最差（0.261）——解读是否正确？—— **正确**

- xgb OOS 截面 IC -0.0140（最接近 0/最好）、单引擎 OOS Sharpe 0.261（最差）——两组事实均复现。
- 解读：截面 IC 是全截面排序贴合度（含做空侧/中间名次），Sharpe 是 top30% 做多组合扣除成本后的收益——两个目标不同。xgb 排序更贴合未来收益，但做多前 30% 的收益被成本/品种结构吃掉，或其对底部分名的正确排序并未转化为多头收益。**"排序贴合 ≠ 做多赚钱"的解读成立，非矛盾。**

### 5.4 工程师诚实结论（模型多样性不是唯一瓶颈）是否合理；后续方向登记？—— **合理，登记**

- 三模型相关性 0.80~0.87（同特征/同标签/同量级 HP），多样性有限；融合仅把 IC 从 -0.064 微调到 -0.0606，未转正。
- 后续方向建议登记：**标签/任务错配是更深层瓶颈**——引擎 A 推理用途是"每日截面选 top30% 做多"，而训练任务是预测（截面化）5 日收益的绝对水平；OOS 截面 IC 为负说明模型排序与未来收益负相关，需在任务设计层（如直接优化截面排序/成对目标/排序损失、或引入市场状态条件化）修复，模型融合只是表层手段。

---

## 6. DISCREPANCY 清单

| # | 级别 | 说明 |
|---|---|---|
| 1 | **关键** | **"生产 config 含 group_cap=0.5" 与实测不符**：P13 评估实际在无 cap / GROUPS_V2 8 组下进行（`load_config()` 不加载 base.yaml，`engine_a_targets_cs` 读到 None）。在生产口径（cap=0.5+ferrous_all）下 F2 单引擎/组合优势反转。裁决 F2 采纳应基于 cap 口径重估，或明确声明评估口径为"无 cap"。 |
| 2 | 次要 | `p13_multimodel.py:432` 组合对齐 `rb = ret_b.loc[ret_b.index.intersection(ret_b.index)]` 为 no-op（应与 ra 对齐）。实测 ret_a/ret_b 索引实际一致故结果正确（组合数字逐位复现），但代码脆弱、与 p5 的 `align()` 不一致，建议修正。 |
| 3 | 观察项 | HGB 恒定列微扰严格非"字面零信息"（行索引单调 ramp 与训练时间弱相关）且 train 微扰/predict 不微扰存在理论分布偏移；实测确定性、可复现、影响可忽略——登记观察，不阻塞。 |
| 4 | 观察项 | 全样本 MaxDD"明显改善"主要在 IS 段（OOS MaxDD 改善 <0.1pp），不应作为生产切换的主要依据。 |

---

## 7. 最终 QA 裁决

**1. 数字真实性**：✅ PASS。v8 基线四项（0.626 / +8.68% / 1.612 / -0.0640）、F2（0.679 / +9.35% / 1.617 / -0.0606）、F3（0.585 / +8.02% / 1.609 / -0.0385）全部独立逐位复现；F2/F3 融合与落盘缓存逐行一致；零泄漏确认；HGB 修复确定性确认；静态审查（同构/F2 权重 IS-only/checkpoint/完整性守卫/git 范围）通过。

**2. 生产切换裁决**：**不采纳 F2 切换生产 signal_cache（保留 v8 为生产、F2 作候选）**。
- 否决理由（生产口径）：在 base.yaml 声明的生产风控配置（group_cap=0.5 + ferrous_all）下，F2 单引擎 OOS Sharpe 0.679→0.597（低于 v8 0.646）、OOS 复利 +9.35%→+7.87%（低于 v8 +9.01%）、组合 1.617→1.612（低于 v8 1.614）；唯一稳健改善（全样本 MaxDD）主要在 IS 段。
- 附：即便在评估所用无 cap 口径，改善幅度（单引擎 +0.053、组合 +0.005）远小于 P8-4 v8 采纳时的幅度（OOS 复利 -1.99%→+8.68%），且 OOS 截面 IC 未转正——不构成"必须立即切换"的证据。
- 建议路径：F2 进入 P12 S4 影子跟踪（shadow monitor），与 v8 并行观察真实 OOS；或在 cap 口径下由工程师重估后再定。若后续仍要采纳，需同时登记"IC 仍负、盈利仍靠黑色系敞口"的风控条件。

**3. 工程师修正项**：修正"生产 config 含 group_cap=0.5"的表述（评估实际无 cap）；组合对齐 no-op 建议清理；后续方向登记标签/任务错配修复（模型多样性不是唯一瓶颈，诚实结论合理）。

**QA 复核状态**：**CONDITIONAL PASS（工程正确性 PASS；生产采纳建议 = REJECT F2 切换，保留 v8 + F2 候选/影子）**。

---

## 附：独立复核脚本与产物

- 脚本：`scripts/qa_p13_independent_verify.py`（28/28 PASS；可重复执行）
- 复核输入：`artifacts/signals_cache18_grouped_{v8,v9_xgb,v9_hgb,v9_f1,v9_f2,v9_f3}.parquet`、`artifacts/p13_multimodel_compare.csv`、`artifacts/p13_model_ic.csv`、`artifacts/p13_verdict.json`
- 复核环境：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（lightgbm 4.7.0 / xgboost 3.4.1 / sklearn 1.9.0）
