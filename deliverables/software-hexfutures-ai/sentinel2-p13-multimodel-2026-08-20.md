# Sentinel-2 P13：引擎 A 多模型融合（2026-08-20）

> 阶段：P13 ｜ 流程：工程师（寇豆码）→ QA（严过关）Round1 发现口径缺陷 → 工程师修正 → QA Round2 VERIFIED → 主理人终裁 ｜ 状态：✅ 完成（融合不采纳，维持 v8）

## TL;DR

**引擎 A 多模型融合（LightGBM + XGBoost + HistGradientBoosting）结论：不采纳，维持 v8 生产**。QA Round 1 揭示关键口径缺陷（Stage B 未加载 base.yaml → 评估在无 cap 口径），工程师修正后按**真实生产口径（group_cap=0.5 + ferrous_all）**重估：**F2 反转劣于 v8**（0.597 vs 0.646）——融合改善 OOS 截面 IC（-0.064→-0.039~-0.061）但未转化为引擎收益，瓶颈在黑色系敞口/截面选择机制而非模型多样性。

## 关键结果（QA Round 2 VERIFIED，逐位一致）

| 方案 | 单引擎 OOS Sharpe | OOS 复利 | 组合 A10/B90 OOS | OOS 截面 IC | 全样本 MaxDD |
|---|---:|---:|---:|---:|---:|
| **v8（生产基线）** | **0.646** | **+9.01%** | **1.614** | -0.0640 | -30.3% |
| xgb 单 | 0.526 | +9.26% | 1.595 | -0.0140 | — |
| hgb 单 | 0.494 | +5.69% | 1.605 | -0.0580 | — |
| F1（3 等权） | 0.562 | +7.19% | 1.610 | -0.0470 | — |
| F2（IC 加权） | 0.597 | +7.87% | 1.612 | -0.0606 | -19.0% |
| F3（LGB+XGB） | 0.486 | +6.20% | 1.603 | -0.0385 | — |

**规则**（三项同改善才采纳）：cap 口径下 F1/F2/F3 截面 IC 全改善（+0.017/+0.003/+0.025）但单引擎+组合 Sharpe 全未超基线 → **无方案满足 → is_pass=false**。

## 关键发现链（QA 价值体现）

1. **Round 1 DISCREPANCY-1**：工程师声称"生产 config 含 group_cap=0.5"但 Stage B 用无 path `load_config()` → base.yaml 未加载 → 评估实际在无 cap 口径（v8 0.626 vs F2 0.679 的"改善"是口径伪影）
2. **修正后真实生产口径**：v8 0.646 vs F2 0.597（**反转**）——F2 增量收益大部分来自黑色系敞口（IS 权重倾斜：i0 w_xgb=0.674、jm0=0.844、j0 w_hgb=0.815），恰被生产 cap 约束
3. **机制确认**：OOS 段 ferrous_all 持仓占比 cap 下 F2 30.1%→26.4%（-3.7pp）降幅大于 v8（-2.4pp）；xgb 单引擎 cap 口径 0.261→0.526 大幅改善——佐证无 cap 结果受黑色系敞口驱动
4. **诚实结论**：模型多样性对截面排序有效（IC 改善）但非收益瓶颈；引擎 A 收益瓶颈在黑色系敞口/截面选择机制，且被 cap 约束——**标签/任务错配更深层修复为后续方向**

## 主理人终裁

1. **F1/F2/F3 均不采纳**，**维持 v8 生产**（cap 口径决定性失败，QA Round 2 VERIFIED）
2. **F2 登记入 S4 影子跟踪扩展**（候选池 +1：S4 rt30 / F2 融合，均在 fresh-OOS 数据刷新后复核）
3. 工程资产保留：`p13_models.py`（XGB/HGB 模型类，可复用）+ `p13_multimodel.py`（checkpoint/resume/守卫，可复用于未来融合实验）+ v9_xgb/hgb/f1/f2/f3 缓存
4. 登记遗留：make_verdict 注释清理（旧无 cap 数字，代码实际取表值——低优先级）；QA Round 1 脚本 [G] 期望已随 cap 口径修正

## 方法论沉淀

- **部署接线标准（§9.18）再次验证价值**：评估脚本必须显式 `load_config("configs/base.yaml")` + 显式传参，否则 group_cap/group_map 静默不生效——本轮"无 cap 口径伪影"正是此坑的实战
- 评估规则明确：**生产口径（cap）为准**，无 cap 对照仅作 sensitivity 不作裁决依据

## 文件清单

- 📄 本报告 + QA Round1/2：`p13-multimodel-fusion-QA-review-2026-08-20.md` + `p13-multimodel-fusion-QA-review-round2-2026-08-20.md` + `qa_p13_independent_verify.py` + `qa_p13_round2_verify.py`
- 🐍 脚本：`scripts/p13_models.py` / `p13_multimodel.py`（cap 口径修正版）
- 💾 数据：`signals_cache18_grouped_v9_xgb/hgb/f1/f2/f3.parquet` + `p13_multimodel_compare.csv`（cap 口径）+ `p13_model_ic.csv` + `p13_verdict.json`（is_pass=false）
- 📄 开发者指南：更新至 **v3.16**（§9.21）
