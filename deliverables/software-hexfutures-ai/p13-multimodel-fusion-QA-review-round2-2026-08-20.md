# P13 多模型融合（XGB/HGB/F1/F2/F3）— QA Round 2 复核报告（cap 口径修正 + 终裁确认）

- **QA**：严过关（software-qa-engineer-2，fresh-eyes）
- **日期**：2026-08-20
- **对象**：P13 引擎 A 多模型融合——工程师对 DISCREPANCY-1（Stage B 未加载生产配置）的修正 + cap 口径终裁
- **范围**：修正质量审查（Stage B 显式 base.yaml / no-op 修复 / HGB docstring / git diff）；cap 口径数字复跑（工程师 --eval-only 复跑 + QA 独立自写 target 复跑）；终裁确认（F2 否决、三方案不采纳、维持 v8）
- **新增独立脚本**：`scripts/qa_p13_round2_verify.py`（21/21 PASS，逐位复现 cap 口径）；Round 1 脚本 `scripts/qa_p13_independent_verify.py` 终裁期望随修正更新（28/28 PASS）
- **口径**：复利口径、OOS 2024-07-18 后、cap=0.5 + ferrous_all（base.yaml 生产口径，18 键）、嵌套零泄漏、完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）、A10/B90

---

## 0. 结论摘要（TL;DR）

| 维度 | 结论 |
|---|---|
| 修正质量（Stage B / no-op / HGB docstring / git 范围） | ✅ PASS（全部落实，语义正确） |
| cap 口径数字复跑（工程师 vs QA 独立） | ✅ PASS（v8 0.646 / F2 0.597 / 组合 1.614 / 1.612，逐位一致，Δ=0） |
| 与我 Round 1 独立 cap 重跑数字对比 | ✅ 一致（0.646 / 0.597 / 1.614 / 1.612） |
| compare.csv / verdict.json 内容 | ✅ is_pass=false / adopted_fusion=null，reason 如实 |
| 终裁与 Round 1 裁决一致性 | ✅ 一致（生产口径 F2 否决、三方案不采纳、维持 v8） |
| 机制解释（IC 改善未转化为收益） | ✅ 方向成立（粗粒度佐证，非证明，见 §3） |
| **最终 QA 裁决** | **VERIFIED（无新增 DISCREPANCY；DISCREPANCY-1/2/3 均已闭环）** |

---

## 1. 修正质量审查

### 1.1 Stage B 显式加载生产配置（DISCREPANCY-1 修复）— ✅ PASS

- `scripts/p13_multimodel.py:545` 改为 `cfg = load_config("configs/base.yaml")`（显式 path），随后 `ea_cfg = cfg.backtest.engine_a` 读取 `group_cap` / `group_map` 并**显式传入** `evaluate_variant(..., group_cap=0.5, group_map=18键)` → `engine_a_targets_cs(..., group_cap=0.5, group_map=18键)`（`p13_multimodel.py:423-426, 571-572`）。
- 与 §9.18 部署接线标准一致：base.yaml 声明 `group_cap: 0.5` + 18 键 group_map（i0/j0/jm0/rb0/hc0 → ferrous_all 5 键，另有 industrial 4 / precious 2 / agri_oil 2 / agri_protein 1 / agri_soft 2 / chem_energy 2）。实测复跑打印 `生产口径: group_cap=0.5 group_map=18 键（ferrous_all 合并=True）` ✅。
- **技术链路复核**：`hexbroker/config.py:289-290` 确认 `load_config()` 无 path → `OmegaConf.create({})` + 代码默认 → `EngineAConfig` 默认 `group_cap=None`/`group_map=None`（`hexbroker/config.py:146-151` 文档明确"None 不启用 / GROUPS_V2 默认 8 组"）→ 无 path 确实读不到 base.yaml。修正后显式参数非 None → `_resolve_group_cap/_resolve_group_map`（`p5_engineA_cross_section.py:110-141`）直接返回显式值，兜底不触发；兜底本身逻辑正确（显式优先 → 无 path 配置 → GROUPS_V2/None 回退）。

### 1.2 no-op 修复（rb=ret_b∩ret_a）— ✅ PASS

- `p13_multimodel.py:443-444`：`ra = ret_a.loc[ret_a.index.intersection(ret_b.index)]`；`rb = ret_b.loc[ret_b.index.intersection(ret_a.index)]`。两序列现均取交集（交集对称，语义与 p5 `align()` 一致：`common = a.index.intersection(b.index)`，`p5_engineA_cross_section.py:535-537`）。DISCREPANCY-2 闭环。

### 1.3 HGB docstring 微扰观察项标注 — ✅ PASS

- `scripts/p13_models.py:154-157`：明确标注"⚠️ 观察项（QA 复核登记）：该微扰并非字面零信息……对模型预测的影响应视为可忽略但不为零。LightGBM/XGBoost 原生把 NaN 当缺失值处理，不受影响"。DISCREPANCY-3 闭环。

### 1.4 git diff 范围 — ✅ PASS

