# 37 · 期货免费数据源多源故障切换 —— 架构决策与主理人复核

> **项目**：HexBroker（中国大宗商品期货 AI 量化交易系统）
> **需求**：深度挖掘期货免费数据源，打造实时稳定可靠的数据流
> **日期**：2026-08-29
> **主理人**：齐活林（Qi）· 交付总监
> **上游**：36 号调研报告（PM 许清楚产出，30,797 字节 / 428 行）

---

## 0. 团队机制降级声明（必读）

按 SOP 本应由**架构师高见远**产出本文。实际情况：

| 成员 | 状态 | 说明 |
|---|---|---|
| software-product-manager（许清楚） | ❌ `Task agent ... is not available` | **完成 36 号报告后**失效，产出已落盘 |
| software-architect（高见远） | ❌ `Task agent ... is not available` | **0 秒启动即失败** |
| software-engineer（寇豆码） | ❌ `Task agent ... is not available` | 前段失效 |
| software-qa-engineer（严过关） | 未调度 | — |

**判定**：系统级智能体注册故障（三成员无一可用，architect 为启动即失败），非任务难度问题。

**处置**：按既定约定降级为**主理人直接实施 + 自验**。本文及本批次代码均为**主理人产出，非团队成员专业产出**，特此标注，不伪装为团队交付。SOP 的阶段划分、质量关卡、feat+docs 双提交纪律予以保留。

---

## 1. TL;DR

1. **找到了 08-28 停摆事故的真根因**：不是"取数失败"，而是"取数**成功**但结果为空/陈旧"。空 DataFrame 能通过 `validate_bars` 全部校验项，5 个源共用同一条路径 → 已在咽喉处修复。
2. **新浪源复活**：不是新浪死了，是项目用了冻结在 2024-07-17 的旧端点。换新端点后 18/18 品种、末日当天。
3. **AkShare 源首次真正跑通**：此前因中文列名 `KeyError` 从未工作过，且**根本没有测试文件**。
4. **🔴 坏消息**：akshare 与新浪新端点返回**同一份数据**（cu0 2026-08-28 收盘价两边逐位相同 = 108900.0）→ **两者不构成冗余，是同一个单点故障**。真正的第二备源只能是交易所官方。
5. **pandadata 可回溯到 2020+** → dominant 日历可本地快照化，解开"主源挂了却要问主源要日历"的死结。

---

## 2. 主理人对 36 号报告的三处更正（独立取证）

### 2.1 🔴 更正一：静默空返回的机制定位错误 → 根因升级

**PM 报告**：`SinaSource.fetch_bars` 静默返回 0 行。

**主理人复核**：机制**不在** `_rows_to_frame`（那里 `:168` 反而有空帧抛错）。真实链路：

| 步骤 | 位置 | 行为 |
|---|---|---|
| 1 | `sina_source.py:99 _fetch_one` | 抓全量历史（冻结 2024-07-17）→ 成功 |
| 2 | `:156 _rows_to_frame` | 构造非空 DataFrame → 成功 |
| 3 | **`base.py:37 _clip_range`** | 按 `[start,end]` 裁剪 → **0 行，静默** |
| 4 | **`schema.py:61 validate_bars`** | **空 DataFrame 通过全部校验** |
| 5 | 返回 | `OK rows=0` |

第 4 步为何放过空帧：`has_duplicates` 为 False ✓、列齐全 ✓、`groupby` 无分组故单调性检查空转 ✓、价格比较对空 Series 恒真 ✓。

**影响面**：`sina:210` / `pytdx:382` / `csv:47` / `akshare:55` / `synthetic:55` **五个源共用同一条路径**。

> **结论：这是全系统咽喉缺陷，逐个源打补丁无效，必须打在咽喉。已修复（见 §4.1）。**

### 2.2 🟡 更正二：新浪不是死源，是端点旧

| 端点 | 状态 | 字段 |
|---|---|---|
| 旧 `json.php/IndexService.getInnerFuturesDailyKLine` | **冻结 2024-07-17** | 6 字段（无持仓量/结算价） |
| 新 `jsonp.php/.../InnerFuturesNewService.getDailyKLine` | **新鲜到 2026-08-28** | 8 字段（含持仓量 `p`、结算价 `s`） |

