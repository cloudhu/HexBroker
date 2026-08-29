# 交付总览 · 期货免费数据源多源故障切换（P0 批次）

**日期**：2026-08-29　|　**主理人**：齐活林（Qi）· 交付总监
**提交**：`1fcb940` → `c709101` → `1922673` → `952373c` → `6354bc5` → `2d9cfc4` → `e292fdb` → `6373482` → `6d8890d`(feat 护栏+恢复) → `17b7e03`(docs 复盘) → `8e85b2d`(feat P0-9 rebuild) → 本批次 feat/docs（编排器）

---

## 🔴 本批次追加：生产数据污染事故与恢复（§6.5）

一次常规全量 pytest（774 测试）在 2026-08-29 11:49:23 **静默污染了生产数据**：
`AkshareSource(save=True)` 默认落盘 + `DataLake.save_processed` 整文件覆盖，
导致 `rb0/2020`（→2 行）、`rb0/2026`（→3 行）、`cu0/2026`（→3 行且**内容是 rb0 的价格**）被覆盖。

**关键教训**：`data/raw/` 被 `.gitignore` 第 2 行忽略 → `git status` 恒为 0 →
**此前所有"零改动"检查对生产目录从未生效过**。

| 项 | 结果 |
|---|---|
| 2026 年度两个分区 | ✅ **真值重建**（非外推），18 品种全部 156 行 |
| 对照组回归（ni0） | ✅ 14 列 **0 处差异** |
| 续接器端到端验证 | ✅ graft 外推 vs 主源真值 **0.0000 bp** |
| `rb0/2020` | 🟡 隔离 + 显式标记（留一法实测重建误差 53 bp，不可用于回测） |
| 三层护栏 | ✅ 已上线，实战拦截 13:25 自动化任务的 pytest |
| 连带发现 | 🔴 **2023 年度口径断裂**：rb0/cu0 存未复权名义价，跨年约 35% 假跳空 |

---

## TL;DR

挖到了 08-28 停摆事故的**真根因**并修复：不是"取数失败"，而是**空 DataFrame 能通过全部数据契约校验**，
5 个数据源共用这条路径，于是"源停更 → 裁成 0 行 → 静默返回成功"被伪装成刷新成功。
同时复活了新浪源、首次跑通 AkShare 源，并发现 **akshare 与新浪是同一上游、不构成冗余**。
本批次追加处置了一起**测试污染生产数据**的事故，并装上三层隔离护栏。

---

## 交付状态

| 项 | 结果 |
|---|---|
| 测试 | **858 passed**（677 → … → 836 → 841 → 843 → 853 → 858，+10 名义价回填，+5 P0-11 驱动器） |
| 静态门禁 | 改动文件 ruff **全通过** |
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
| 附带 | `p6_4 --trading-day` 四态判定（EMPTY/PARTIAL/STALE） | ✅ |
| **P0-5** | 后复权续接器 `graft`（含换月默认值**按实测反转**） | ✅ |
| **P0-4** | 备源 raw 拉取器 `BackupRawFetcher` + `provisional` 契约 | ✅ |
| **P0-6** | 换月检测 —— 前向预测路线**证伪**，改事后对账 | 🟡 部分 |
| **P0-9** | provisional 重建流水线 `rebuild`（sidecar 挂标 + 扫描 + 真值重建，只清被覆盖日期） | ✅ |
| **P0-编排器** | 故障切换编排器 `failover`（三级降级 + 差异化路由 + NoAnchor 红线 + P0-9 自动挂标） | ✅ |
| **🆕 事故** | 测试污染生产数据 → 三层护栏 + 真值重建 + 隔离 | ✅ |
| **P0-10** | 年度口径审计（18 品种 × 9 年）：**确认仅 cu0/rb0 的 2023 名义价冒充**（±27%~51% 假跳空）；检测器固化 `caliber` | ✅ 审计 / 🟡 处置待拍板 |
| **P0-12** | 缺失年度显式化：`load_processed` 洞/标记逐条告警（数据行为不变）+ `quality_notes` 三类结构化提示 | ✅ |
| **P1-c** | 🆕 `raw_close` 列语义修复：parse 阶段 `enrich_raw_close` 备源名义价回填（失败大声降级），实测 raw_close ≠ adj_close | ✅ 新写入 / 🟡 存量回填待拍板 |
| **P1-b** | 🆕 生产 manifest 增量回填 16 品种（`skip_existing` 防触碰盘中自动化 manifest），processed 层覆盖 2 → 18 | ✅ |
| **P0-11** | 🆕 `rb0/2020` 真值重建驱动器就绪（dry-run/--apply + 自动清标 + 边界验证）；**真值拉取待会话重载注册 pandadata 工具** | 🟡 驱动器 ✅ / 执行阻断 |
| **P0-12** | 缺失年度显式化（`load_processed` 静默跳过） | ✅（重复行，上为准） |
| **P0-13** | `hc0`/`ni0` OHLC 包络校验失败：根源端毛刺 bar（各 1 根）；`schema.repair_envelope` 收口三源修复，akshare 补缺失步骤 + 大声告警 | ✅ |

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
5. **🔴 近似价差是净损害**（18 品种消融）：`dominant_id` 切换日 **≠** 后复权因子切换日
   （cu0 相差 3 个交易日；ni0 换了月但比值根本没变）。A 无视换月 1.19bp vs
   B 近似价差 4.31bp（2/18 劣化、0/18 优于 A）→ **默认值已反转为"跳过调整 + 告警"**。
   cu0 误差 57.41 → 21.35 bp，ni0 20.12 → **0.00 bp**。
