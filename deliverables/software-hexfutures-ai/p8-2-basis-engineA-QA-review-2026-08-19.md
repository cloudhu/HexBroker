# P8-2 引擎 A 基差特征实验 QA 独立复核报告（fresh-eyes）

- **复核人**：Edward（严过关，QA）
- **日期**：2026-08-19
- **范围**：`hexbroker/feature/fundamental.py` + pipeline/config/group_modeling 改动 + v5 缓存重估 + 裁决审查
- **方法**：独立重实现交叉验证（不调用 p8/p5 函数）+ 独立零泄漏参考实现 + 全量重训确定性验证

---

## 0. 复核结论速览

| 项目 | 结论 |
| --- | --- |
| 零泄漏（静态 + 独立验证） | **PASS**（4/4 独立检查全过） |
| 11 例 fundamental 单测 | **11/11 PASS** |
| 全量测试套件 | **239 passed / 0 failed**（含 data/sources；工程师称 224，见 DISCREPANCY-1） |
| v5 缓存独立性 | **完全复现**（8624 行 / 662 天 / 日均13.0 / OOS 176 天 / 13.1） |
| 引擎 A S2 OOS 重估 | **精确复现** v4 **-0.024** → v5 **-0.335**（Δ **-0.311**） |
| 组合 A15/B85 OOS | **精确复现** v4 **1.384**（vol=N）/1.393（vol=Y）→ v5 **1.343**/1.347 |
| 训练确定性 | **已验证确定性**（全量重训位级复现 v5）→ v4/v5 差异无种子混杂 |
| 关键裁决 | **同意 v5 不采纳 + 模块默认关闭**；"瓶颈在模型/标签"**成立且有新增证据** |
| 最终 QA 裁决 | **QA VERIFIED（通过，附 3 条 DISCREPANCY 与 2 条建议）** |

---

## 1. 静态审查表

### 1.1 `hexbroker/feature/fundamental.py`（新文件，5586B）

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| ffill 对齐只用当日及以前 | ✅ | `br_raw.reindex(out.index).ffill()`：K 线日 t 取基本面日期 ≤ t 的最新值；早于首个基本面日 → NaN |
| 滚动分位/z-score 尾随无前视 | ✅ | `rolling(252, min_periods=60)` 为尾随窗口；`_rolling_zscore` 均值/标准差同为尾随，`ddof=0`，std=0 → NaN 防御 |
| 滚动在原始基本面日期网格计算后 ffill | ✅ **正确做法** | 避免先 ffill 成日频后再滚动导致的重复值计数扭曲；与引擎 B `load_basis_panel` 形态一致 |
| 缺失处理 | ✅ | 无数据品种 → 全 NaN → `build_windows` 训练前 fill 0.0（中性填充），因果 |
| 输入规范化 | ✅ | 去重（keep last）+ 升序 + float；缺 `basis_ratio` 列显式 raise |
| 参考实现逐点对比 | ✅ | 独立纯循环实现（含 15% 缺失日）与模块输出**逐日完全一致**（见 §3） |

### 1.2 `hexbroker/feature/pipeline.py`

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| fail-fast | ✅ | `transformers` 含 `fundamental` 但未传 `fundamental_data` → `ValueError`；测试 `test_pipeline_fundamental_fail_fast` 覆盖 |
| 排除在滚动 z-score 之外 | ✅ | `norm_cols = [c for c in feat_cols if not c.startswith("f_basis")]`，基差特征保持无量纲/原始形态 |
| 默认不启用 | ✅ | 默认 `transformers=["technical","microstructure","normalize"]` 不变；`build_features` 新增参数默认 `None`，向后兼容 |
| 分支顺序 | ✅ | fundamental 在 cross 后、normalize 前注入；特征名白名单裁剪兼容 |

### 1.3 `hexbroker/config.py` / `scripts/group_modeling*.py`

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| `FeatureConfig.fundamental_params` 默认 | ✅ | `default_factory=dict`，生产默认空 |
| 生产引擎 A 指向 | ✅ | `engine_a.signal_cache = artifacts/signals_cache18_grouped_v4.parquet`（v4 未动，mtime 18:29:39 与 P8-2 前一致） |
| 生产引擎 B / 组合 | ✅ | win=252 / thr=0.70 / A15:B85，均未被 P8-2 改动 |
| 分组建模显式启用 | ✅ | `group_modeling.py` 显式加 `"fundamental"` + `fundamental_params`；`load_fundamental_data` 缺失品种跳过（WARN）不中断 |
| 特征重要性采集 | ✅ | `--collect-importance` 按组聚合（各组 global_codes 不同不可跨组合并，处理正确） |
| git diff 范围 | ✅ | P8-2 专属改动 = fundamental.py（新）+ pipeline.py/config.py/group_modeling 增量 + test_fundamental_features.py（新）；v4 缓存与既有数据文件未动；工作树中另有 P6-3/P7 未提交改动（cal_return_all 等），与 P8-2 正交 |

---

## 2. 复跑对比（真实输出）