项目内 `sina_source.py:28,32` 用的正是旧端点。akshare 的 `futures_main_sina` 走的正是新端点 —— 这是 PM 误判的由来（两者结论相反却都成立）。

**生产隐患性质**：若直接故障切换到旧端点实现，会**静默喂两年陈数据**。

### 2.3 🟡 更正三：交易所官方端点

| 交易所 | PM 报告 | 主理人实测 |
|---|---|---|
| SHFE / INE | ✅ 200 可用 | 根域 200，但 `/data/dailydata/kx/kx{date}.dat` **四个日期全部 404**；正确路径 `/data/tradedata/future/dailydata/` |
| CZCE | 未发现可用 | `.htm` → 412，但 **`.txt` → HTTP 200 / 37,379 字节真数据**，268 行，CF(6)+SR(6)+TA(12)=**24 合约全得**，覆盖本系统全部 3 个郑商所品种 |
| DCE | — | **全站 412**（含根域），本环境不可达 |

### 2.4 pytdx 定性（P2）

- `TdxHq_API`（7709）：握手成功延迟 0.19s，但 27 个方法、`get_security_count(28/29/30/47)` 全返回 None → **标准协议不含期货扩展市场**。
- `pytdx.exhq.TdxExHq_API`（7727，才有 `get_instrument_bars`）：**裸 socket 测 TCP 7727 通**（排除防火墙），但 `connect()` 全 TimeoutError → **协议握手失败，非网络封锁**。

---

## 3. Q3 答案：pandadata 历史 `dominant_id` 可回溯 ✅

PM 因工具索引无 `mcp__pandadata__*` 未答。主理人实测 `20200302~20200306` 四品种：

| 品种 | dominant_id | close_pcr（后复权） | open_interest | settlement |
|---|---|---|---|---|
| CF | `CF005.CZC` | 6467.264… | 424144（不复权） | 6454.21（**已复权**） |
| J | `J2005.DCE` | 2190.007… | 120025 | 2175.11 |
| RB | `RB2005.SHF` | 3688.205… | 1466679 | 3663.42 |
| TA | `TA005.CZC` | 4432.814… | 847983 | 4422.51 |

**20/20 行完整返回**。三个衍生结论：

1. **`settlement` 与 `close` 同属 `close_pcr` 口径**（均被复权）→ **Q6「收盘价 vs 结算价」答案：两者同口径可任选**。
2. **`open_interest` 不复权**（真实整数）→ 主力判定可放心使用。
3. **🔑 解锁架构决策**：既然能回溯 2020+，**dominant 日历可一次性本地快照化**（落 parquet 缓存）。这解开了 PM 方案隐含的死结 —— "主源挂了，但 dominant 日历还得问主源要"。

**两个新坑（已记录）**：
- `start_date` 必须是 `YYYYMMDD` **无横线**，传 `2020-03-02` → HTTP 400。
- **默认复权方法是 `close_pcs` 而非项目用的 `close_pcr`**，不显式传 `method` 会拿到错误口径。

---

## 4. 已落地实施（本批次，commit `1fcb940`）

### 4.1 P0-0 咽喉门禁

- `schema.validate_bars(df, freq, allow_empty=False)` —— 空帧默认抛 `HexEmptyDataError`；需容忍须**显式**声明。
- 新模块 `hexbroker/data/freshness.py` —— `assert_nonempty` / `assert_fresh` / `check_fetch_result`：
  - 陈旧判定：最新 bar 早于 `end - max_stale_days`（默认 5 日历日，覆盖周末+3 天连休）→ `HexStaleDataError`。
  - **历史回填自动豁免**：`end < today - max_stale_days` 时不做新鲜度判定。
  - `today` 可注入 → 单测确定性。
- `DataSource._finalize(...)` —— 统一收口「裁剪 → 门禁 → BarFrame → 校验」，**5 个源全部改走此路径**。
- 新增可区分异常（P0-7）：`HexEmptyDataError` / `HexStaleDataError` / `HexQuotaError` / `HexNetworkError`。

