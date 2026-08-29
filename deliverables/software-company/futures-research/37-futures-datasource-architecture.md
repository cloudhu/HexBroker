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

### 4.7 「交易日拉取体检」三重判定（补 P1-a 语义缺口）

`scripts/p6_4_apply_persisted_dir.py --trading-day` 原只判「目录 0 个 json」。补齐为三重判定，
任一不通过即醒目横幅 + `exit 3`：

| 判定 | 场景 | 旧行为 |
|---|---|---|
| **EMPTY** | 目录 0 个 json | 已覆盖 |
| **PARTIAL** | 品种数 < `--expect`（如 18 只落 5 个） | ⚠️ **exit 0**，下游按 18 品种出信号缺腿 |
| **STALE** | 数据最新日期 早于 `--asof` - `--stale-days` | ⚠️ **exit 0**，把真实拉取失败伪装成刷新成功 |
| **EMPTY_DATE** | 文件存在但取不到日期字段 | ⚠️ 同 STALE |

新增 `--asof`（基准交易日，默认今天）与 `--stale-days`（默认取数据层
`DEFAULT_MAX_STALE_DAYS`，惰性导入以免失败快速路径依赖 pandas）。
历史回填场景不加 `--trading-day`，避免被陈旧判定误杀。

**判定文案须与事实一致**：初版在 PARTIAL 场景下会错报"数据陈旧"（数据其实是新鲜的），
已修正 —— 误导性告警比没有告警更糟。

**测试方式**：`tests/test_p6_4_apply_gate.py`（17 测试）。刻意用单测而非跑真目录验证 ——
本脚本 happy path 会真正调用 `p6_4_fill_gaps.py --stage parse` **写入生产 parquet**，
用真实目录验证门禁会造成生产数据污染。

### 4.6 P0-8 真实联网冒烟脚本

`scripts/p36_smoke_sources.py` —— 刻意**不进 pytest**（CI 不应依赖外网）。退出码 `0` 至少一个源新鲜 / `1` 无源新鲜 / `3` 脚本异常。

```
[smoke] symbols=18 sources=['sina', 'akshare'] window=2026-08-21..2026-08-28
  [sina]    OK rows=108 syms=18 last=2026-08-28 10.338s
  [akshare] OK rows=108 syms=18 last=2026-08-28 7.864s
```

### 4.8 P0-5 后复权续接器（`graft`）+ 🔴 换月处理默认值**反转**

新增 `hexbroker/data/graft.py`：主源（pandadata 后复权）停更时，把备源名义价按锚点
比例因子续接上去，保持口径不变。设计要点是**先验证、再外推**：

> 「段内 `adj/raw` 应为常数」这条约束在**向前外推时是恒真式**（adj 本就由 raw 算出），
> 唯一有意义的位置是**锚点之前的重叠区**（两边都是已知真值）。

#### 真实数据端到端反证（不是自证）

用 `scripts/dev_probe_37_graft_truth.py`：把 pandadata 后复权**截断**到 08-20 当作主源历史，
用新浪名义价续接 08-21~08-27，再与**被截掉的真值后半段**逐日比对。

| 品种 | 误差 | 品种 | 误差 |
|---|---|---|---|
| ag0 / al0 / au0 / cf0 / hc0 / i0 / j0 / jm0 | 0.00 bp | m0 / p0 / rb0 / sc0 / sr0 / ta0 / y0 / zn0 | 0.00 bp |
| **cu0** | 21.35 bp | **ni0** | 0.00 bp |

**16/18 品种与真值逐位相同。**

#### 🔴 消融：近似价差是净损害，默认值已反转

| 策略 | 均值 | 说明 |
|---|---|---|
| A 无视换月（k 恒定） | **1.19 bp** | |
| B `dominant_id` + 近似价差 `raw[R]/raw[R-1]` | 4.31 bp | **2/18 劣化**、0/18 优于 A |
| C `dominant_id` + 精确价差（Oracle） | 1.19 bp | 与 A 逐品种相同 |

**根因**：pandadata 的 `dominant_id` 切换日 **≠** 后复权因子切换日。

