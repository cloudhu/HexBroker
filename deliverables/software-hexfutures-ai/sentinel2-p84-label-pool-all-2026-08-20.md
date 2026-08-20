# P8-4 全品种统一截面重构报告（v8 采纳）

**日期**：2026-08-20 ｜ **流程**：工程师实现 → QA fresh-eyes 复核（VERIFIED，有条件采纳）→ 主理人终裁

## TL;DR

| 项目 | 结论 |
|---|---|
| 实现 | `label_pool="all"`：全 18 品种统一截面（根治 P8-3 跨组尺度错配），默认 group 不变 |
| **引擎 A OOS 复利** | v4 **-1.99%** → v8 **+8.68%**（首次转正，QA 逐位复现、双段均正） |
| 组合 A15/B85 OOS | 1.384 → **1.602** |
| m0 入选率 | 恢复至 27.3%（接近 top30% 公平份额；组规模偏置根治） |
| **QA 裁决** | **有条件采纳 v8 为生产信号缓存**（零泄漏 8/8、数字真实，附 3 项风控条件） |
| **主理人终裁** | **采纳 v8**（更新 EngineAConfig.signal_cache → v8），附风控监测条件 |

---

## 一、实现（label_pool，向后兼容）

- `label_pool="all"`：所有品种 fwd 合并大面板 → union 对齐 → **当日截面化**（cross_z）→ 按品种取回；单品种组 m0 自动参与全品种截面（不再退化绝对标签）
- 无前视不变（当日行内统计，QA 扰动实证 max|Δ|=0）
- 修复首轮 bug：union 空洞污染 fwd 标签 → 2208 条 → 修复后 8624 条（与 v4/v7 覆盖率一致，加回归单测）
- 默认 group 逐字节兼容 P8-3；196/197 测试通过

## 二、结果（QA 独立复现，复利口径）

| 指标 | v4 (absolute) | v7 (cross_z 组内) | **v8 (cross_z 全品种)** | v9 (cross_rank 全品种) |
|---|---:|---:|---:|---:|
| 引擎 A OOS 复利 | -1.99% | -1.63% | **+8.68%** ✅ | +8.81% |
| 引擎 A OOS Sharpe | -0.024 | +0.004 | **+0.626** | +0.518 |
| 全样本 Sharpe / MaxDD | 0.682 / -19.7% | 0.418 / -37.7% | **0.867 / -26.1%** | 0.755 / -18.7% |
| 组合 A15/B85 OOS | 1.384 | 1.399 | **1.602** | 1.570 |
| m0 选中天数占比 | 21.9% | 20.3% | **27.3%** | 30.5% |
| OOS 分段 | seg1 -7.53% / seg2 +5.98% | seg1 -3.19% / seg2 +1.58% | **seg1 +2.22% / seg2 +6.30%** | 双段正 |

## 三、QA 关键验证

1. **数字真实**：独立复算逐位一致；权益路径无单日尖峰（最大 +2.28%）；**OOS 双段均正（v8/v9 唯一）**；全样本也改善
2. **组规模偏置根治**：2 品种组 79.9% 病理消失（v8 最大组 22.73% ≈ 公平份额）
3. **零泄漏**：8/8 PASS（含全品种面板 union 对齐）

## 四、QA 风控条件（采纳前提）

1. ⚠️ **OOS 截面 IC 仍为负（-0.064）**：v8 盈利来自**做多选中组合的正漂移**（非排序技能），**主要靠黑色系敞口**（jm0/zn0/rb0/j0/hc0/i0 贡献 +0.7~1.8%）→ **若黑色系进入下行趋势，OOS 边际可能回吐**——需持续监测
2. ⚠️ **m0 恢复是公平性修复非 alpha**（m0 OOS realized -0.263%/5d 为负）——不要误读为 m0 信号变好
3. ⚠️ **双变体选择偏差**：v8/v9 两个变体都跑通，选择 v8 有轻微多重比较风险——已用全样本/OOS 双指标 + 分段稳定性缓解

## 五、主理人终裁

1. **采纳 v8 为生产信号缓存**：`EngineAConfig.signal_cache` → `artifacts/signals_cache18_grouped_v8.parquet`（配置更新，P6-5 基础上改一行）
2. **风控监测条件（随采纳生效）**：
   - 监测黑色系组敞口（ferrous_raw/ferrous_steel 合计选中占比，若 >50% 且黑色系转弱需预警）
   - 跟踪 OOS 截面 IC（当前 -0.064 为负，若持续恶化需复审）
   - 复利口径为唯一评估口径（方法论沉淀）
3. **记录**：v9（cross_rank+all）作对照保留；v4/v6/v7 保留回归基线
4. **生产配置更新**：EngineAConfig.signal_cache=v8 + 相关文档同步（v3.11）

## 六、文件清单

| 文件 | 说明 |
|---|---|
| `scripts/refine_lightgbm_champion.py` | +label_pool（group/all，默认 group） |
| `scripts/group_modeling_v2.py` | +--label-pool 透传 |
| `scripts/p8_4_label_pool_all.py` | 重估编排 |
| `artifacts/signals_cache18_grouped_v8.parquet` | **生产信号缓存（8,624 行）** |
| `artifacts/signals_cache18_grouped_v9.parquet` | 对照（保留） |
| `artifacts/p8_4_*.csv` / `p8_4_verdict.json` | 实验数据 |
| `deliverables/software-hexfutures-ai/p8-4-label-pool-all-QA-review-2026-08-20.md` | QA 复核报告 |
| `docs/developer-guide.md` | 更新至 v3.11（§9.16） |
