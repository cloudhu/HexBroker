# 交付总览 · 期货免费数据源多源故障切换（P0 批次）

**日期**：2026-08-29　|　**主理人**：齐活林（Qi）· 交付总监
**提交**：`1fcb940`（feat）→ `c709101`（docs）

---

## TL;DR

挖到了 08-28 停摆事故的**真根因**并修复：不是"取数失败"，而是**空 DataFrame 能通过全部数据契约校验**，
5 个数据源共用这条路径，于是"源停更 → 裁成 0 行 → 静默返回成功"被伪装成刷新成功。
同时复活了新浪源、首次跑通 AkShare 源，并发现 **akshare 与新浪是同一上游、不构成冗余**。

---

## 交付状态

| 项 | 结果 |
|---|---|
| 测试 | **713 passed**（原 677 + 新增 36） |
| 静态门禁 | 改动文件 ruff **全通过**（剩余 2 个为未触及文件的既有问题） |
| 真实联网 | 18/18 品种双源（sina / akshare）末日 **2026-08-28** |
| 仓库完整性 | `git fsck --no-dangling` **无输出**，对象库完整 |
| 提交纪律 | feat + docs 双提交 ✅ |

---

## 修复清单

| 编号 | 内容 | 状态 |
|---|---|---|
| **P0-0** | 咽喉门禁：空帧 + 陈旧数据必须显式失败 | ✅ |
| **P0-1** | 新浪源换新端点（旧端点冻结于 2024-07-17） | ✅ |
| **P0-3** | AkShare 源修复（中文列名 `KeyError`，从未跑通过） | ✅ |
| **P0-7** | 失败可区分：4 个新异常类型 | ✅ |
| **P0-8** | 真实联网冒烟脚本 | ✅ |
| **D3** | pytdx symbol 命名错位（`rb` vs `rb0`） | ✅ |
| 附带 | `testpaths` 白名单漏网（17 个测试永不执行） | ✅ |
| P0-4 / 5 / 6 | 备源 raw 拉取器 / 后复权续接器 / 换月检测告警 | ⬜ 下一批 |
| — | 故障切换编排器（三级降级） | ⬜ 下一批 |

---

## 关键结论

1. **咽喉缺陷**：`_clip_range` 裁空 + `validate_bars` 放行空帧，5 源共用 → 已在咽喉修复，
   而非逐个源打补丁。硬化为严格默认后 **677 测试仍全绿**，证明无代码依赖空帧通过（证据，非假设）。
2. **新浪没死**：项目用的是冻结在 2024-07-17 的旧端点。换新端点后 18/18 品种、末日当天，
   且新增持仓量与结算价字段。
3. **🔴 akshare == 新浪新端点**：`cu0` 2026-08-28 收盘价两边逐位相同（108900.0）。
   两者是同一上游的两层皮，**故障切换在它们之间没有意义**。真正独立的备源只能是交易所官方。
4. **Q3 已答**：pandadata 可回溯 2020+ 取 `dominant_id` → dominant 日历可本地快照化，
   解开"主源挂了却要问主源要日历"的死结。附带解答 Q6（结算价与收盘价同属 `close_pcr` 口径）。

---

## 团队机制降级声明

`software-product-manager` / `software-architect` / `software-engineer` **三个智能体全部报
`Task agent ... is not available`**（architect 为 0 秒启动即失败），判定为系统级注册故障。
已降级为**主理人直接实施 + 自验**。PM 在失效前落盘的 36 号调研报告为真实团队产出；
本批次代码与 37 号架构文档为**主理人产出，已明确标注**。

---

## 对 36 号报告的三处更正

| # | PM 结论 | 主理人复核 |
|---|---|---|
| 1 | "SinaSource 静默空返回" | 机制**不在** `_rows_to_frame`（那里有抛错）。真根因在 `_clip_range` + `validate_bars` 放行空帧 |
| 2 | 新浪是死源 | **不是**，是项目用了旧端点。新端点新鲜到当天 |
| 3 | SHFE/INE ✅200 | 根域 200 但数据文件 404；**CZCE 需走 `.txt`**（`.htm` 为 412），24 合约全得 |

---

## 文件清单

**新增**
- `hexbroker/data/freshness.py` —— 空结果 / 陈旧门禁模块
- `hexbroker/data/test_freshness.py` —— 17 测试
- `hexbroker/data/sources/test_akshare_source.py` —— 19 测试（此前无测试文件）
- `scripts/p36_smoke_sources.py` —— 真实联网冒烟（P0-8，刻意不进 pytest）
- `deliverables/software-company/futures-research/37-futures-datasource-architecture.md`

**修改**
`hexbroker/__init__.py`（4 个新异常）、`hexbroker/data/base.py`（`_finalize` 收口）、
`hexbroker/data/schema.py`（空帧门禁）、`sources/{sina,akshare,pytdx,csv,synthetic}_source.py`、
`sources/test_sina_source.py`、`pyproject.toml`

---

## 需要您拍板

1. **Q1**：是否接受备源只续接短期窗口（如 10 日）？
2. **Q6**：收盘价 vs 结算价 —— 实测两者同口径可任选，选哪个？
3. **下一步优先级**：继续 P0-4/5/6（备源链路闭环），还是先修 `p6_4_apply_persisted_dir.py
   --trading-day` 的「目录非空但数据陈旧仍 exit 0」语义缺口？