6. **续接精度实测**：16/18 品种与 pandadata 真值**逐位相同**（0.00 bp）
   （做法：截断主源真值后半段做对照，不是自证）。
7. **🔴 零告警也可能有误差**：cu0 端到端续接出 -21.35 bp 但 `warnings` 为空 ——
   口径跳变在锚点**之后**，重叠区无对照样本，**原理上不可检出**。
   故确立 `provisional` 契约：续接段一律标记临时值，主源恢复后必须重建。
8. **🆕 续接器真实端到端复验：0.0000 bp**（关键结论 7 的重要补充）：
   在恢复 `cu0`/`rb0` 08-28 时，graft 外推值与主源真值**逐位相同** —— 而且是
   **在 cu0 带 2 条告警、`breaks=3` 的不利条件下**达成的。
   → 告警是**保守提示**，不是精度判决。只要 anchor 落在最后一个**稳定段**
   （cu0 在 08-21~08-27 五日内比值恒定 `1.475842`），外推即精确。
   结论 7 的 21.35 bp 源于 anchor 取在跳变**之前**的段，属**锚点选取**问题。
9. **🆕 锚点污染的危害远大于插值方法本身**：留一法实验最初用 2022 年度，
   得误差 1269.98 bp；换用口径连续的 2025 年度后降至 53.22 bp ——
   差异**全部**来自后锚点取自口径已断裂的 2023 年（`k` 被污染为 1.0）。
11. **🆕 P0-9 落地：临时值绝不静默转正**：`rebuild.py` 的重建语义是
    **逐行替换 + 追加，只清被真值覆盖的日期** —— 真值没覆盖到的 provisional
    日期保留挂标（专项回归测试固化）。标记用独立 `_provisional.json` sidecar，
    不进 manifest（manifest 会被常规写入整份重建，挂标会被冲掉）。
12. **🆕 插值重建年度数据不可行**：即使两端口径连续（2025 年度），
    线性插值绝对均值误差仍有 **53.22 bp**（P95 121.72 bp，最大 135.38 bp），
    且年内约 10 次跳变，每次可造成 1%~2% 虚假收益 → 不可用于回测。
13. **🆕 编排器锚定前提**：备源/Tier3 请求窗口必须**向前扩展 lookback 天**——
    graft 依赖 raw 与湖内 adj 的重叠日期定锚，按调用方 start 原样请求时
    两序列无交集，续接必然失败（首轮 6 个测试失败同根因，属真实设计缺口而非夹具问题）。
    差异化路由：Quota 当日锁 / Network 重试一次 / Empty·Stale 零重试；
    湖内无锚点（NoAnchor）拒绝续接，续接成功自动 P0-9 挂标，编排器不落数据。
14. **🆕 P0-10 审计定案**：全库 18 品种 × 9 年度（外部新浪名义价校准 + 内部
    边界跳变交叉）——**名义价冒充仅限 cu0/rb0 的 2023 年度**（k≡1.0，
    相邻年 1.27~1.49，假跳空 -26.9%~-32.7% / +36.2%~+50.6%）；ag/au/m 全程
    k≈1 属真值特征（滚动价差极小）；其余 13 品种干净。
    ⚠️ 附带发现：湖内 `raw_close` 列恒等于 `adj_close`（管线映射），**不携带
    名义价校准信息** → 湖内自检失效，立 P1-c。
    处置选项待拍板：①隔离 2023 + `_MISSING_2023.json`（沿 rb0/2020 先例）
    ②保留 + 备案 ③等 pandadata 授权恢复后真值重建（根治，插值已证伪不可用）。
