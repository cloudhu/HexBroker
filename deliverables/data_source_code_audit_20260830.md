# HexBroker 数据源代码审计报告

**审计日期**：2026-08-30
**审计范围**：`hexbroker/data/**`（数据源抽象层、8 个数据源实现、容错与治理链）
**代码规模**：`hexbroker/data/` 5108 行 + `hexbroker/data/sources/` 2269 行
**审计方法**：代码通读 + 真实数据湖取证（18 品种 × 9 年度）+ 门禁实证复现
**测试基线**：`hexbroker/data` 180 项全绿（177 passed / 1 skipped）

---

## TL;DR

| 判定 | 结论 |
|---|---|
| **整体** | 🟡 **代码工程质量高，但存在一个已发生的数据缺失事故 + 一个门禁失效组合** |
| **最严重** | 🔴 `ag0`/`au0`/`m0` 三个品种 **2024 年缺失 7/18–12/31 共约 112 个交易日**，且**现有全部门禁均未能检出** |
| **门禁失效根因** | 用"单日涨跌停幅度 10%"作为年度边界跳变阈值，**只适合检出短时破裂，检不出半年静默缺失**（实测跳变 6.27%~9.24%，全部低于阈值） |
| **次高危** | 🟠 数据湖 root 约定分裂（`data/processed` 空 / `data/raw/processed` 有数据）+ `save_processed` 按年整区覆盖写 —— 组合起来是"引信未点燃"的截断事故 |
| **缺陷合计** | P0 ×2 ｜ P1 ×8 ｜ P2 ×9 |

**主理人裁决建议**：P0-1/P0-2 立即立项修复（数据已受损）；P1-1/P1-2 优先于所有新功能（架构性引信）；其余按 P1→P2 顺序排期。

---

## 一、P0 —— 阻断级（数据已受损 / 门禁失效）

### 🔴 P0-1：ag0 / au0 / m0 三品种 2024 年数据尾部截断

**现象**：三个品种的 2024 年度分区只有约 130 行，而其他 15 个品种为 242 行。

| 品种 | 2024 行数 | 实际区间 | 缺失交易日 | 对照（其他 15 品种） |
|---|---|---|---|---|
| ag0 | 131 | 2024-01-02 → **2024-07-17** | ~112 | 242 |
| au0 | 131 | 2024-01-02 → **2024-07-17** | ~112 | 242 |
| m0 | 130 | 2024-01-02 → **2024-07-17** | ~113 | 242 |

**取证命令**：全湖 18 品种 × 9 年度首尾覆盖度扫描，全库**仅此 3 处**异常。
（注：中间空档扫描只捞出 72 处春节/国庆 11 天假期，属正常；本缺陷是**尾部截断**形态，diff 检测天然看不见。）

**三重影响**：
1. 2024 下半年样本在训练/回测中静默缺失，样本量腰斩；
2. 2024→2025 年度边界实际跨越 169 天，边界跳变检测输入失真；
3. 数据被静默拼接 —— 无任何 `_MISSING` 标记、无 sidecar、无告警。

**为什么门禁没抓到**：
- `store.quality_notes`（`store.py:78-131`）只检查**年度文件是否存在**（`present` 来自 `glob("*.parquet")`），`2024.parquet` 存在即判干净，**从不检查年度内日期覆盖度**；
- `caliber.boundary_fake_gaps` 阈值失效 —— 见 P0-2。

**处置**：
1. 立即回填 `ag0`/`au0`/`m0` 的 `2024-07-18 ~ 2024-12-31`（可复用 `scripts/p11_truth_rebuild.py` 或主源）；
2. `quality_notes` 增加**年度首尾覆盖度**检查（首日应 ≤1/10、末日应 ≥12/20，首尾年份豁免）；
3. 回填前先对现有分区做快照备份。

---

### 🔴 P0-2：`boundary_fake_gaps` 阈值对"长时间静默缺失"完全失效

**实证数据**（取自真实数据湖 `close` ≡ `adj_close` 后复权列）：