**为什么默认值能硬化**：全量 **677 测试在改为严格默认后仍然全绿** → 代码库中无任何地方依赖"空帧通过校验"。这是证据，不是假设。

### 4.2 P0-1 新浪端点复活

换 `jsonp.php/InnerFuturesNewService.getDailyKLine`，新增 JSONP 剥壳（`var _RB0=(...);`）与单字母字段映射。

**真实联网验证**：18/18 品种、108 行、末日 **2026-08-28**、10.3s；`open_interest`/`settlement` 有真值。

### 4.3 P0-3 AkShare 源修复

akshare 返回中文列名 `日期 开盘价 最高价 最低价 收盘价 成交量 持仓量 动态结算价`，原实现按英文列名 rename 无效 → `KeyError: 'datetime'`。

修复后真实联网验证：18/18 品种、108 行、末日 2026-08-28、7.9s；`open_interest`/`settlement` 真实填充（原硬编码 0.0）。

> **注**：akshare 对大小写**不敏感**（`rb0`/`RB0` 均可），与 pandadata 的"必须大写"铁律相反。

### 4.4 D3 pytdx symbol 命名错位

主力连续原输出 `rb`，生产读取 `rb0` → 该源产出**永远检索不到**。已修。

### 4.5 附带发现：`testpaths` 白名单漏网

`pyproject.toml` 的 `testpaths = ["tests", "hexbroker/data/sources"]` 是**白名单**，新增的 `hexbroker/data/test_freshness.py` 在名单外 → **17 个测试永不执行**。与 PM 报告的"mock 假绿灯"是同一类病（测试存在但无效）。已补 `hexbroker/data` 并加注释警示。

### 4.6 P0-8 真实联网冒烟脚本

`scripts/p36_smoke_sources.py` —— 刻意**不进 pytest**（CI 不应依赖外网）。退出码 `0` 至少一个源新鲜 / `1` 无源新鲜 / `3` 脚本异常。

```
[smoke] symbols=18 sources=['sina', 'akshare'] window=2026-08-21..2026-08-28
  [sina]    OK rows=108 syms=18 last=2026-08-28 10.338s
  [akshare] OK rows=108 syms=18 last=2026-08-28 7.864s
```

---

## 5. 🔴 关键架构发现：akshare 与新浪不构成冗余

实测交叉验证：

| 品种 / 日期 | sina 新端点收盘 | akshare 收盘 |
|---|---|---|
| cu0 / 2026-08-28 | 108900.0 | **108900.0** |
| rb0 / 2026-08-28 | 3112.0 | **3112.0** |

**逐位相同**。原因：akshare 的 `futures_main_sina` 就是新浪新端点的包装器。

> **结论：sina + akshare 是同一个上游的两层皮，故障切换在它们之间没有意义。**

因此源的分层必须重新定义：

| 层级 | 源 | 独立性 | 状态 |
|---|---|---|---|
| **主源** | pandadata（`close_pcr` 后复权，口径权威） | 独立 | 可用（依赖 MCP 连接器接入） |
| **备源 1** | 新浪新端点 / akshare（**同一上游**） | 与彼此不独立 | ✅ 已修复可用 |
| **备源 2（真正独立）** | 交易所官方 | **独立** | CZCE `.txt` ✅ 已验证；SHFE/INE 路径待修；DCE 412 待换环境 |
| **P2 候选** | pytdx 扩展行情 / 东财 / TQSDK / CTP | 独立 | pytdx 握手失败；东财两种环境均失败；其余需账号 |

---

## 6. 架构原则（沿用 PM §3.4，主理人复核认可）

**核心原则：不换口径，只换 raw 供给。**

主源 pandadata 提供权威 `close_pcr` 后复权序列；备源**只供 raw 名义价**，由本地续接器挂到既有后复权序列上，**备源不得自行定义主力连续合约**。

**续接算法**（`adj` = 既有后复权，`raw_backup` = 备源名义价，`t0` = 锚点日）：
- 无换月：`adj[t] = adj[t0] × raw_backup[t] / raw_backup[t0]`
- 有换月日 R：`factor = raw_N[R] / raw_O[R-1]`，`t ≥ R` 段整体乘 `factor` 后再套上式