- **cu0**：比值 08-14~08-20 恒为 1.472692，08-21 起跳到 1.475842（+0.2139%）；
  而 `dominant_id` 到 **08-24** 才从 CU2609 切到 CU2610 —— **相差 3 个交易日**。
  按 08-24 施加近似价差，把误差从 -21.35 bp **放大**到 -57.41 bp。
- **ni0**：`dominant_id` 在 08-21 变了，但比值**根本没变** → 施加调整纯属无中生有（+20.12 bp）。

> **决策（有证据即更正）**：未提供精确价差时**跳过**换月调整并告警，
> **绝不静默套用近似价差**。修复后 ni0 20.12 → **0.00 bp**，cu0 57.41 → **21.35 bp**。

cu0 残留的 21.35 bp **不可约** —— 主备主力切换日相差 3 个交易日，该信息在续接时无从得知。
应对：限制续接窗口 + 主源恢复后**用主源重建**被续接的窗口（见 §7 P0-6）。

#### 换月日检测尝试（未采纳为默认值）

试图仅用备源持仓量（OI）拐点检测主力切换：cu0 检出 08-20（对应 08-21 跳变 ✅）、
ni0 检出 08-20 ✅，但 al0 检到 08-14（实际 08-17）、ta0 检到 08-13（实际 08-19），
且检出频率 2.5~14 次/年明显虚高。**命中率约一半，噪声过大，不作唯一判据。**

### 4.9 P0-4 备源 raw 拉取器（`backup`）

新增 `hexbroker/data/backup.py` —— `BackupRawFetcher`，按优先级从免费源拉**名义价**，
供 §4.8 的续接器消费。三条设计红线：

1. **备源只供 raw，绝不自造后复权** —— 复权口径唯一权威是主源。
2. **失败必须可区分归因** —— `BackupExhaustedError.attempts` 逐源留痕
   （源名 + 异常类型 + 原因），杜绝"静默降级"。
3. **新鲜度门禁不放过** —— 复用 `assert_fresh`（自带历史回填豁免），陈旧即视为该源失败。

`save` **默认 False**：备源数据仅用于续接，不得落数据湖，否则后续
"主源恢复后重建"会分不清哪些是权威数据。

**交叉校验的能力边界**（已在代码注释中明示）：新浪与 akshare 是同一上游，
该校验**只能**发现解析层分叉，**无法**发现上游停更（两者会同时坏）。
真实联网实测 2 品种 2.34s、零告警（同一上游 → 收盘价逐位相同，符合预期）。

**端到端实测**（真实联网 + pandadata 真值对照）：

```
BackupRawFetcher(sina,akshare) → graft_adjusted
锚点 2026-08-20  k0=1.472692  重叠 n=5
cu0 续接 08-21~08-27：误差 -21.35 bp，告警 0
```

⚠️ **零告警却有 21.35 bp 误差** —— 口径跳变（08-21）发生在锚点**之后**，
重叠区没有对照样本，**原理上不可检出**。因此新增 `GraftResult.provisional`
标记：任何续接段一律标记为临时值，主源恢复后**必须**重建该窗口。
不能靠 `warnings` 兜底（本例 warnings 为空）。

**顺带修复**：`HexQuotaError` / `HexNetworkError` 此前缺少 `source`/`symbol`
参数，与其余三个数据异常不一致，导致故障切换编排器无法统一归因。已补齐。

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
  - ⚠️ **该式已实测证伪，见 §4.8** —— 仅当 `factor` 来自逐合约精确价差且日期正确时可用；
    无精确价差时**跳过调整**，不做近似。

**五条硬约束**：
1. ~~dominant 日历唯一权威 = pandadata~~ → **🔴 已更正，见 §4.8**：`dominant_id` 切换日
   ≠ 后复权因子切换日（cu0 差 3 个交易日）。仍**可本地快照化**（见 §3）用于粗粒度参考，
   **但不得作为续接器的换月调整依据**。