| 品种 | 2024 末日期 | 2024 末值 | 2025 初值 | 跳变 | 是否被 10% 阈值检出 |
|---|---|---|---|---|---|
| ag0 | 2024-07-17 | 8131.00 | 7586.66 | **-6.69%** | ❌ 漏检 |
| au0 | 2024-07-17 | 581.56 | 618.04 | **+6.27%** | ❌ 漏检 |
| m0 | 2024-07-17 | 3111.00 | 2823.67 | **-9.24%** | ❌ 漏检 |
| rb0（正常对照） | 2024-12-31 | 4236.98 | 4233.14 | -0.09% | — |
| cu0（正常对照） | 2024-12-31 | 109464.87 | 108737.78 | -0.66% | — |
| i0（正常对照） | 2024-12-31 | 5900.31 | 5923.03 | +0.39% | — |
| ag0 2023→2024（正常对照） | 2023-12-29 | 5976.00 | 6036.00 | +1.00% | — |

**根因**：`caliber.py:25` 的 `DEFAULT_MAX_JUMP = 0.10` 取自"期货单日涨跌停幅度 4%~12%"。
该量纲假设失败模式是"短时剧烈口径破裂"，但真实失效模式是"**长时间静默缺失**"——半年价格漂移通常落在 6%~9%，天然低于单日涨跌停幅度。

**结论**：缺失品种的跳变比正常值（0.1%~1.1%）高约一个数量级，**信号极强，却卡在阈值之下全部漏检**。

**处置（建议新增零歧义检测）**：
```python
def boundary_calendar_gaps(adj_by_year, max_calendar_gap_days=5):
    """年度边界日历跨度检测。正常跨年 ≤5 日历日（元旦假期）。
    缺失半年 → 跨度 169 天，确定性命中，零阈值歧义、零误报。"""
```
该检测零外部依赖，可直接进 CI。

---

## 二、P1 —— 高危（故障场景下放大）

### 🟠 P1-1：数据湖 root 约定分裂

| 调用点 | root 实参 | 实际落盘路径 | 现状 |
|---|---|---|---|
| `DataLake.__init__` 默认 | `"data"` | `data/processed/` | **0 品种（空）** |
| `SinaSource` / `AkshareSource` / `PytdxSource` / `CzceSource` 默认 | `"data/raw"` | `data/raw/processed/` | **18 品种（全部数据）** |
| `scripts/fetch_data.py:57` | `"data"` | `data/processed/` | — |
| `scripts/dev_restore_polluted_2026.py:259` | `ROOT/"data"/"raw"` | `data/raw/processed/` | — |

**影响**：任何按 `DataLake()`（默认 `root="data"`）读取的组件都会读到空湖。
最危险链路：`failover._anchor(root="data")` → `load_processed` 抛 `FileNotFoundError` → 返回 `None` → 判 `NoAnchor` → **备源在主源故障的紧急时刻被拒绝续接**，故障切换静默失效。

**处置**：统一为单点常量（建议 `configs/base.yaml` 的 `data_root`），并在 `DataLake.__init__` 中校验目录非空（空则告警）。

### 🟠 P1-2：`save_processed` 按年整区覆盖写，窄窗口调用即截断整年

**证据**：`store.py:65-70` 按 `(symbol, year)` 整文件 `write_parquet` 覆盖，**不做 merge**；
`base.py:76` 的 `_clip_range` 会先把结果裁到 `[start, end]`。

**触发路径**：任何 `fetch_bars(start, end)` 窄窗口 + `save=True`（**四个源默认值均为 True**）→
该年已有数据被整年替换为窗口内的几行。

**示例**：`CzceSource.fetch_bars(symbols, "2026-08-28", "2026-08-30")` → `2026.parquet` 从 156 行变为 3 行。

**现状**：因 P1-1 的 root 分裂，当前落到 `data/raw/processed`，未直接污染主湖 —— 属**引信未点燃**。

**处置**：改为 merge-on-`(symbol, datetime)` 语义，或 save 前强制校验窗口覆盖整年。

