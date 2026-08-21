# Sentinel-2 P20：增量数据更新 + Fresh-OOS 复核 + 生产首次运行（2026-08-21）

> 阶段：P20 ｜ 流程：工程师（寇豆码）→ QA（严过关）→ 主理人终裁 ｜ 状态：✅ 主流程 VERIFIED + P20-4 进行中

## TL;DR

**数据自 08-17 后首次刷新（08-18~08-20 增量）**，Sentinel-2 进入实盘运行阶段：① 18 品种 K 线全部更新到 08-20（RAW_SCALE_FIX 换算逐位正确）；② **S4 fresh-OOS 复核：形式触发不升级**（信号缓存未重建，指标逐位同基线）；③ **生产流水线首次运行**：08-20 空计划（引擎 A fold 截断 + 基差未更新）+ 模拟盘 A30/B70 新配置回放（期末 1,150,948 / +15.09% / Sharpe 0.763）。

## 关键结果（QA 独立复现）

| 项 | 结果 |
|---|---|
| 增量数据 | 18 品种 K 线全到 08-20（54 行，RAW_SCALE_FIX：au0×1.2596/ag0×1.4505/m0×0.3168 逐位正确）；边界无伪跳变（max sc0 +4.13%） |
| S4 fresh-OOS | fresh_kline=YES 但指标逐位同基线（v2 -0.3131/rt30 -0.0257/池化 0.6037）→ **不升级**（新 K 线未进信号缓存窗口） |
| p16 08-20 | **空计划**（引擎 A cache_end fold 截断 + **基差面板止 08-17 → 引擎 B 无 br_rank**） |
| p17 回放 | **期末 1,150,947.65（+15.09%）/Sharpe 0.763**/MaxDD -9.50%（A30/B70+nf_b0.30 首个完整回放，取代旧 A10/B90 状态） |

## QA 关键裁决（D1-D4 建议登记）

- **D1（重要）**：08-20 空计划是**数据不完整产物**非真实无信号——基差止 08-17 而 p16 STALE_DAYS=5 容差掩盖（完整性 PASS 与引擎无信号并存）→ **P20-4 修复 + 基差补齐**
- **D2**：S4 monitor fresh 判定易误读（fresh_kline 单独为真即 UPGRADE_TRIGGER）——升级 gate 应基于 fresh_signal（缓存重建）
- **D3**：p17 pending 计数 1→0 翻转（OOS_END 硬编码已知限制，如实标注）
- **D4**：空计划日 JSON 无原因字段——P20-4 加显式标注；spot_check WARN 系配置变更预期差异（A10/B90→A30/B70）

## 主理人终裁

1. **P20 主流程验收通过**（QA VERIFIED，数字全部独立复现）
2. **P20-4 立项执行中**：基差增量补齐（18 品种到 08-20）+ D1 完整性检查修复（basis_latest < target_date 即 WARN）+ 空计划日语义标注
3. **S4 登记后续**：信号缓存重建（v8 口径 + 08-18~08-20 新数据）后复测，或积累更多 fresh-OOS 日
4. **生产运行状态**：p16+p17 流水线实盘闭环就绪（等基差补齐后 08-20 可出真实计划）

## 文件清单

- 📄 交付报告（工程师版）：`sentinel2-p20-incremental-fresh-oos-2026-08-21.md` + QA 复核（QA review 文档）
- 🐍 复用：`p6_4_fill_gaps.py` / `p12_s4_shadow_monitor.py` / `p16_daily_signal.py` / `p17_shadow_account.py`
- 📊 数据：`data/raw/processed/*/1d/2026.parquet`（150 行）+ `artifacts/p20_*.log` + `trade_plans/2026-08-20` + `shadow_account/`（更新）
- 📄 开发者指南：更新至 **v3.24**（§9.29）