### 2.1 测试
```
tests\test_fundamental_features.py ...........  [100%]  11 passed in 0.50s
全量默认套件（tests + hexbroker/data/sources）: 239 passed in 64.75s
相关专项（test_refine_lightgbm + splitter_leakage + pipeline_e2e + config）: 22 passed
```

### 2.2 v5 缓存覆盖率（独立核验）
```
[cov v4] rows=8624 days=662 avg=13.0 | OOS days=176 OOS avg=13.1
[cov v5] rows=8624 days=662 avg=13.0 | OOS days=176 OOS avg=13.1
[sig diff] p_up 改变率=57.7% | exp_ret 改变率=100.0%
[sig corr] exp_ret corr=0.809 | p_up corr=0.749
```
与工程师报告**逐位一致**（avg=13.027190332326285 精确一致）。

### 2.3 独立交叉验证：引擎 A S2 + 组合（自写代码，未调用 p8/p5 函数）
```
[A-S2 v4] full Sharpe=0.682 ann=+9.7% MaxDD=-19.7% | OOS Sharpe=-0.024 OOS ret=-2.0%
[A-S2 v5] full Sharpe=0.607 ann=+8.4% MaxDD=-20.1% | OOS Sharpe=-0.335 OOS ret=-8.6%
[combo v4 A0.15/B0.85 vol=N] OOS Sharpe=1.384 | vol=Y OOS Sharpe=1.393
[combo v5 A0.15/B0.85 vol=N] OOS Sharpe=1.343 | vol=Y OOS Sharpe=1.347
```
**精确复现**：ΔOOS Sharpe = **-0.311**；组合 Δ = **-0.040/-0.046**。与 `artifacts/p8_engineA_basis_compare.csv` 完全一致。

### 2.4 S1–S4 稳健性 + A–B 相关性（独立验证）
```
S1: v4 -0.024 -> v5 -0.335 (Δ -0.311)     S3: v4 -0.006 -> v5 -0.311 (Δ -0.305)
S2: v4 -0.024 -> v5 -0.335 (Δ -0.311)     S4: v4 -0.006 -> v5 -0.311 (Δ -0.305)
A-B OOS corr: v4 0.618 -> v5 0.561
```
与工程师报告完全一致。

### 2.5 特征重要性（`p8_basis_importance.json` 独立核验，8 组）
```
precious 9.0% | ferrous_steel 11.3% | ferrous_raw 15.8% | industrial 11.1%
agri_oil 18.0% | agri_protein 10.7% | agri_soft 13.4% | chem_energy 12.9%
```
f_basis_ratio_rank 在 8 组 rank 14–19/26–32，f_basis_ratio 在 agri_oil rank 3/26（5.78%）。**模型确实用上基差**（与报告一致）。

### 2.6 训练确定性验证（QA 新增关键验证）
冠军超参 `lgbm_subsample=0.683 / lgbm_colsample_bytree=0.967` 且 `LightGBMForecast._lgbm_kwargs()` **未设 random_state** → 理论上有种子不确定性。为此 QA 做了**两次同设置重跑 + 全量重训**：

- precious 组重跑 run1/run2（同 v5 设置）→ 与原始 v5 的 au0/ag0 信号**位级一致**（exp_ret/p_up max diff = 0.0）
- **全量 8 组重训（24m13s）** → 重生成的 v5 缓存与原始 v5 所有统计**逐位一致**（rows/days/avg=13.027190332326285/p_up 改变率 57.676252319109466%/corr=0.8087062270899796）
- 结论：**该环境训练确定性成立** → v4 vs v5 差异**无种子混杂**，Δ-0.311 可归因于基差特征本身（非重训噪声）

---

## 3. 零泄漏独立检查（真实输出）

QA 自写参考实现（纯循环，明显因果）与模块输出对比 + 扰动测试 + 真实数据网格级抽样：

```
[1] 参考实现对比（含 15% 缺失基差日）: PASS 完全一致
  [合成: 改未来不影响过去]: PASS
  [真实au: 改未来不影响过去]: PASS
  [合成: 当天值改变 → 当天特征应改变(未误判为无前视)]: PASS
[3] 网格级抽样验证（真实 au，30 日）：PASS 全部一致
零泄漏检查总体: PASS
```

另核验：18 个基差数据文件均无重复日期（dup_dt=0），2018–2026 覆盖完整；`load_fundamental_data` 短名映射（`_norm_sym`）与管线 `sym_label` 一致。

---

## 4. 关键裁决

### 4.1 "v5 OOS 恶化是否真实、是否混杂" → **真实，无混杂**
- 独立重实现精确复现 -0.311（§2.3）
- 恶化集中在 **2025H2（+1.50%→-3.94%）与 2026H1（+4.44%→+0.65%）**，即最近数据段持续变差，非单段 fluke
- S1–S4 四种稀疏策略一致恶化（-0.305~-0.311），非策略选择假象
- **确定性验证**排除了种子混杂（§2.6）
- 选中信号质量下降：top30% 选中集的 OOS p_up 0.954→0.942、exp_ret 0.0774→0.0714