### 🟠 P1-3：Tier3（交易所官方）续接后未挂 provisional，且 graft 异常未捕获

**证据**：`failover.py`
- Tier2（备源）：第 261-270 行有 `try/except HexDataError`；第 284-293 行调用 `mark_provisional` ✅
- Tier3（官方）：第 346-357 行 `graft_adjusted` **裸调无 try/except**，且**完全没有** `mark_provisional` ❌

**双重影响**：
1. 违反模块 docstring 第 20-21 行自述红线"续接段**一律 provisional**"；
2. graft 抛错时异常冒泡出 `fetch_adjusted`，**整批品种（含已成功者）的 Outcome 全部丢失**。

**处置**：Tier3 对齐 Tier2 的 try/except + `mark_provisional`。

### 🟠 P1-4：freshness 历史回填豁免连"覆盖性"一起豁免

**实证复现**（源停更于 2026-06-01，`today=2026-08-30`，`max_stale_days=5`）：

| 请求 end | 结果 |
|---|---|
| 2026-08-28 | ✅ 拦截 `HexStaleDataError` |
| 2026-08-25 | ✅ 拦截 |
| **2026-08-24** | ❌ **放行 —— 数据缺 2 个月却报"成功"** |
| 2026-08-23 | ❌ **放行** |

**根因**：`freshness.py:110` `if end_d < ref - timedelta(days=max_stale_days): return latest`。
本意是豁免**新鲜度**判定，却连带跳过了"**latest 是否覆盖 end**"的覆盖性检查。

**讽刺之处**：这正是该模块 docstring 第 7 行明列要防的事故模式（"数据源停更"）。

**处置**：豁免新鲜度但保留覆盖性检查 —— 无论是否豁免，均应校验 `latest >= end - max_stale_days`。

### 🟠 P1-5：交叉校验后 `break`，唯一真冗余源 czce 永不执行

**证据**：`backup.py:206-243`，`sources = ("sina", "akshare", "czce")`：
- 第 1 个源成功 → `continue`（继续拉下一源）
- 第 2 个源成功 → 交叉校验后 **`break`** → **第 3 个源永不执行**

**矛盾点**：该模块 docstring 第 13-16 行自述 ——
> 新浪新端点与 akshare 是同一上游……**二者不构成冗余**……**真正的上游冗余只能由交易所官方源提供（P1）**

即：**唯一的真冗余源（czce）在正常路径下永不参与**，而占据执行机会的交叉校验按自述"只能发现解析层不一致，无法发现上游停更"。

**处置**：交叉校验只在**同一上游组**内做（sina↔akshare）；跨上游组（czce）应继续尝试并按覆盖度选择。

### 🟠 P1-6：sina / pytdx 静默丢弃 `repair_envelope` 告警

**证据**：`schema.py:153-154` 明文约定 ——
> **调用方必须上报告警**（logging 等），修复是数据变更，静默即事故。

| 数据源 | 是否上报告警 |
|---|---|
| `akshare_source.py:118-120` | ✅ `logger.warning` |
| `sina_source.py:220` | ❌ `df2, _ = repair_envelope(...)` |
| `pytdx_source.py:296` | ❌ `df2, _ = repair_envelope(...)` |

**影响**：数据被实质修改（四价极值重定 low/high、丢弃废 bar）却无任何留痕。

**处置**：三源统一上报告警，并建议把 notes 透传到 `BarFrame.metadata`。

### 🟠 P1-7：pytdx 每品种泄漏一个 TCP 连接

**证据**：`_fetch_continuous`（`pytdx_source.py:271-286`）调用链：
1. `_select_main_contract()` → `_connect()` 建 api1，`self._api = api1`
2. `_fetch_contract_bars()` → `_connect()` 建 api2，`self._api = api2`（**api1 被覆盖，从未 disconnect**）

`fetch_bars` 的 `finally` 只 disconnect `self._api`（即最后一个）。

**影响**：18 品种批量 → 泄漏 18 个连接；公共资源服务器可能触发限流。

