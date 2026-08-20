# P8-3 标签截面化重构报告（QA 关键修正）

**日期**：2026-08-19 ｜ **流程**：工程师实现（v6/v7 双变体）→ QA fresh-eyes 复核（VERIFIED + 度量假象修正）→ 主理人终裁

## TL;DR

| 项目 | 结论 |
|---|---|
| 实现 | 标签截面化三变体（cross_rank / cross_z / cross_demean），默认 absolute 不变，零泄漏 QA 验证 |
| v6 cross_rank | OOS 截面 IC 显著改善（-0.063→+0.035）但引擎 A OOS 恶化（-0.024→-0.389）——跨组尺度错配，**不采纳** |
| v7 cross_z | 引擎 A OOS 算术 +0.004、组合 1.384→1.399——但 **QA 修正：复利口径仍亏损 -1.63%，是"亏得少一点"的度量假象**，**仅作候选** |
| **主理人终裁** | **v4 维持生产**；v7 作候选不进生产；标签截面化方向已验证（P8 根因诊断正确），但需解决跨组可比性 |

---

## 一、实现（工程师，QA 零泄漏 PASS）

### 标签截面化（scripts/refine_lightgbm_champion.py）
| label_mode | 变换 | 说明 |
|---|---|---|
| absolute（默认） | 无 | 现行为，回归绝对 fwd 收益 |
| cross_rank | 当日截面分位 [0,1] | 越大越强 |
| cross_z | 当日截面 z-score | 跨组可比 |
| cross_demean | 当日去均值 | 相对收益 |

- 无前视：截面化只用**当日**各品种 fwd（axis=1 行统计），与折边界无关
- 退化处理：品种<2 / σ=0 / 单品种组 m0 → 保留原始 fwd
- 向后兼容：默认 absolute 与现行为逐字节等价（192 测试通过）

### 重训（覆盖率 v4=v6=v7 完全一致：8624 行/662 天）
- v6 cross_rank（34m57s）+ v7 cross_z（33m34s）

## 二、结果（QA 独立复现）

| 缓存 | A-S2 OOS Sharpe | A-S2 OOS 收益 | 组合 OOS | exp_ret 截面 IC(OOS) |
|---|---:|---:|---:|---:|
| v4（absolute 基线） | -0.024 | -1.99% | 1.384 | -0.063 |
| v6（cross_rank） | -0.389 | -8.15% | 1.381 | **+0.035** |
| v7（cross_z） | **+0.004*** | **-1.63%*** | **1.399** | -0.041 |

*⚠️ QA 修正：v7 的 +0.004 是算术日均微正（+0.0002%/日），**复利口径 OOS 仍亏损 -1.63%**——"转正"是度量假象，实为"亏得少一点"。

## 三、QA 关键发现

1. **v6 恶化根因（成立）**：跨组尺度错配——单品种组 m0 走绝对标签（exp_ret≈0.008）vs cross_rank 组（3.1-3.8）→ 按日跨 18 品种统一 rank 时 **m0 入选 0%、2 品种组占做多 79.9%**
2. **v7 采纳理由须修正**：OOS 分段 seg1 改善（-0.518→-0.108）但 seg2 恶化（+0.628→+0.244）；全样本 Sharpe 0.682→0.418、MaxDD -19.7%→**-37.7%**——证据强度低
3. **IC 改善 ≠ 盈利（正确）**：v6 IC 升但 P&L 恶化；v7 IC 仍负但 Sharpe 微正
4. **标签是活性成分**（补做 v5 隔离）：同基差特征基线上 cross_z 拉到 +0.004 → 改善确来自标签截面化
5. **测试缺口**：P8-3 无专用单测（QA 用独立零泄漏脚本补齐验证）

## 四、主理人终裁

1. **v4 维持生产**（组合 OOS 1.384）；v7 作候选、不进生产（证据强度低 + 全样本显著恶化）
2. **P8 根因诊断验证成立**：标签截面化方向正确（cross_z 确实改善引擎 A），但**跨组可比性是关键工程问题**——需解决"单品种组/多品种组 exp_ret 尺度统一"才能发挥截面化价值
3. **后续方向优先级**（QA 建议）：① **全 18 品种统一截面**（而非组内截面）根治组规模偏置 ② m0 单品种组处理 ③ 截面中性/多空结构 ④ 单品种组引入组外参照
4. **方法论沉淀**：评估引擎 A 用**复利口径**（非算术日均 Sharpe）；"OOS 转正"须核实复利收益

## 五、文件清单

| 文件 | 说明 |
|---|---|
| `scripts/refine_lightgbm_champion.py` | +label_mode（截面化三变体，默认 absolute） |
| `scripts/group_modeling*.py` | +--label-mode 透传 |
| `scripts/p8_3_label_cross_section.py` | 重估编排（覆盖率/引擎A/组合/IC） |
| `scripts/qa_p83_independent_verify.py` / `qa_p83_zero_leakage_check.py` | QA 复核脚本 |
| `artifacts/signals_cache18_grouped_v6.parquet` / `_v7.parquet` | 实验缓存（不采纳/候选） |
| `artifacts/p8_3_engineA_compare_v6.csv` / `_v7.csv` / `p8_3_cs_ic_v6.csv` / `_v7.csv` | 实验数据 |
| `deliverables/software-hexfutures-ai/p8-3-label-cross-section-QA-review-2026-08-19.md` | QA 复核报告 |
| `docs/developer-guide.md` | 更新至 v3.10（§9.15） |