2. 备源只供 raw，不自定主力连续
3. 锚点冻结：`adj[t0]` 与 `raw_backup[t0]` 必须同一天
4. **校准校验（零成本在线闸门）**：同段内 `adj[t]/raw_backup[t]` 应为常数，偏离 → 判换月并告警
5. 换源 = 换基准，须全量重建 + 重训 + 重校准
6. **🆕 续接值一律标记为 provisional（降级）**：主源恢复后必须**用主源重建**
   被续接的窗口，不得让续接值长期沉淀。理由：主备主力切换日不一致会引入
   **恒定乘性偏移**（cu0 实测 21.35 bp，不可前向消除），只能事后重建消除。

**口径对齐实证**（决定备源接线方式）：
- pandadata 为**比例后复权**，与免费源名义价差常数比例因子（18 品种跨度 **0.4781(cf0) ~ 8.7769(i0)**）→ **禁止直接拼接**。
- 比**日收益率**后 **14/18 品种逐位相同**（比值变异系数 ≈1e-16）。
- 剩余 4 品种分歧**全部只发生在主力换月当日一天**：`j0` 08-18 差 **493.7bp**、`ta0` 08-19 差 161.1bp、`hc0` 08-19 差 62.7bp、`cu0` 08-21、`al0` 08-17；换月后逐位相同。

---

## 6.5 🔴 生产数据污染事故与恢复（2026-08-29）

> **本節独立成章**：这起事故的性质超出了"数据源"范畴 —— 它证明
> **数据层的写入路径缺乏隔离，测试可以覆盖生产数据，且 git 无法兜底**。

### 6.5.1 事故经过

2026-08-29 11:49:23，一次常规全量 pytest（774 测试）**静默污染了生产数据**。

| 受损文件 | 大小 | 内容 |
|---|---|---|
| `data/raw/processed/rb0/1d/2020.parquet` | 9133 B | 全年 → **2 行**合成数据 |
| `data/raw/processed/rb0/1d/2026.parquet` | 9266 B | 全年 → **3 行** |
| `data/raw/processed/cu0/1d/2026.parquet` | 9266 B | 3 行，内容**是 rb0 的价格** |
| `rb0/1d/manifest.json`、`cu0/1d/manifest.json` | 292 B | 同步被覆盖 |

全盘体检（162 个年度分区）确认污染范围**仅此 3 个 parquet**，其余 159 个完好。

### 6.5.2 根因链

1. `test_akshare_source.py` 用 `AkshareSource()` —— `save` 参数**默认为 `True`**
   （P0-3 为签名统一而加）。
2. `DataLake.save_processed` 用 `write_parquet` **整文件覆盖**（非 merge），
   按 `symbol/freq/year` 分区写入。
3. `DataLake` 复用同一 root，多个品种的测试写同一路径 → **跨品种串扰**
   （`cu0/2026` 的内容与 `rb0/2026` **逐字段相同**，`a.equals(b) == True`）。

**污染内容取证的铁证**：`rb0/2020.parquet` 残留的 2 行与
`test_akshare_source.py:146` 的合成数据 `[2020-03-02, 1.0, 2.0, 0.5, 1.5, 100, 1000, 1.4]`
**逐字段吻合**。

### 6.5.3 🚨 为什么此前"零改动"检查完全失效

`data/raw/` 被 `.gitignore` **第 2 行**忽略 → `git status --short data/` **恒为 0**。
同理 `artifacts/`（第 6 行）也不在版本控制内。

> **方法论教训（必须记住）**：对被 gitignore 的生产目录，**`git status` 不是
> 有效的变更检查手段**。此前批次"零改动"的结论对 `data/` 目录**从未成立过**。
> 本次是靠 `find -newermt` + 逐文件内容比对才定位到污染。

**自我反省**：37 号文档此前已记下"用真实目录验证会污染生产数据"的教训，
却未举一反三安装护栏，**同类错误犯了第二遍**。

### 6.5.4 三层护栏（已落地）

| 层 | 机制 | 拦截对象 |
|---|---|---|
| ① | autouse 隔离 fixture：`DataLake.__init__` 默认 root 改道 tmp | 所有测试的隐式落盘 |
| ② | `real_data_lake` fixture：需**显式 opt-in** 才用真实目录 | 误用真实目录 |
| ③ | 兜底哨兵：拦截 `write_parquet`/`write_manifest` 写入生产目录 | 绕过①②的漏网写入 |