**处置**：`_connect` 复用已有 `self._api`，或在新建前先 disconnect。

### 🟠 P1-8：pytdx 接口语义与其他源不一致（只取最近 N 根）

**证据**：`pytdx_source.py:360-361` `count = 1200 if freq == "1d" else 2000`；
sina / akshare 拉全历史后再由 `_clip_range` 裁剪。

**影响**：请求 `start=2015` 时 pytdx 只能取到最近 ~4.8 年 → 裁剪后 0 行 → `HexEmptyDataError`。
**历史回填任务在 pytdx 源上必然失败**，且失败原因（"空结果"）具有误导性。

**处置**：按 `start` 动态计算 `count`，或在 `fetch_bars` 开头校验 `start` 是否在 count 覆盖范围内并给出明确错误。

---

## 三、P2 —— 治理与一致性

| ID | 缺陷 | 证据 | 影响 |
|---|---|---|---|
| P2-1 | `health_check` 只 `try import`，不探测连通性 | base/sina/pytdx/akshare/czce 五个源全部如此 | 名为健康检查实为依赖检查；编排器据此判"源可用"会误判（新浪服务器宕机仍返回 True） |
| P2-2 | `amount` 列 18/18 品种 **100% 为零** | sina/akshare 不提供 → 补 0.0；czce 提供但主链路不走它 | schema 把 `amount` 列为 `REQUIRED_COLS` 却恒零，属契约欺骗。当前 feature/factor 层未引用，**暂无下游影响** |
| P2-3 | `akshare_source.py:109` 用 `set` 求交决定列顺序 | `list(set(...) & set(...))` | 列序不确定 → 落 Parquet 后 schema 不稳定 |
| P2-4 | `czce_source.py:168` `sort_values` 未指定稳定排序 | 默认 `kind='quicksort'` | 并列 OI 时主力选择不确定，与"并列取第一个，保证确定性"的注释矛盾 |
| P2-5 | `czce_source.py:220` 逐日 HTTP 请求，无缓存 | `pd.bdate_range` × 每次下全表 37KB 取 1 行 | 回填 5 年 ≈ 1300 次请求/品种 × 0.3s ≈ 6.5 分钟，18 品种约 2 小时 |
| P2-6 | `caliber.py:4-5` 注释过时 | 称"processed 湖内 `raw_close` 恒等于 `adj_close`"；实测 rb0 `raw_close`=4047 vs `adj_close`=5451.02（k≈1.347） | 文档与现状不符（p37 回填后已修复），易误导后续判定 |
| P2-7 | 交叉校验告警黑洞 | `RawPull.warnings` 在 `failover._try_backup:251` 被丢弃（只用 `.close`） | `_cross_check` 产生的告警无人消费，静默 |
| P2-8 | 测试全绿但无真实湖完整性基线 | 180 项测试覆盖代码逻辑，无一条校验年度覆盖度 / 边界日历跨度 | 本次 P0-1 缺失在测试全绿下潜伏 |
| P2-9 | czce 主力规则与 sina/pytdx 完全不同却共用 symbol key | 三者都写 `cf0`：czce=逐日 OI 最大；pytdx=当前主力全部历史；sina=新浪自有连续规则 | 同 key 不同语义，切换源时数值不可比 |

---

## 四、做得好的部分（不应在重构中丢失）

审计不应只挑毛病。以下设计经实证检验是**正确且高质量**的，重构时需保留：

