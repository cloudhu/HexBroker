# P8 引擎 A 信号质量迭代报告（基差特征实验 + 根因深化）

**日期**：2026-08-19 ｜ **流程**：主理人诊断（P8-1）→ 工程师实现（P8-2）→ QA fresh-eyes 复核（VERIFIED + 根因深化）→ 主理人终裁

## TL;DR

| 项目 | 结论 |
|---|---|
| P8-1 诊断 | 引擎 A 特征体系纯量价（无基本面特征）→ 嫌疑"缺基差特征" |
| P8-2 实验 | 加入基差特征（f_basis_ratio/rank/z）重训 v5 → **引擎 A OOS -0.024→-0.335 恶化** |
| **QA 根因深化** | 基差特征截面 IC **-0.059**（≈0/负）——基差是**品种内时序信号**，天生无截面排序力 → **训练任务（时序预测）与推理用途（截面选品种）错配** |
| **主理人终裁** | v5 不采纳；基差特征模块保留默认关闭；**根因=任务/用途错配，重构方向=标签/训练目标截面化** |

---

## 一、P8-1 特征缺口诊断（主理人）

- 当前特征：technical(12) + microstructure(7) + iterative(7) + cross(外盘) + normalize——**纯量价**
- 基差是已验证 alpha 源（引擎 B OOS 1.26~1.62；品种内时序 IC +0.047）
- **直接嫌疑**：引擎 A 训练从未见过基差因子

## 二、P8-2 基差特征实验（工程师，QA 独立复现）

### 实现（全部 QA 零泄漏检查 PASS）
- 新增 `hexbroker/feature/fundamental.py`：f_basis_ratio / f_basis_ratio_rank(252,60) / f_basis_ratio_z / f_basis；滚动在原始基本面日期网格计算后 ffill 对齐（无前视）
- pipeline 支持 fundamental_data（**默认不启用**，不破坏 239 测试）；group_modeling 显式启用
- 重训 v5 缓存（8624 行，覆盖率与 v4 完全一致，纯信号质量变化）

### 结果（QA 精确复现）
| 指标 | v4 基线 | v5（含基差） | Δ |
|---|---:|---:|---:|
| 引擎 A S2 OOS Sharpe | -0.024 | **-0.335** | **-0.311** |
| 组合 A15/B85 OOS | 1.384 | 1.343 | -0.040 |
| 特征重要性 | — | f_basis* 合计 9-18%（模型确实用上） | — |
| 分组 IC | — | agri_oil +0.134 改善 / chem_energy -0.064 恶化 | — |

## 三、QA 根因深化（本轮核心产出）

1. **v5 恶化真实无混杂**：独立复现 + 全量重训位级一致（排除随机种子风险）
2. **关键新证据**：v5 exp_ret 截面 IC（-0.040）**优于** v4（-0.063），但 P&L 反而恶化 → **问题在"信号→P&L"层**
3. **根因**：f_basis_ratio_rank 截面 IC = **-0.059**（≈0/负）——基差是品种内时序信号（引擎 B 时序 IC +0.025），天生无截面排序力。developer-guide P2 早已记录"截面 IC 是错误口径"
4. **结论**：**训练任务（LightGBM 时序预测 exp_ret）与推理用途（每日截面选 top30% 品种）错配**——模型学的是"该品种未来涨跌"，用的却是"当日哪几个品种最强"

## 四、主理人终裁

1. **v5 不采纳**，生产维持 v4 + P6-5 配置（组合 OOS 1.384）
2. **基差特征模块保留默认关闭**（已就绪、零泄漏、11 测试通过，供引擎 A 重构后复用）
3. **根因定论**：引擎 A 瓶颈不是缺特征，而是**任务/用途错配**——重构方向 = **标签/训练目标截面化**（如直接训练截面排序目标：当日各品种的相对强弱/rank 标签，而非绝对未来收益）
4. **方法论沉淀（QA 建议采纳）**：
   - 后续引擎 A 实验**先做目标用法维度的单特征 IC 预检**（本次应重训前置）
   - 引擎 B 的时序信号（基差）不应直接作为引擎 A 截面选择特征（口径错配）
   - 可选：`_lgbm_kwargs()` 显式加 `random_state` 提升可复现性

## 五、文件清单

| 文件 | 说明 |
|---|---|
| `hexbroker/feature/fundamental.py` | 新增基差特征模块（默认关闭，测试通过） |
| `hexbroker/feature/pipeline.py` / `config.py` / `scripts/group_modeling*.py` | 管线/配置/信号生产接入 |
| `tests/test_fundamental_features.py` | 11 例（零泄漏/默认关闭） |
| `artifacts/signals_cache18_grouped_v5.parquet` | 实验缓存（不采纳，保留） |
| `artifacts/p8_basis_importance.json` / `p8_engineA_basis_compare.csv` / `p8_group_ic.csv` | 实验数据 |
| `deliverables/software-hexfutures-ai/p8-2-basis-engineA-QA-review-2026-08-19.md` | QA 复核报告 |
| `docs/developer-guide.md` | 更新至 v3.9（§9.14） |
