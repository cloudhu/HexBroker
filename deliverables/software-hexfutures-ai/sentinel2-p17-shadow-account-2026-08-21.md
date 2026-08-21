# Sentinel-2 P17：内部簿记模拟盘 + 上游 broker.py multiplier bug（2026-08-21）

> 阶段：P17 ｜ 流程：工程师（寇豆码）→ QA（严过关）→ 主理人终裁 ｜ 状态：✅ 模拟盘 VERIFIED + ⚠️ 上游 bug CONFIRMED（待修复）

> **⚠️ 修复后重估标注（P18-2，2026-08-21）**
> P18-P0 已修复 `broker.py` 的 SimBroker 全局 multiplier=10 记账 bug（QA VERIFIED，
> 全量 pytest 219 过）。本报告正文中 `consistency_bt_equity.csv` / §4 的 **bt 列
> （期末 1,905,743 / 总收益 90.62% / Sharpe 1.797）为修复前 BacktestEngine 口径**
> （×10 记账，不代表真实口径）。修复后真实口径：shadow account 主路径 1,090,232
> （0.572）、修复后 BT 纯 B OOS 1,094,051（0.568）。P17 模拟盘簿记本身用品种级
> multiplier，不受 broker 修复影响，结论保持有效；完整修复前后对比见
> `artifacts/p18_broker_fix_verify.csv` 与
> `deliverables/software-hexfutures-ai/sentinel2-p18-reestimation-2026-08-21.md`。

## TL;DR

**内部簿记模拟盘落地并 VERIFIED**（外部模拟盘 API 均不支持期货）——ShadowAccount 准确记账（品种级 multiplier），OOS 回放 502 日全量。**同时发现上游重大缺陷**：`broker.py` 平仓盈亏用**全局 multiplier=10** 而非品种级乘数（fee 却用品种级）→ **历史回测绩效指标（Sharpe~1.6/+90%）全部偏高失真**，需修复后重估。

## 模拟盘回放（QA 独立复现，block + T+1 开盘）

| 指标 | 值 |
|---|---|
| 期末权益 | 1,090,232（+9.02%） |
| 年化 / MaxDD / Sharpe | +4.44% / -8.42% / 0.572 |
| 成交 / 执行 / 阻断 | 884 笔 / 468 / 33（黑色系 FAIL） |
| 引擎 A 手数 | **全 0 手**（生产 100% 纯 B，P16 事实复核） |

## ⚠️ 上游 broker.py multiplier bug（QA 三重独立证据）

| 证据 | 结果 |
|---|---|
| 静态审查 | L75/L92 用 `self.cost.multiplier`（全局 10）；`CostModel.fee()` 用品种级 → **同一账户 fee 品种级、PnL 全局级口径不一致**；品种级 multiplier 从未传入 broker |
| 最小复现 | ni0（×1）PnL 放大 10 倍（+99.89→+799.89）；jm0（×60）压缩至 0.16 倍 |
| 回测 monkeypatch | buggy **Sharpe 1.797/+90.62%** vs fixed **0.568/+9.42%**（Sharpe -68.4%、权益 -811,693） |

**影响范围**：
- ❌ 受影响：所有 BacktestEngine 绩效指标（引擎 B 1.622、组合 1.614/1.664、P16"+90%"等，P8-P16 ~20 份报告）——注意非均匀（×1 品种放大、×60 缩小）
- ✅ 不受影响：计划生成/手数/名义（CONTRACTS18 品种级）、手续费、信号/模型/校准/特征、P17 shadow account 记账

## 主理人终裁

1. **P17 模拟盘验收通过**（记账品种级正确、T+1 约定文档化、FAIL 三模式、一致性四层归因）
2. **broker.py bug 确认为上游历史缺陷**（非 P17 引入）——**立即修复（P18-P0）**：最小方案 = L75/L92 `self.cost.multiplier` → `self.cost._multiplier(symbol)` + 补回归测试（ni0×1/jm0×60 品种级记账）
3. **历史回测指标重估（P18）**：修复后按优先级重跑——P16 组合 OOS > P11 引擎 B > P9 组合网格 > P5 引擎基线；P13/P14/P15 引用的 OOS 绩效数字同步更新（分类/校准类结论不受影响）
4. **修复前**：所有 BacktestEngine 绩效数字视为偏高失真，不得直接对外宣称
5. 生产路径就绪（外部模拟盘不支持期货结论复核成立；未来 CTP 仿真接入点=替换 execute_plan）

## 文件清单

- 📄 本报告 + QA 复核：`sentinel2-p17-shadow-account-2026-08-21.md` + `p17-QA-review-2026-08-21.md`
- 🐍 脚本：`scripts/p17_shadow_account.py`
- 📊 数据：`artifacts/shadow_account/`（equity_curve/positions_history/plan_executions/consistency_compare/plans_cache 502 份）
- 📄 开发者指南：更新至 **v3.20**（§9.25）
