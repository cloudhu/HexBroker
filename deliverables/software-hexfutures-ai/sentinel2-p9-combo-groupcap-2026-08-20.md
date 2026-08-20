# P9 组合级验证 + 黑色系敞口风控报告

**日期**：2026-08-20 ｜ **流程**：工程师实现 → QA fresh-eyes 复核（VERIFIED 附条件）→ 主理人终裁

## TL;DR

| 项目 | 结论 |
|---|---|
| 组合网格 | vol=N 最优 **A10/B90**（OOS Sharpe 1.612 vs 基线 1.602；A15/B85 零漂移复现） |
| 黑色系敞口 | full mean 32.8%（>50% 67 天/12.2%）；OOS mean 26.5%（>50% 16 天/10.0%） |
| group_cap=0.5 | 消除极端集中（>50% 天数→0），OOS 性能中性（QA 修正：性能增益不稳健，纯风控价值） |
| **QA 修正** | A10/B90 是网格**下边界**最优（真正最优可能 <0.10）；cap 增益对 refill 规则敏感 |
| **主理人终裁** | **A10/B90 + vol_target=False 固化**；vol=Y 维持候选不翻转 P4-1；group_cap 保留为杠杆（下轮加 group_map 后显式启用） |

---

## 一、组合网格（P9-1，12 配置，复利口径，QA 逐位复现）

| rank | w_a | w_b | vol | OOS Sharpe | OOS 复利 | OOS MaxDD | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | 0.10 | 0.90 | Y | 1.662 | +43.79% | -8.1% | 全网格最优（但 MaxDD 深） |
| 2 | 0.15 | 0.85 | Y | 1.636 | +41.83% | -7.9% | |
| 3 | **0.10** | **0.90** | **N** | **1.612** | **+28.70%** | -6.1% | ⭐ vol=N 最优 |
| 5 | 0.15 | 0.85 | N | 1.602 | +27.54% | -5.9% | P8-4 基线（零漂移） |
| 7 | 0.25 | 0.75 | Y | 1.572 | +38.06% | -7.6% | |
| 12 | 0.35 | 0.65 | Y | 1.494 | +34.48% | -7.2% | |

**QA 关键修正**：A10/B90 是网格**下边界**最优（vol=N OOS Sharpe 随 w_a 严格单调递减；全样本反而单调递增，A10 全样本 1.023 < A15/B85 1.049）→ 真正最优可能 <0.10，需补测 A5/A0。

## 二、黑色系敞口 + group_cap（P9-2）

- **敞口统计**：黑色系（i0/j0/jm0/rb0/hc0）每日选中占比 full mean 32.8% / p90 60% / max 83.3%（>50% 67 天）；OOS mean 26.5% / >50% 16 天
- **group_cap=0.5 对照**：>50% 天数 16→0（极端集中构造性消除）；引擎 A OOS 0.626→0.646、组合 1.602→1.605
- **QA 修正（重点）**：cap 性能增量对 **refill 规则敏感**——工程师"重扫补满" vs QA"跳过不回头"在 252/547 天选品不同 → cap OOS Sharpe 0.646 vs 0.656、复利 +9.01% vs +8.54%（QA 变体甚至低于基线 +8.68%）→ **cap 只能当纯风控（风险降低稳健、性能中性），不能当性能增强**

## 三、配置固化（P9-3）

- `ComboConfig`：w_engine_a **0.15→0.10** / w_engine_b **0.85→0.90**（vol=N 网格最优）
- `EngineAConfig`：新增 `group_cap: float | None = None`（默认不启用）
- p6_5_config_validate.py 更新（修复原 4 FAIL——旧脚本仍期待 v4）；pytest 197 通过；旧 yaml 兼容

## 四、主理人终裁

1. **A10/B90 + vol_target=False 固化**（QA 放行，PASS with caveats）：
   - 方向与先验一致（引擎 A OOS 截面 IC 负 → 降权重更稳健）+ B 主导组合低风险
   - 注意：+0.010 在噪声内、全样本反降、网格边界——文案标注"与 A15/B85 等价，因先验选 A10"
   - 遗留：补测 A5/B95、A0/B100（下轮）
2. **vol=Y 维持候选，不翻转 P4-1 终裁**（QA 裁决）：vol=Y 只在 w_a 低时更好，增益与权重交互；推翻前终裁需专项（OOS 分段稳定性 + VOL_TARGET 敏感性 + 模拟盘）
3. **group_cap 默认不启用，保留为风控杠杆**（QA 裁决）：当前 EngineAConfig 无 group_map 字段，默认 GROUPS_V2 把黑色系分两组 → cap=0.5 无法实现"合并黑色系≤50%"目标；**下轮加 group_map 配置后部署 yaml 显式启用**（零成本、消除尾部集中度风险）
4. **遗留 3 项登记**：① git 提交（P 系列脚本全部 untracked）② group_map 配置落地 ③ A10 边界补测

## 五、文件清单

| 文件 | 说明 |
|---|---|
| `hexbroker/config.py` | ComboConfig A10/B90 + EngineAConfig.group_cap |
| `scripts/p5_engineA_cross_section.py` | +group_cap/group_map（默认 None 向后兼容） |
| `scripts/p9_combo_validation.py` | 组合网格编排 |
| `scripts/p6_5_config_validate.py` / `p6_config_validate.py` | 验证更新（100% PASS） |
| `artifacts/p9_combo_grid.csv` / `p9_ferrous_exposure.csv` / `p9_group_cap_compare.csv` | 实验数据 |
| `deliverables/software-hexfutures-ai/p9-combo-groupcap-QA-review-2026-08-20.md` | QA 复核报告 |
| `docs/developer-guide.md` | 更新至 v3.12（§9.17） |
