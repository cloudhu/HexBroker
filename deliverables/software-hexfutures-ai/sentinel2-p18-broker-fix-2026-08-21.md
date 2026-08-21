# Sentinel-2 P18：broker.py multiplier 修复 + 历史回测重估（2026-08-21）

> 阶段：P18 ｜ 流程：工程师（寇豆码）→ QA（严过关）Round1 确认 → 工程师修复 → QA Round2 VERIFIED → 重估中 ｜ 状态：✅ 修复完成（QA VERIFIED）+ 重估进行中

## TL;DR

**上游缺陷修复完成**（P17 发现、QA 三重证据确认）：`broker.py` 平仓/浮盈盈亏改用**品种级 multiplier**（原全局 ×10）——修复后纯 B OOS 期末 **1,094,051 / Sharpe 0.568**（vs 修复前 1,905,743 / 1.797，Sharpe -68.4%），与 shadow account 准确记账相关 0.9998。**历史回测绩效指标全部修正为真实口径**，重估进行中。

## 修复内容（QA VERIFIED，最小 2 行）

| 项 | 内容 |
|---|---|
| broker.py L75/L92 | `self.cost.multiplier`（全局 10）→ `self.cost._multiplier(symbol/sym)`（品种级） |
| 向后兼容 | 无 contracts 路径回退全局 ×10（fast_cfg/旧调用行为不变） |
| 回归测试 | `tests/test_broker_multiplier_pnl.py` 4 例（ni0 ×1=99.895 / jm0 ×60=5993.7 / unrealized / 回退） |
| 全量回归 | **pytest 219 passed**（QA 独立复跑 EXIT=0） |
| QA 交叉验证 | 15/15 checks（含加仓-部分平仓、空头路径）；修复后纯 B OOS 逐位复现（1,094,050.66/0.5683） |

## 修复后关键指标（纯 B OOS，QA 逐位复现）

| 指标 | 修复前（失真） | 修复后（真实） |
|---|---:|---:|
| 期末权益 | 1,905,743 | **1,094,051** |
| 总收益 | +90.62% | **+9.42%** |
| 年化 | +38.33% | **+4.63%** |
| MaxDD | -14.05% | **-8.93%** |
| Sharpe | 1.797 | **0.568** |

## 主理人终裁

1. **P18 修复验收通过**（QA Round2 VERIFIED，git 已提交 `d3ec0a6`）
2. **历史回测重估执行中**（P18-2，QA 同意优先级）：P16 组合 OOS > P11 引擎 B > P9 组合网格 > P5 引擎基线——修复后 Sharpe 大幅下调是真实口径，**生产决策（引擎 B 为主、A10/B90）是否改变需重估后明确**
3. **修复前数字作废**：所有引用旧 BacktestEngine 绩效的报告数字（引擎 B 1.622、组合 1.614/1.664 等）待重估更新；修复前不得对外宣称
4. **P17 artifact 标注**：shadow_account 遗留基线加"修复前"标注（QA 建议，工程师执行中）

## 文件清单

- 🐍 修复：`hexbroker/backtest/broker.py`（2 行）+ `tests/test_broker_multiplier_pnl.py`（4 例）
- 🧪 验证：`scripts/p18_verify_broker_fix.py` + `artifacts/p18_broker_fix_verify.csv` + QA 脚本 ×3
- 📄 开发者指南：更新至 **v3.21**（§9.26）
- ⏳ 重估中：`artifacts/p18_reestimation.csv`（修复前后各报告指标对比）