### 4.2 "瓶颈在模型/标签层面"推断 → **成立，且有新增证据**
QA 独立 IC 分析（OOS，日截面 + 品种内时序）：
```
[引擎B 品种内时序 IC] br_rank vs realized: +0.0252（基差确有时序 alpha）
[引擎A 视角 - 基差特征截面 IC] f_basis_ratio: -0.042 | f_basis_ratio_rank: -0.059 | f_basis_ratio_z: -0.058
[引擎A 信号] v4 exp_ret 截面 IC: -0.063 | v5 exp_ret 截面 IC: -0.040
```
关键证据链：
1. **v5 的 exp_ret 截面 IC（-0.040）其实优于 v4（-0.063）**（与分组 IC 部分改善一致），但 OOS P&L 反而恶化 → 改善的排序质量未能转化为收益，问题出在"信号→P&L"层
2. **v4/v5 的 exp_ret OOS 截面 IC 均为负** → 引擎 A 的核心病灶是 exp_ret 的截面排序能力本身为负，与特征体系无关
3. 基差特征本身在引擎 A 用法下**截面 IC ≈ 0/负**（-0.04~-0.06）——基差是品种内时序信号，无截面排序力（与 developer-guide P2 已记录的教训一致："基差收敛是品种内时序信号，截面 IC 是错误口径——不同品种基差率水平不可比（RB 6~10% vs AU -0.6% vs SC -50%）"）

### 4.3 "v5 不采纳 + 模块默认关闭" → **合理**
- v5 全指标劣于 v4（单引擎/组合/全部稀疏策略/OOS 分段），生产维持 v4 缓存正确
- 模块默认关闭不破坏既有 224/239 测试，向后兼容 ✓
- 保留模块供后续复用合理（零泄漏 + 测试通过 + 特征重要性已证明模型可用上）

### 4.4 工程师是否遗漏其他候选方向 → **是，两条**
1. **（重要）特征-引擎用法错配可预判**：引擎 A 是"特征→LightGBM→exp_ret→日截面 top30% 选品种"；基差特征（f_basis_ratio_rank/z）是**品种内归一化的时序信号**，天生不提供截面排序信息。P2 已在 developer-guide 记录"截面 IC 是错误口径"。工程师自己也建议"先做单特征 IC 验证 f_basis_ratio_rank 在 OOS 的截面表现"——**该验证应是重训前置步骤而非事后建议**，QA 已代为执行并确认截面 IC≈负（-0.059），说明本实验从特征形态上就难以成功。
2. **（结构性）训练-推理任务错配是瓶颈本质**：引擎 A 训练目标是"品种内下 5 日收益预测"（时序任务），推理用途是"跨品种比较 exp_ret"（截面任务）——两者不同。任何只提升时序预测精度的特征（含基差）都不会必然改善截面排序。建议后续：直接在截面任务上训练/验证（如每折输出做日截面 IC 作为目标），或重构标签（如截面相对收益/截面 rank 目标），比继续加特征更对症。

### 4.5 DISCREPANCY 说明
- **D-1（轻微）**：工程师称"不破坏 224 测试"，实际当前全量默认套件为 **239 passed**（含 hexbroker/data/sources）；数字差异不影响结论（全部通过，无失败）。
- **D-2（QA 操作记录）**：复核过程中 QA 以 `--collect-importance`（默认输出路径）重跑单组，一度覆盖 `p8_basis_importance.json`；已通过**全量重训**恢复（8 组、30665B，与原始逐位一致）；`signals_cache18_grouped_v5.parquet` 亦被 QA 重训覆盖，内容位级一致（见 §2.6）。当前产物状态完好：v4 缓存 mtime 18:29:39 未动、v5 内容一致、importance JSON 已恢复。
- **D-3（口径说明）**：v4 生产缓存的训练确定性未单独重跑验证（同机制下 precious 位级一致 + 全量 v5 复现，机制确定性已充分证明），如需 100% 可另跑一次 v4 全量重训（约 24 分钟）。

---

## 5. 最终 QA 裁决

**QA VERIFIED（通过）**

1. **零泄漏**：fundamental 特征严格因果（静态审查 + 4 项独立验证全过）——**无 DISCREPANCY**。
2. **复现性**：工程师全部量化声明（覆盖率、A-S2 OOS、组合、S1-S4、A-B 相关性、特征重要性、信号差异）均被独立重实现**精确复现**。
3. **裁决**：同意 **v5 不采纳、生产维持 v4、基差特征模块保留默认关闭**；"瓶颈在模型/标签层面"结论成立，QA 新增证据（exp_ret 负截面 IC + 特征-用法错配 + 训练-推理任务错配）进一步强化该判断。
4. **遗留建议**（非阻塞）：
   - 后续引擎 A 实验前，先做单特征在**目标用法维度**（截面）的 IC 预检，避免 24 分钟级重训试错；
   - 引擎 A 重构方向应聚焦标签/训练目标（截面化），而非继续堆特征；
   - 可选：为 `LightGBMForecast._lgbm_kwargs()` 显式加 `random_state`（当前环境实际确定，但显式化更稳健，便于跨机复现）。