15. **🆕 P0-12 落地：读路径不再静默**：`load_processed` 对年度洞/`_MISSING`
    标记逐条告警（数据行为不变，洞照旧拼接但不再无声），`quality_notes`
    提供三类结构化提示（hole / stale_mark / missing_mark）供程序化消费。
    生产 rb0 实战复核：2020 洞带完整标记内容告警，1855 行读取逐位一致。
    → ①隔离处置方案的配套告警已就绪。
16. **🆕 P0-13 定案：校验器没错，是数据源脏**：hc0/ni0 各恰好 1 根新浪源端
    毛刺 bar（hc0 2021-12-30 C=4394<L=4395 差 1 点；ni0 2023-08-28
    C=167030<L=167230 差 200 点，1/3000 量级）。修复能力三源三份且不一致
    （sina 有 / akshare 完全缺失 / pytdx 语义略异）→ 收口为
    `schema.repair_envelope`（四价极值重定 low/high + 丢非正价，返回告警
    列表），akshare 补齐并大声告警 —— **修复是数据变更，静默即事故**。
    端到端实测：hc0/ni0 全历史 4199 行拉取成功（此前被拒），告警逐日命中
    探针定位日期。
17. **🆕 P1-b 落地：manifest 机制从形同虚设到全覆盖**：`backfill_manifests`
    早已存在但从未对生产执行，且存在两个接线缺口 —— ①会无条件重写盘中
    自动化维护的 cu0/rb0 manifest（v1 ↔ backfill-* 无谓翻覆）→ 补
    `skip_existing` 增量模式（幂等）；②fundamental/global 扁平布局天然
    不覆盖 → 另案。生产执行回填 16 品种，manifest 覆盖 2 → 18；
    回填语义 = 整 freq 目录全量（date_range/行数/指纹/口径常量），
    与 save_processed 的"最近一次写入"语义不同（遗留观察，随 P1-c 一并考虑）。
18. **🆕 P1-c 落地：raw_close 不再是 adj 的复制品**：p6_4 parse 阶段新增
    `enrich_raw_close` —— 备源（sina/akshare，save=False）名义价按日期
    对齐回填 raw_close，覆盖率 <90% / 备源全败时**原样返回 + 大声归因**
    （不炸管线，真实拉取失败由 §4.7 体检拦住）。dry-run 实测：
    raw_close=107690（名义）≠ adj_close=158594（复权），"名义价冒充"
    从此可湖内自检（caliber 检测器有了湖内分子）。
    ⚠️ 存量分区（全部 18 品种全历史）仍是复制品，回填选项待拍板
    （离线回填 / 随 P0-11 真值重建 / 维持现状）。
19. **🆕 P0-11 驱动器就绪，唯一剩余 = 会话重载**：pandadata 连接器已恢复
    connected，但 MCP 工具注册发生在会话启动时 —— 本会话工具表里没有它，
    直连端点 401（token 宿主加密托管）。重建已收敛为
    `scripts/p11_truth_rebuild.py` 一条命令（复用 p6_4 规范化 + P1-c
    名义价回填 + P0-9 rebuild_partition，dry-run 默认），重建分区将
    **直接携带真名义价**。新会话执行：拉真值 → --apply → 自动清标 +
    边界连续性验证。

---

## 🔴 新增风险（需您处置）

**pandadata MCP 连接器 token 已失效**（2026-08-28 19:50 实测 `Authentication required`）。
主源当前**不可用**，备源链路与续接器已从"备用"变成"刚需"。
在此之前，§4.7 的 `--trading-day` 体检是唯一能拦住"静默用旧数据"的闸门。

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
- `conftest.py` —— 🆕 **重写为三层护栏**（autouse 隔离 / `real_data_lake` opt-in /
  兜底哨兵拦截生产目录写入）。原文件仅 8 行 sys.path 设置
- `scripts/dev_restore_polluted_2026.py` —— 🆕 污染分区恢复工具
  （内置 ni0 对照组回归门禁，对照组不过则拒绝落盘）
