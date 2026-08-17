# SENTINEL Phase 1+3 验证报告（2026-08-17）

> **背景**：按 SENTINEL 四层架构（L1 强度排序 / L2 状态门控 / L3 RL 决策 / L4 自回归进化）推进。本报告记录 Phase 1（L1 排序学习器）与 Phase 3（L3 RL 决策层）验证结果。
> **目标**：验证架构核心假设——① 排序目标 vs 回归目标；② RL 决策层能否在 OOS 段产生正收益。

---

## 一、Phase 1：L1 强度排序器（RankNet）验证

**自研纯 numpy RankNet**（2×32 tanh MLP，pairwise 排序损失，无任何开源模型）：
- 合成数据正确性：测试集排序相关 0.97 ✅
- 真实数据（跨品种合并训练，嵌套口径）：

| 指标 | LightGBM exp_ret（回归目标） | RankNet 分数（排序目标） |
|---|---|---|
| Q4-Q0 价差/5日 | **0.700%** | 0.320% |
| RankIC | +0.0123 | **+0.0296** |

**裁决**：排序目标在极端分位价差未胜出（回归更强），但全局排序质量（RankIC）更优。→ **L1 信号源采用 LightGBM exp_ret（0.700%）**；RankNet 降级为 L4 进化候选。**排序目标路线未成立，按架构失败判定逻辑回退**。

## 二、Phase 3：L3 RL 决策层验证（SENTINEL 核心创新）

**SentinelTradingEnv**（自研，编码探索经验）：
- 三品种组合决策、**离散多头仓位档位 {0, 0.5, 1.0}³ = 27 动作**（无做空——看空反指经验）；
- 状态 = exp_ret 强度(3) + 趋势 MA20(3) + 波动率(3) + 持仓(3) = 12 维；
- 奖励 = 实现收益 − 换手惩罚（w_pnl=1/w_turn=0.1）；
- 训练：自研纯 numpy PPO（30k steps），train 段 2019-04~2021-12。

**OOS 评估（2022-01~2024-06，未见过数据）**：

| 指标 | SENTINEL RL（OOS） | 规则基线 top-30%+trend（全样本） |
|---|---|---|
| 年化 | **+13.80%** | +10.2% |
| 最大回撤 | -18.92% | -8.8% |
| Sharpe | 0.83 | 1.21 |

**裁决**：✅ **RL 决策层可训练且 OOS 正收益**（年化 13.8%，2.5 年未见数据）——架构第三层成立。但 Sharpe/回撤逊于规则基线（比较口径不同：RL 仅 OOS 2.5 年 vs 基线全样本 6.5 年），奖励设计待优化（ep_rew_mean=-12 显示换手惩罚主导，w_pnl/w_turn 需重平衡）。

## 三、SENTINEL 进展总结

| Phase | 状态 | 结论 |
|---|---|---|
| L1 排序学习器 | ✅ 完成验证 | 排序目标未胜出 → 信号源用 LightGBM exp_ret |
| L2 状态门控 | 🔄 并入 | 趋势/波动已作为 RL 状态特征（Phase 3 内嵌） |
| **L3 RL 决策层** | ✅ **最小验证通过** | **OOS 年化 +13.8%**（正收益） |
| L4 自回归进化 | ⏳ 设计就绪 | ExAMM 引擎进化 L1 超参/L3 奖励权重（适应度=嵌套回测 Sharpe） |

## 四、主理人裁决建议

1. **Phase 3 成功，继续 Phase 4**：用 ExAMM 进化引擎优化 L3 奖励权重（w_pnl/w_turn/w_dd）+ L1 特征子集，适应度 = 嵌套口径完整回测 Sharpe；
2. **P1 奖励工程**：放大 pnl 项（w_pnl ×100 尺度）、换手惩罚降低、加趋势状态惩罚（编码 trend 过滤经验进奖励）；
3. **P1 公平对比**：规则基线在 OOS 段（2022-2024）同口径复跑，与 RL 直接对比；
4. **P2**：RL 训练加长（100k+ steps）+ seed 稳定性检查。

## 五、交付物

- `hexbroker/forecast/intensity.py` — 自研 RankNet 强度排序器
- `hexbroker/rl/sentinel_env.py` — SENTINEL RL 环境（组合级单边多头）
- `scripts/compare_ranknet_vs_lgbm.py` — Phase 1 对比脚本
- `scripts/sentinel_phase3_rl.py` — Phase 3 训练/评估脚本
- 架构文档：`sentinel-architecture-2026-08-17.md`