- `hexbroker/` 包 0 改动（tracked 修改数 = 0）；`scripts/` 无 tracked 修改（p13_models.py / p13_multimodel.py / qa 脚本均为 untracked 新文件）；`artifacts/` 在 .gitignore 中（compare.csv / verdict.json 为输出物）。公众号文章等为历史未提交文件，与 P13 无关。范围干净。

---

## 2. cap 口径数字复跑对比

### 2.1 工程师复跑复现（QA 执行 `--eval-only`，71s）

QA 在本机复跑修正后的 `scripts/p13_multimodel.py --eval-only`（Stage B 重估 ~70s，缓存复用不重训）：

| 指标（cap=0.5+ferrous_all） | v8 | xgb | hgb | F1 | F2 | F3 |
|---|---|---|---|---|---|---|
| 单引擎 OOS Sharpe | 0.646 | 0.526 | 0.494 | 0.562 | **0.597** | 0.486 |
| OOS 复利 | +9.01% | +9.26% | +5.69% | +7.19% | +7.87% | +6.20% |
| 组合 A10/B90 OOS Sharpe | 1.614 | 1.595 | 1.605 | 1.610 | **1.612** | 1.603 |
| OOS 截面 IC | -0.0640 | -0.0140 | -0.0580 | -0.0470 | **-0.0606** | -0.0385 |
| 全样本 MaxDD | -30.3% | -16.5% | -29.8% | -18.2% | -19.0% | -22.2% |
| OOS MaxDD | -6.7% | -8.1% | -4.5% | -6.4% | -6.6% | -6.5% |

与工程师声明（v8 0.646/+9.01%/1.614/-0.0640；xgb 0.526/1.595；hgb 0.494/1.605；F1 0.562/1.610；F2 0.597/1.612；F3 0.486/1.603）**逐位一致**。

### 2.2 QA 独立自写 target 复跑（fresh-eyes，21/21 PASS）

`scripts/qa_p13_round2_verify.py`：不调用 p13/p5 重估函数，显式 `load_config("configs/base.yaml")` → 自写 P9-2 cap 算法（capped_selection）+ BacktestEngine 重跑 v8/F2/F3 → 与复跑后的 compare.csv 对比：

- v8：ind OOS_Sharpe 0.6460 vs ship 0.6460（Δ=0）；OOS_ret 0.0901 vs 0.0901（Δ≈7e-17）；组合 1.6137 vs 1.6137（Δ=0）；IC -0.0640 vs -0.0640（Δ≈3e-17）
- F2：0.5967 / 0.0787 / 1.6118 / -0.0606 — 全部 Δ≈0
- F3：0.4857 / 0.0620 / 1.6027 / -0.0385 — 全部 Δ≈0

### 2.3 与我 Round 1 独立 cap 重跑数字对比 — ✅ 一致

| 指标 | Round 1 QA 独立（报告 §5.1） | Round 2 复跑（ship） | 一致 |
|---|---|---|---|
| v8 单引擎 OOS Sharpe | 0.646 | 0.6460 | ✅ |
| F2 单引擎 OOS Sharpe | 0.597 | 0.5967 | ✅ |
| v8 组合 A10/B90 OOS | 1.614 | 1.6137 | ✅ |
| F2 组合 A10/B90 OOS | 1.612 | 1.6118 | ✅ |
| v8 OOS 复利 / F2 OOS 复利 | +9.01% / +7.87% | +9.01% / +7.87% | ✅ |

### 2.4 compare.csv + verdict.json 内容核对 — ✅

- `p13_multimodel_compare.csv`：cap 口径全表（上表数值，含 volN/volY 组合行）与复跑一致。
- `p13_verdict.json`：`is_pass=false`、`adopted_fusion=null`；baseline oos_sharpe_a=0.64599 / combo_oos_sharpe=1.61373 / oos_cs_ic=-0.06396；details 中 F1/F2/F3 delta 与复跑一致；reason 如实表述"无融合方案同时改善三项关键指标 → 不采纳；收益瓶颈不在模型多样性"。✅

---

## 3. 终裁确认

### 3.1 工程师终裁 vs Round 1 QA 裁决 — ✅ 一致

工程师"F2 否决、三方案均不采纳、维持 v8（IS_PASS: NO）"与我 Round 1 裁决"**不采纳 F2 切换生产 signal_cache（保留 v8 为生产、F2 作候选）** / 生产采纳 REJECT"一致。终裁规则复现（make_verdict）：cap 口径下 F1/F2/F3 的 OOS 截面 IC 均改善（+0.0169 / +0.0034 / +0.0254），但单引擎与组合 Sharpe 全部未超基线（F1 -0.083/-0.004、F2 -0.049/-0.002、F3 -0.160/-0.011）→ 无方案同时满足三项 → `is_pass=false / adopted=null` ✅（与落盘 verdict 一致）。

### 3.2 机制解释复核 — ✅ 方向成立（粗粒度佐证）