**实现要点**：只判 `root is None` 会漏 —— 源层常**显式传字符串默认值**
`"data/raw"`，必须同时判 `_is_protected(root) is not None`。

**实战验证**：护栏上线后，13:25 触发的自动化窗口任务跑了一整轮 pytest
（13:33–13:34，日志含 FCST 训练与 BT 回测），`find data/raw -newermt "13:00"`
**返回空** —— 护栏精确拦截。

### 6.5.5 恢复：真值重建（非外推）

**关键突破**：`artifacts/p6_4_pull_20260828_d1/{sym}.json` 存有 pandadata
**主源原始返回的逐字落盘**（`method=close_pcr`，18 品种，2026-08-29 09:40 拉取）。
主源 token 虽已失效，但 **08-28 的真值在本地**。

**证明该目录即生产口径**：`ni0` 的记录 `close=157270.1396125526` 与
`ni0` 生产分区 08-28 行**逐位相同**。

据此确立字段映射（14 列），并以**未受损的 ni0 作对照组回归验证**：

| 生产列 | 映射规则 |
|---|---|
| `open/high/low/close` | 直取（已是 `close_pcr` 后复权） |
| `volume/amount/open_interest` | 直取 |
| `raw_close = adj_close` | 均取 `close` |
| `limit_up/limit_down` | `bool(high>=limit_up)` / `bool(low<=limit_down)` |
| `is_rollover` | 由 adj/raw 比值是否恒定判定 |

**对照组结果：`PASS`，14 列 0 处差异**。

恢复结果（155 行备份 + 08-28 真值 = 156 行）：

| 品种 | 08-28 close | graft 外推值 | 偏差 |
|---|---|---|---|
| cu0 | `160719.20722501545` | `160719.20722501545` | **0.0000 bp** |
| rb0 | `3738.590674917303` | `3738.590674917303` | **0.0000 bp** |

恢复后全盘复检：18 品种 2026 年度**全部 156 行**（2026-01-05~2026-08-28）、
schema 全对、串扰解除；manifest 同步为 `n_rows=156`。

#### 🎁 意外收获：续接器拿到一次真实的端到端验证

`graft_adjusted` 的外推值与主源真值 **0.00 bp 完全一致** —— 而且是
**在 cu0 带 2 条告警、`breaks=3` 的不利条件下**达成的。

> **结论修正**：续接器的告警是**保守提示**而非精度判决。只要 anchor 落在
> 最后一个**稳定段**（cu0 在 08-21~08-27 五日内比值恒定 `1.475842`），
> 外推即可精确。此前 §4.8 记录的「cu0 21.35 bp 误差」源于 anchor 取在
> 跳变**之前**的段，属锚点选取问题，非算法缺陷。

### 6.5.6 `rb0/2020` 判定：隔离，不重建

无备份（备份链只有 2022/2024/2026），主源不可用。用**留一法**量化
"插值重建"的可行性：

| 实验对象 | 两端 k 差 | 年内突变 | 线性插值绝对均值误差 | 最大误差 |
|---|---|---|---|---|
| 2025（两端口径连续） | 524 bp | 3 处 | **53.22 bp（0.53%）** | 135.38 bp |
| Oracle 对照 | — | — | **0.00 bp** | 0.00 bp |

> **实验设计纠错**：最初用 2022 年度做实验，得误差 1269.98 bp。但后锚点
> 取自 2023-01-03，而 **2023 年度口径已断裂**（§6.5.7），`k=1.0` 是脏锚点。
> 换用口径连续的 2025 年度后误差降至 53 bp —— 说明**锚点污染的危害远大于
> 插值方法本身**。

**判定**：53 bp 水平误差 + 年内约 10 次跳变（每次可造成 1%~2% 虚假收益）
→ **不可用于回测**。

