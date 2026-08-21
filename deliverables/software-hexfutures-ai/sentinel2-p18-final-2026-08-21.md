# Sentinel-2 P18 终裁：broker 修复 + 历史回测重估（2026-08-21）

> 阶段：P18 全流程 ｜ 流程：工程师 → QA Round1（P17 bug 确认）→ 修复 → QA Round2 VERIFIED → 重估 → QA Round3 VERIFIED + 归因 DISCREPANCY → 工程师更正 → 主理人终裁 ｜ 状态：✅ 闭环

## TL;DR

**上游缺陷修复 + 全量重估闭环**：`broker.py` multiplier 修复（git `d3ec0a6`）后，历史回测绩效全部修正为真实口径（**Sharpe 系统性 -68% 量级**）；重估数字经 QA 三轮独立复核逐位复现；归因更正确认**引擎 A "转正"主因是 P8-4 缓存升级（v2→v8）**而非 broker 修复（隔离矩阵实证）。

## 修复后真实口径（QA Round3 VERIFIED，逐位复现）

| 报告 | 指标 | 修复前（失真） | 修复后（真实） | 归因 |
|---|---|---|---|---|
| P16 组合 A10/B90 | OOS Sharpe | 1.612 | **0.566** | broker-only 干净 |
| P16 组合 volY | OOS Sharpe | 1.662 | 0.567 | broker-only |
| P11 引擎 B win252/thr0.70 | OOS Sharpe | 1.622 | 0.490 | broker-only |
| P11 最优 | OOS Sharpe | win63/thr0.70 1.778 | **win126/thr0.60 0.641** | thr 0.60 普遍更优 |
| P9 网格最优 | OOS Sharpe | A10/B90 1.612 | **A0.35/B0.65 0.731** | 引擎 A 转正后 A 更高权重更优 |
| P5 引擎 A S2 | OOS Sharpe | -0.337（v2+buggy） | **+0.990**（v8+fixed） | ⚠️ 主因 v2→v8 缓存升级 |

## 引擎 A 转正归因（QA Round3 隔离矩阵，关键修正）

| 缓存 | buggy | fixed | 解读 |
|---|---|---|---|
| v2 | -0.1546 | -0.4643 | broker 修复在 v2 上反而更差 |
| v8 | +0.6255 | **+0.9904** | v8+buggy 已为正；broker 修复边际 +0.36 |

**结论**：符号翻转主因 = **P8-4 缓存升级（v2→v8，label_pool=all cross_z）**，broker 修复在 v8 口径上边际提升（+0.36）非转正因素。历史 -0.337 为 v2 缓存时代口径。

## 主理人终裁（生产决策更新）

1. **P18 全流程验收通过**（修复 VERIFIED + 重估 VERIFIED + 归因更正）
2. **修复前数字作废**：引擎 B 1.622、组合 1.614/1.664 等全部旧绩效数字不得对外宣称；以 `sentinel2-p18-reestimation-2026-08-21.md` 为准
3. **生产决策更新（P19 立项候选）**：
   - **引擎 B thr 0.70→0.60 立项**（3/4 档 win 0.60 最优；需成本/容量复核）
   - **引擎 A 名义上调立项**（v8+fixed +0.99 / cap0.5 +1.05——值得恢复可交易评估；需保证金/容量确认）
   - **A0.35/B0.65 待引擎 A 恢复可交易后以 cap0.5 复验**（P9 网格用无 cap，生产 cap0.5 未验证）
4. **P15 结论不受影响**：信号层（IC/校准）结论与 broker 无关仍成立
5. 方法论沉淀：**绩效对比必须控制缓存版本**（v2 vs v8 隔离矩阵）；报告数字标注"修复前/修复后"口径

## 文件清单

- 📄 重估报告（归因更正版）：`sentinel2-p18-reestimation-2026-08-21.md` + QA Round3 复核
- 🐍 修复：`broker.py`（2 行）+ `tests/test_broker_multiplier_pnl.py`（4 例）
- 🐍 重估：`scripts/p18_reestimate.py`（含隔离矩阵）+ `p18_verify_broker_fix.py`
- 📊 数据：`p18_reestimation.csv`（含 P5-ISO 隔离矩阵）+ `p18_engineB_grid_postfix.csv` + `p18_combo_grid_postfix.csv` + `p18_broker_fix_verify.csv`
- 📄 开发者指南：更新至 **v3.22**（§9.27）
