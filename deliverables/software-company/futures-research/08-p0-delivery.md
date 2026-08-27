# P0 四项优化立项 · 交付裁决报告

**主理人**：齐活林（Qi）· 交付总监 | **日期**：2026-08-26 | **工作流**：🏗️ 标准 SOP（PM→架构师→工程师→QA）

---

## TL;DR

P0 四项优化（撮合可信度加固 / 前视自检 / 四层版本化 / Bootstrap 绩效区间）经标准 SOP 四阶段全流程交付完成：**QA fresh-eyes 独立复核 PASS（路由判定 NoOne 无需返工），全量测试 549 collected / 538 passed / 0 failed / 11 skipped（均为可选依赖），红线 6/6 通过，默认配置行为零变化（严于 <1e-12 红线）。**

---

## 一、交付概览

| 阶段 | 成员 | 状态 | 产出 |
|---|---|---|---|
| 增量 PRD | 产品经理（许清楚） | ✅ | `04-p0-prd.md`（目标/用户故事/需求池/验收标准 A1-A4） |
| 增量设计+任务分解 | 架构师（高见远） | ✅ | `05-p0-arch.md`（实现方案/接口/任务 T01-T05/Q1-Q5 裁决建议） |
| 代码实现 | 工程师（寇豆码） | ✅ | T01-T05 全实现，IS_PASS: YES，`06-p0-impl.md` |
| 测试验证 | QA 工程师（严过关） | ✅ | fresh-eyes 独立复核 PASS，`07-p0-qa.md` |

**主理人裁决（Q1-Q5）**：全部采纳架构师建议——Q1 下一 bar 开盘价±滑点 / Q2 双口径默认输出 / Q3 sidecar JSON / Q4 partial 部分成交（默认 None，启用 5%）/ Q5 block=20, n_boot=1000。

---

## 二、交付内容（P0 × 4）

| P0 | 交付物 | 关键验证 |
|---|---|---|
| **P0-1 撮合可信度** | `backtest/execution.py`（ExecutionConfig/next_bar_ref_price/cap_order_qty/run_dual_caliber）+ engine/walkforward 接入 + `docs/matching-assumptions.md`（16 条 MA-01~16） | next_bar 成交价精确匹配；volume_cap partial/reject 正确；双口径 Δ% 量化合理 |
| **P0-2 前视/递归自检** | `tools/` 包（dag/lookahead_analysis/recursive_analysis/exemptions）+ CI 接入 | 注入缺陷 6 类 pattern 100% 检出；CI 范围 EXIT=0（0 未豁免/6 豁免）；只读不污染 |
| **P0-3 四层版本化** | `data/manifest.py`（DataManifest+回填）+ `utils/fingerprint.py`（FourLayerFingerprint）+ SignalStore sidecar + `scripts/backfill_manifests.py` | manifest 回填指纹一致；四层指纹可复现+敏感性正确；Parquet schema 零改动 |
| **P0-4 Bootstrap 区间** | `evaluation/bootstrap.py`（block_bootstrap/bootstrap_metrics_ci）+ pipeline 报告三块（dual_caliber/fingerprints/bootstrap）+ `--no-dual-caliber` | MC 覆盖率真实 100 次模拟 ∈[0.90,1.00]；固定 seed 可复现；旧报告 key 全保留 |

**测试增量**：476（基线）→ 549 collected（新增 9 个测试文件 73 项）。

---

## 三、红线审计（QA 独立验证，6/6 通过）

- ✅ 不修改清单零触碰：broker.py / cost.py / forecast/base.py / rl/futures_env.py / evaluation/stats.py / evaluation/metrics.py 均 `git diff` 为空
- ✅ pyproject.toml 依赖零新增（dependencies 段 15 项不变）
- ✅ SignalStore 只落 OOS 物理隔离 + 双闸门判定逻辑零改动
- ✅ **默认零变化（最强证据）**：QA 直接 `git show HEAD:engine.py` 加载改动前真实代码对照，3 场景（多品种/涨跌停列/真实涨停触发）权益曲线与六项指标差异 **0.000e+00**（严于 <1e-12）

---

## 四、遗留问题与裁决

| # | 严重度 | 问题 | 主理人裁决 |
|---|---|---|---|
| L-01 | **P2** | `recursive_analysis` 全目录运行对含合法递归函数（如工具自身 `dfs→dfs`）误报自环、退出码 1；**CI 门禁范围实测 EXIT=0 不受影响** | **列入遗留清单，不阻塞交付**。建议下次迭代：给工具自身递归加豁免（特判忽略递归自调用），或缩小 CI 检查范围并文档化 |

---

## 五、文件清单

**交付文档**（`deliverables/software-company/futures-research/`）：
| 文件 | 说明 |
|---|---|
| `01-pm-market-research.md` | 开源调研（14 项目对比矩阵 + 8 条最佳实践） |
| `02-arch-optimization.md` | 架构对比 + P0/P1/P2 优化清单 |
| `03-lead-verdict.md` | 调研阶段主理人裁决 |
| `04-p0-prd.md` | P0 增量 PRD |
| `05-p0-arch.md` | P0 增量设计 + 任务分解 |
| `06-p0-impl.md` | 工程师实现说明（含偏差 D-01~06） |
| `07-p0-qa.md` | QA fresh-eyes 独立复核报告 |
| `08-p0-delivery.md` | 本报告（交付裁决） |

**代码**（13 修改 + 20 新增，git status 已确认）：
- 修改：`config.py` `configs/base.yaml` `constants.py` `backtest/engine.py` `backtest/walkforward.py` `data/store.py` `forecast/signal_store.py` `forecast/trainer.py` `utils/fingerprint.py` `evaluation/__init__.py` `pipeline.py` `tests/_helpers.py` `.github/workflows/ci.yml`
- 新增：`backtest/execution.py` `data/manifest.py` `evaluation/bootstrap.py` `tools/{__init__,dag,lookahead_analysis,recursive_analysis,exemptions}.py` `scripts/backfill_manifests.py` `docs/matching-assumptions.md` + 9 个测试文件

---

## 六、下一步建议

1. **提交入库**：建议按项目惯例拆分 feat+docs 双提交（代码 / 交付文档），commit 后跑 `git fsck --no-dangling` 验证
2. **P0-2 全目录运行**：将 lookahead/recursive CLI 纳入常规 CI（当前已接入 pytest 后步骤）；修复 L-01 豁免逻辑
3. **P1 排期**：按 `03-lead-verdict.md` 的 P1 清单（CTP/SimNow Gateway、因子库 DSL、ML/RL 滚动重训、风控 RiskRule 接口化、中国市场规则化、CI lint）立项
4. **双口径对照使用**：新策略上线前跑 `--no-dual-caliber` 关闭的默认口径对比，将 next_bar 口径作为可信度下限参考

**状态**：✅ P0 四项交付闭环（调研→PRD→设计→实现→QA 独立复核全通过）｜ 遗留 P2×1（L-01，不阻塞）