**处置**：
- 污染文件移入 `artifacts/quarantine/rb0_2020_polluted_20260829_1149.parquet`
- 原位留 `_MISSING_2020.json` 显式声明（含原因、证据、重建条件）
- ⚠️ **残留风险**：`load_processed` 用 glob，文件不存在即**静默跳过且不报错**
  —— 跨 2020 年的回测会静默缺该年度。这正是 §4.1 咽喉缺陷的另一种表现形式。

### 6.5.7 🚨 连带发现：2023 年度口径断裂（既有缺陷，非本次事故造成）

验证各年度后复权因子 k（`k = processed值 / akshare名义价`）时发现：

| 品种 | 2022末 k | 2023首 k | 2023末 k | 2024首 k | 判定 |
|---|---|---|---|---|---|
| al0 / cf0 / i0 / j0 / jm0 / p0 / sc0 / sr0 / ta0 / y0 / zn0 | — | — | — | — | ✅ 跨年连续 |
| **rb0** | 1.353219 | **1.000000** | 1.000000 | 1.346928 | ❌ **断裂** |
| **cu0** | 1.474450 | **0.999848** | 1.000000 | 1.505286 | ❌ **断裂** |
| m0 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | ⚠️ 待定 |

**rb0 2023 年度存的是未复权名义价**（首值 4063.00 == akshare 名义 4063.0），
而相邻年度是后复权价 → 跨 2023 拼接会产生约 **35% 的假跳空**。

- 该文件 mtime 为 2023-08-16，**未被本次事故触及**，属既有缺陷。
- 另发现 `hc0`/`ni0` 拉取触发 `HexDataError: 存在 OHLC 包络关系被破坏的 bar`，
  待单独排查。

> **此项需单独立项（建议 P0-10）**：影响所有跨 2023 年的回测与训练，
> 且数据"看起来完全正常"（243 行、完整、无告警），是最隐蔽的一类缺陷。

---

### 4.10 P0-9 provisional 标记与真值重建流水线（`rebuild`）

**落地模块**：`hexbroker/data/rebuild.py`（约 330 行）+ `hexbroker/data/test_rebuild.py`（22 测试）。

**契约设计**（三点关键决策）：

1. **标记用独立 sidecar `_provisional.json`，不进 manifest**。理由：manifest
   会被 `DataLake.save_processed` 在每次常规写入时整份重建，provisional
   状态挂在那里会被冲掉；sidecar 生命周期独立、可幂等合并日期、损坏时
   重建（标记丢失方向是保守的——未标记即当真值用，挂标操作方负责及时挂）。
2. **重建 = 逐行替换 + 追加，只清被真值覆盖的日期**。真值未覆盖到的
   临时日期**保留挂标**——绝不静默把临时值转正（专项回归测试
   `test_no_overlap_keeps_all_marks`：真值一行没对上时全标记保留、分区不动）。
   这是对"零告警也有 21.35 bp 误差"教训的直接落地：**宁可长期挂标，不可静默转正**。
3. **真值来源注入（`truth_fetcher`），模块不绑定任何数据源**；拉取异常逐对象
   隔离（单点失败不中断整批，进 `report.skipped`）；schema 列集合不一致直接
   SKIPPED（宁可跳过，不可部分写入）。

**API 一览**：

```
mark_provisional(root, layer, symbol, freq, year, dates, *, method, anchored_at, reason)
clear_provisional(root, layer, symbol, freq, year=None, dates=None) -> bool
scan_rebuild_needed(root, layer="processed") -> list[RebuildNeeded]
    # 同时扫 _provisional.json 与 _MISSING_*.json（后者含 2026-08-29 事故的
    # rb0/2020 隔离标记），同 (symbol,freq,year) 双标记合并为一条
rebuild_partition(root, item, truth) -> RebuildResult   # replaced/appended/uncovered
rebuild_pipeline(root, truth_fetcher, *, layer, symbols=None) -> RebuildReport
```

**与 P0-12 的衔接**：`_MISSING_*.json` 标记由本模块负责"重建后清除"；
但 `load_processed` 读路径对缺失年度的**告警**仍归 P0-12（本模块不改编
读行为）。