**五条硬约束**：
1. dominant 日历唯一权威 = pandadata（**可本地快照化**，见 §3）
2. 备源只供 raw，不自定主力连续
3. 锚点冻结：`adj[t0]` 与 `raw_backup[t0]` 必须同一天
4. **校准校验（零成本在线闸门）**：同段内 `adj[t]/raw_backup[t]` 应为常数，偏离 → 判换月并告警
5. 换源 = 换基准，须全量重建 + 重训 + 重校准

**口径对齐实证**（决定备源接线方式）：
- pandadata 为**比例后复权**，与免费源名义价差常数比例因子（18 品种跨度 **0.4781(cf0) ~ 8.7769(i0)**）→ **禁止直接拼接**。
- 比**日收益率**后 **14/18 品种逐位相同**（比值变异系数 ≈1e-16）。
- 剩余 4 品种分歧**全部只发生在主力换月当日一天**：`j0` 08-18 差 **493.7bp**、`ta0` 08-19 差 161.1bp、`hc0` 08-19 差 62.7bp、`cu0` 08-21、`al0` 08-17；换月后逐位相同。

---

## 7. 待办（按优先级）

### P0（未完）
- [ ] **P0-4 备源 raw 拉取器**（基于 §4.2/4.3 已修好的源）
- [ ] **P0-5 后复权续接器**（§6 算法）
- [ ] **P0-6 换月检测与告警**（约束 4 的在线闸门）
- [ ] **故障切换编排器**：主 → 备1 → 备2 三级降级，按 §4.1 的异常类型差异化路由

### P1
- [ ] SHFE / INE 官方源路径修正（`/data/tradedata/future/dailydata/`）
- [ ] CZCE `.txt` 官方源接入（已验证 24 合约）
- [ ] `p6_4_apply_persisted_dir.py --trading-day` 语义缺口：**目录非空但数据陈旧**目前仍返回 `exit 0`，须与 P0-0 同源修复
- [ ] dominant 日历本地快照化（§3）

### P2
- [ ] DCE 官方源（本环境 412，需换网络环境）
- [ ] pytdx 扩展行情 7727（握手失败，需换环境）
- [ ] 东财（代理/非代理两种环境均失败，倾向排除）

### 待用户拍板（Q 系列）
- **Q1**：是否接受备源只续接短期窗口（如 10 日）？
- **Q6**：收盘价 vs 结算价 —— **已由 §3 答：两者同口径可任选**，待确认选哪个
- Q2 / Q4 / Q7：沿用 36 号报告原文

---

## 8. 文件清单（本批次）

| 文件 | 变更 |
|---|---|
| `hexbroker/__init__.py` | 新增 4 个可区分异常 |
| `hexbroker/data/freshness.py` | 🆕 空结果 / 陈旧门禁模块 |
| `hexbroker/data/base.py` | 新增 `_finalize` 统一收口 + `today` 注入口 |
| `hexbroker/data/schema.py` | `validate_bars` 空帧门禁 |
| `hexbroker/data/sources/sina_source.py` | 换新端点 + JSONP 解析 + 字段映射 + 收口 |
| `hexbroker/data/sources/akshare_source.py` | 中文列名映射 + 签名统一 + 收口 |
| `hexbroker/data/sources/pytdx_source.py` | D3 symbol 修复 + 收口 |
| `hexbroker/data/sources/csv_source.py` | 收口（关闭新鲜度判定） |
| `hexbroker/data/sources/synthetic_source.py` | 收口（关闭新鲜度判定） |
| `hexbroker/data/test_freshness.py` | 🆕 17 测试 |
| `hexbroker/data/sources/test_akshare_source.py` | 🆕 19 测试（此前无测试文件） |
| `hexbroker/data/sources/test_sina_source.py` | mock 修正为 JSONP 真实契约 |
| `scripts/p36_smoke_sources.py` | 🆕 真实联网冒烟（P0-8） |
| `pyproject.toml` | testpaths 补 `hexbroker/data` |

**验证**：713 passed（原 677 + 新增 36）；改动文件 ruff 全通过；真实联网 18/18 双源末日 2026-08-28。