"融合改善 OOS 截面 IC 但未转化为引擎收益（黑色系敞口被 cap 约束）"：
- **IC 改善属实**：F1/F2/F3 OOS 截面 IC 均优于 v8（-0.0470 / -0.0606 / -0.0385 vs -0.0640）。
- **收益未转化属实**：三方案单引擎 + 组合 OOS Sharpe 全部低于 v8。
- **黑色系敞口被约束（佐证）**：QA 独立测算 OOS 段 ferrous_all 组持仓占比——cap 下 F2 30.1%→26.4%（-3.7pp）降幅大于 v8 30.4%→28.0%（-2.4pp）；F2 在 cap 下黑色系敞口被削更多，与其无 cap 增量收益部分来自黑色系敞口一致。
- 诚实说明：持仓占比是**粗粒度佐证**，非敞口贡献的精确分解；但方向与 Round 1 结论（F2 IS 权重把 i0/j0/jm0 大幅倾斜给 xgb/hgb → 增量收益依赖黑色系敞口）一致，机制解释成立。
- 附带观察：xgb 单引擎 OOS Sharpe 在 cap 口径 0.261→0.526 大幅改善（cap 约束黑色系敞口对 xgb 有利），进一步佐证"无 cap 口径的收益结构受黑色系敞口驱动"——不影响裁决，记录备查。

### 3.3 遗漏项判断 — 无阻塞项

- **F2 无 cap 对照归因拆分**：**判断为不必要（非阻塞，可选 sensitivity）**。裁决口径是生产口径（cap=0.5+ferrous_all），该口径下 F2 已决定性失败（单引擎 0.597<0.646、组合 1.612<1.614）；无 cap 对照的归因分解（把 F2 无 cap 超额收益拆到品种/组敞口贡献）只能解释"为什么无 cap 下 F2 看起来更好"，不改变 cap 口径下的否决。工程师主动提出可补跑——可选，非阻塞，若补跑建议作为 sensitivity 记录而非裁决依据。
- 未发现其他遗漏 DISCREPANCY。

### 3.4 非阻塞观察项（建议清理，不阻塞）

1. `p13_multimodel.py:634-635` make_verdict 注释基线数字仍为旧无 cap 口径（"+0.626 / 1.612"），代码实际从表取基线（0.646 / 1.614）——逻辑正确，注释过时，建议顺手清理。
2. Round 1 QA 脚本 `qa_p13_independent_verify.py` 的 [G] 终裁期望原为无 cap 口径（F2 采纳），已随 cap 口径修正更新为 is_pass=false/adopted=null（脚本现 28/28 PASS；历史无 cap 数字复现检查保留，作历史记录）。

---

## 4. 最终 QA 裁决

**1. 修正质量**：✅ PASS。Stage B 显式 `load_config("configs/base.yaml")` + 显式传 group_cap=0.5 / group_map=18 键（对照 §9.18 部署接线标准）；`_resolve_group_cap/_resolve_group_map` 兜底正确且修正后不触发；no-op 修复（rb=ret_b∩ret_a）语义与 p5 align() 一致；HGB docstring 观察项标注到位；git diff 范围干净（hexbroker 0 改动、仅 untracked 新脚本 + gitignored 输出物）。

**2. cap 口径数字复跑**：✅ PASS。工程师复跑（QA 本机 `--eval-only` 71s）与工程师声明逐位一致；QA 独立自写 target 复跑 21/21 PASS（v8/F2/F3 全部 Δ≈0）；与我 Round 1 独立 cap 重跑数字（0.646 / 0.597 / 1.614 / 1.612）完全一致；compare.csv / verdict.json（is_pass=false / adopted_fusion=null）内容正确。

**3. 终裁确认**：✅ 与 Round 1 裁决一致。生产口径下 F2 否决、三方案不采纳、维持 v8；机制解释（IC 改善未转化为收益、黑色系敞口被 cap 约束）方向成立；无遗漏阻塞项（F2 无 cap 归因拆分为可选非必要）。

**QA Round 2 复核状态**：**VERIFIED（无新增 DISCREPANCY；DISCREPANCY-1/2/3 全部闭环；终裁确认：IS_PASS=NO，adopted_fusion=null，维持 v8 生产，F1/F2/F3 不采纳，F2 可入 P12 S4 影子跟踪）**。

---

## 附：Round 2 复核脚本与产物

- 新增脚本：`scripts/qa_p13_round2_verify.py`（21/21 PASS，自写 target + 显式 base.yaml 独立复跑 + 机制占比验证 + 终裁规则复现）
- 更新脚本：`scripts/qa_p13_independent_verify.py`（[G] 终裁期望随 cap 口径修正，28/28 PASS）
- 复核输入：`artifacts/p13_multimodel_compare.csv`、`artifacts/p13_model_ic.csv`、`artifacts/p13_verdict.json`、`artifacts/signals_cache18_grouped_{v8,v9_xgb,v9_hgb,v9_f1,v9_f2,v9_f3}.parquet`
- 复核环境：`C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