**验证**：22 测试全过（含静默转正回归门禁）；全量 **796 passed**；ruff 全过；
`data/` 零改动（护栏在线）。

### 4.11 故障切换编排器（`failover`）—— P0 链路收口

**落地模块**：`hexbroker/data/failover.py`（约 360 行）+ `hexbroker/data/test_failover.py`（14 测试）。

**三级降级拓扑**：

| 层 | 来源 | 语义 | 实现方式 |
|----|------|------|---------|
| Tier 1 | pandadata `close_pcr` | 后复权**真值**，命中即 `provisional=False` | 注入式（MCP 适配器由调用方传入，token 可失效不绑定） |
| Tier 2 | sina+akshare（`BackupRawFetcher`） | 名义价 → `graft_adjusted` 续接 → 一律 `provisional=True` | 复用 §4.9 备源 + §4.8 续接器 |
| Tier 3 | 交易所官方 | 同备源语义（名义价续接） | 注入式预留，None = 如实归因不可用 |

**差异化路由**（防"盲目重试"与"过早放弃"两个极端）：

| 主源异常 | 路由 |
|---------|------|
| `HexQuotaError` | **当日锁定主源**（`_quota_locked_date`），会话内零重试，后续调用直接归因 `QuotaLocked` |
| `HexNetworkError` | 重试**恰好一次**，再败才降级 |
| `HexEmptyDataError` / `HexStaleDataError` | 源已坏，**零重试**直接降级 |
| 其他异常 | 归因 `Other`，不重试 |
| `primary_fetcher=None` | 归因 `NotWired`，直接从备源开始 |

**🔴 红线（实现期确认的锚定前提）**：
1. **湖内无锚点（NoAnchor）→ 拒绝续接**，绝不用备源名义价冒充后复权
   （专项测试 `test_no_anchor_refuses`）。
2. **备源/Tier3 请求窗口必须向前扩展 `lookback` 天**（`_overlap_start`）：
   `graft_adjusted` 依赖 raw 与湖内 adj 的**重叠日期**确定锚点比例因子，
   按调用方 `start` 原样请求时两序列无交集，续接必然失败。这是测试
   首轮暴露的真实设计缺口（6 个失败用例同根因），不是测试夹具问题。
3. **续接成功自动 P0-9 挂标**：按年调用 `mark_provisional`（幂等），
   编排器**不落数据**，只留痕。

**逐次归因**：每次尝试记 `Attempt(tier, source, ok, kind, error)`，
`Outcome.attempts_summary()` 可直接进告警文案；`primary_error_kind`
供调用方决定告警级别。

**验证**：14 测试全过（主源成功零挂标 / Quota 当日锁定 / Network 重试一次与
两次降级 / Empty·Stale 零重试 / NotWired / 备源续接挂标与 sidecar 内容 /
NoAnchor 拒绝 / Exhausted 逐源归因 / Tier3 降级与不可用报告 / 多品种跨尺度
锚点互不串扰）；全量 **810 passed**（796 + 14）；ruff 全过；生产 `data/`
零污染（13:50 cu0/rb0 2026 分区重写经逐字段核验为 13:45 盘中自动化窗口的
正常生产写入，非测试行为）。

---


## 7. 待办（按优先级）

### P0（未完）
- [x] ~~**P0-5 后复权续接器**（§6 算法）~~ → **已完成，见 §4.8**（含默认值反转）
- [x] ~~**P0-4 备源 raw 拉取器**~~ → **已完成，见 §4.9**
- [x] ~~**P0-6 换月检测与告警**~~ → **部分完成**：前向预测路线实测不可靠（持仓量拐点
      命中率约一半），已改为 **事后对账 + `provisional` 标记**（§4.9）。
      真正的重建流水线归 P0-9。
- [x] ~~**P0-9 provisional 标记与重建流水线**~~ → **已完成，见 §4.10**
      （sidecar 挂标 + scan + 真值重建 + 只清覆盖日期，22 测试）
- [x] ~~**故障切换编排器**~~ → **已完成，见 §4.11**：主 → 备1 → 备2 三级降级，
      差异化路由（Quota 当日锁 / Network 重试一次 / Empty·Stale 零重试），
      NoAnchor 拒绝续接红线 + P0-9 自动挂标（14 测试，全量 810 passed）