1. ✅ **`validate_bars` 默认拒绝 0 行**（`schema.py:82-88`）—— 直击 2026-08-28 停摆事故根因，注释完整记录了故障链路。
2. ✅ **重复索引按完整 `(symbol, datetime)` 判定**（`schema.py:95`）—— 正确区分了"多品种共享日期"与"真重复"。
3. ✅ **`graft.py` 的重叠区校验位置判断**（docstring 第 36-47 行）—— 明确指出"外推后校验是恒真式，只有锚点前重叠区才有意义"，这是难得的方法论自觉。
4. ✅ **换月近似价差消融后主动放弃**（`graft.py:249-256`）—— 有实测证据支撑（18/18 品种不优于不调整），宁可告警也不静默套用近似。
5. ✅ **`failover` 差异化路由**（Quota 锁定 / Network 重试一次 / Empty+Stale 直接降级）与 `Attempt` 全量归因结构。
6. ✅ **`repair_envelope` 三源收口**（`schema.py:134-178`）—— 消除重复实现，语义对齐有逐位验证。
7. ✅ **`store.quality_notes` 对"无标记洞"的危险分级**（`store.py:119-122`）—— 已意识到"零留痕静默拼接最危险"，只是检查粒度不够（见 P0-1）。
8. ✅ **数据源 docstring 中的实测取证记录**（如 sina 旧端点冻结于 2024-07-17、akshare 中文列名导致从未跑通）—— 质量很高，是本次审计能快速定位问题的基础。

---

## 五、缺陷汇总表

| ID | 优先级 | 缺陷 | 处置要点 | 状态 |
|---|---|---|---|---|
| P0-1 | 🔴 P0 | ag0/au0/m0 2024 年缺失约 112 交易日 | 回填 + `quality_notes` 加覆盖度检查 | ⛔ 待立项 |
| P0-2 | 🔴 P0 | `boundary_fake_gaps` 阈值检不出长时间缺失 | 新增 `boundary_calendar_gaps`（日历跨度） | ⛔ 待立项 |
| P1-1 | 🟠 P1 | 数据湖 root 约定分裂（空湖 / 真湖） | 统一 `data_root` 单点，构造时校验 | ⛔ 待修 |
| P1-2 | 🟠 P1 | `save_processed` 按年覆盖写，窄窗口即截断 | 改 merge 语义 | ⛔ 待修 |
| P1-3 | 🟠 P1 | Tier3 缺 provisional + graft 异常冒泡 | 对齐 Tier2 | ⛔ 待修 |
| P1-4 | 🟠 P1 | freshness 豁免连带豁免覆盖性 | 保留覆盖性检查 | ⛔ 待修 |
| P1-5 | 🟠 P1 | 交叉校验 break 导致 czce 永不执行 | 按上游分组 | ⛔ 待修 |
| P1-6 | 🟠 P1 | sina/pytdx 静默丢弃修复告警 | 三源统一上报 | ⛔ 待修 |
| P1-7 | 🟠 P1 | pytdx 每品种泄漏一个连接 | `_connect` 复用/先断 | ⛔ 待修 |
| P1-8 | 🟠 P1 | pytdx 只取最近 N 根，语义不一致 | 动态 count / 前置校验 | ⛔ 待修 |
| P2-1 ~ P2-9 | 🟡 P2 | 见上表 | 按排期 | ⛔ 待排 |

---

## 六、文件清单

**本次审计覆盖文件**：

抽象层：`hexbroker/data/base.py`、`schema.py`、`caliber.py`、`store.py`、`freshness.py`、`manifest.py`、`graft.py`、`backup.py`、`failover.py`
数据源：`sources/{__init__,sina,akshare,pytdx,czce,csv,synthetic,tqsdk,qlib}_source.py`
取证脚本：临时内联 Python（未落盘）
取证数据：`data/raw/processed/{18 品种}/1d/{2018..2026}.parquet`

**本报告**：`deliverables/data_source_code_audit_20260830.md`

---

## 七、建议的下一步

1. **立即**：确认 `ag0`/`au0`/`m0` 的 2024 下半年缺失是否为已知问题（若是，补 `_MISSING` 标记留痕；若否，按 P0 立项回填）。
2. **本周**：修 P1-1 + P1-2（架构性引信，优先于所有新功能）。
3. **排期**：P0-2 的 `boundary_calendar_gaps` 与 P1-4 的覆盖性检查合并为一个"数据完整性门禁"PR，进 CI。

**是否继续？** 如需我直接实施 P0-2 的 `boundary_calendar_gaps` + `quality_notes` 覆盖度检查（含测试），可以立即开工。