- `scripts/dev_probe_year_rebuild_error.py` —— 🆕 年度重建误差留一法评估
- `hexbroker/data/test_freshness.py` —— 17 测试
- `hexbroker/data/graft.py` —— 后复权续接器（P0-5）
- `hexbroker/data/test_graft.py` —— 25 测试（含真实事故模式固化）
- `hexbroker/data/backup.py` —— 备源 raw 拉取器（P0-4）
- `hexbroker/data/test_backup.py` —— 19 测试
- `hexbroker/data/rebuild.py` —— 🆕 P0-9 provisional 标记与真值重建流水线
- `hexbroker/data/test_rebuild.py` —— 🆕 22 测试（含静默转正回归门禁）
- `hexbroker/data/failover.py` —— 🆕 三级故障切换编排器（主→备1→备2）
- `hexbroker/data/test_failover.py` —— 🆕 14 测试（差异化路由 / NoAnchor 红线 / 跨尺度锚点）
- `hexbroker/data/caliber.py` —— 🆕 年度口径检测器（边界假跳变 + 名义嫌疑年）
- `hexbroker/data/test_caliber.py` —— 🆕 12 测试（rb0 断裂形态固化）
- `tests/test_store_quality.py` —— 🆕 14 测试（P0-12 缺失年度显式化）
- `scripts/dev_probe_p0_10_caliber.py` —— 🆕 P0-10 审计探针（只读，外部名义价校准）
- `artifacts/p0_10_audit_20260829.log` —— 🆕 审计证据存档
- `scripts/dev_probe_p0_13_envelope.py` —— 🆕 P0-13 探针（只读直调 `ak.futures_main_sina` 定位毛刺 bar）
- `artifacts/p0_13_probe_20260829.log` —— 🆕 探针证据存档
- `scripts/dev_probe_37_graft_truth.py` —— 真实数据反证 + 三策略消融
- `hexbroker/data/sources/test_akshare_source.py` —— 24 测试（19 既有 + 5 包络修复）
- `scripts/p36_smoke_sources.py` —— 真实联网冒烟（P0-8，刻意不进 pytest）
- `tests/test_p6_4_apply_gate.py` —— 17 测试
- `deliverables/software-company/futures-research/37-futures-datasource-architecture.md`

**修改**
`hexbroker/__init__.py`（4 个新异常 + 补齐 Quota/Network 的 source/symbol）、
`hexbroker/data/base.py`（`_finalize` 收口）、`hexbroker/data/schema.py`（空帧门禁）、
`sources/{sina,akshare,pytdx,csv,synthetic}_source.py`、`sources/test_sina_source.py`、
`scripts/p6_4_apply_persisted_dir.py`（四态判定）、`pyproject.toml`

---

## 需要您拍板

1. **Q1**：是否接受备源只续接短期窗口（如 10 日）？
2. **Q6**：收盘价 vs 结算价 —— 实测两者同口径可任选，选哪个？
3. **🔴 pandadata 连接器 token 失效**，是否现在恢复授权？（主源不可用期间，
   整个备源链路的优先级建议上调；`rb0/2020` 的重拉也卡在这里）
4. **下一步优先级**：P0-9 ✅、编排器 ✅、P0-10 审计 ✅（处置待拍板）、P0-12 ✅、P0-13 ✅、P1-b ✅、P1-c ✅（存量回填待拍板）、P0-11 驱动器 ✅（执行待会话重载）。剩余拍板项 = cu0/rb0 2023 处置 / raw_close 存量回填；剩余执行 = 新会话拉真值跑 P0-11 --apply、扁平层 manifest 另案。
5. **🔴 cu0/rb0 2023 处置拍板**：①隔离 + `_MISSING_2023.json`（留 1 年洞，需 P0-12 配套）②保留 + 备案 ③等授权恢复后真值重建？
5. **🔴 新增 · 2023 年度口径断裂**（rb0/cu0 存未复权名义价，跨年约 35% 假跳空）：
   是否立 P0-10 优先处理？影响所有跨 2023 年的回测与训练，且数据"看起来完全正常"。
6. **🆕 新增 · `rb0/2020`**：当前为隔离 + 显式标记状态。是否接受
   "2020 年度暂时不可用"，等 pandadata 恢复后重拉？
7. **🆕 新增 · 生产 manifest 缺失**：全库仅 cu0/rb0 两个品种有 manifest，
   其余 16 个从未生成。是否补全，还是索性废弃 manifest 机制？