- [ ] **🔴 P0-10 年度口径一致性审计**（§6.5.7）：rb0/cu0 的 2023 分区为未复权
      名义价，跨年拼接产生约 35% 假跳空。需全品种全年度扫描 + 修复或标记。
- [ ] **P0-11 `rb0/2020` 重建**：待 pandadata 恢复授权后用
      `get_future_daily_post(method=close_pcr)` 重拉 2020 全年，替换隔离区文件。
- [ ] **P0-12 缺失年度显式化**：`load_processed` 用 glob 会静默跳过缺失年度，
      需改为读 `_MISSING_*.json` 标记并告警（§6.5.6 残留风险）。
- [ ] **P0-13 `hc0`/`ni0` OHLC 包络校验失败排查**：
      `HexDataError: 存在 OHLC 包络关系被破坏的 bar`。
- [ ] **P1-b 生产 manifest 补全**：全库仅 cu0/rb0 两个品种有 manifest，
      其余 16 个从未生成 —— manifest 机制形同虚设。

### 🔴 新发现的风险（需处置）
- **pandadata MCP 连接器 token 已失效**（2026-08-28 19:50 实测
  `Authentication required`）。主源当前**不可用** —— 这使备源链路与续接器
  从"备用"变为"刚需"。建议：① 尽快恢复连接器授权；② 在此之前
  `--trading-day` 体检（§4.7）是唯一能拦住"静默用旧数据"的闸门。

### P1
- [ ] SHFE / INE 官方源路径修正（`/data/tradedata/future/dailydata/`）
- [ ] CZCE `.txt` 官方源接入（已验证 24 合约）
- [x] ~~`p6_4_apply_persisted_dir.py --trading-day` 语义缺口~~ → **已完成，见 §4.7**
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
| `scripts/p6_4_apply_persisted_dir.py` | `--trading-day` 四态判定 + `--asof/--stale-days`（§4.7） |
| `tests/test_p6_4_apply_gate.py` | 🆕 17 测试（§4.7） |
| `hexbroker/data/graft.py` | 🆕 P0-5 后复权续接器（§4.8） |
| `hexbroker/data/test_graft.py` | 🆕 25 测试（含真实事故模式固化用例） |
| `scripts/dev_probe_37_graft_truth.py` | 🆕 真实数据端到端反证 + 三策略消融（§4.8） |
| `hexbroker/data/backup.py` | 🆕 P0-4 备源 raw 拉取器（§4.9） |
| `hexbroker/data/test_backup.py` | 🆕 19 测试（降级路由 / 陈旧判定 / 交叉校验） |
| `hexbroker/__init__.py` | 补齐 `HexQuotaError`/`HexNetworkError` 的 `source`/`symbol` 参数 |
| `conftest.py` | 🆕 三层护栏（§6.5.4）：isolated lake / opt-in real lake / 生产写哨兵 |
| `hexbroker/data/rebuild.py` | 🆕 P0-9 provisional 标记与真值重建流水线（§4.10） |
| `hexbroker/data/test_rebuild.py` | 🆕 22 测试（含静默转正回归门禁） |
| `hexbroker/data/failover.py` | 🆕 三级故障切换编排器（§4.11） |
| `hexbroker/data/test_failover.py` | 🆕 14 测试（差异化路由 / NoAnchor 红线 / 跨尺度锚点） |
| `scripts/dev_restore_polluted_2026.py` | 🆕 污染分区恢复工具（真值直取 + ni0 对照回归门禁） |
| `scripts/dev_probe_year_rebuild_error.py` | 🆕 年度重建误差留一法评估（插值证伪） |

**验证**：**810 passed**（原 677 → 713 → 730 → 751 → 752 → 774 → 796 → 810）；改动文件 ruff 全通过；
真实联网 18/18 双源末日 2026-08-28；备源端到端实测（sina→graft）误差 -21.35 bp 且已标记
provisional；`git fsck --no-dangling` 无输出；`data/` 零改动。
