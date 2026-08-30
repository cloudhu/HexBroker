# D5 数据事故根因归因报告

- **事故**：`data/raw/processed/{ag0,au0,m0}/1d/2024.parquet` 从 243/243/242 行 → 131/131/130 行，干净前缀截断于 **2024-07-17**，丢失 112 个交易日
- **归因日期**：2026-08-30
- **调查性质**：只读取证，未修改任何代码/数据，无 git 提交
- **环境**：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`（pandas 3.0.3 + pyarrow 24.0.0）

---

## 1. 结论

**根因：可复现的脚本缺陷（三缺陷叠加），不是一次性人为操作失误。**

**肇事执行**：`scripts/refine_lightgbm_champion.py` 于 **2026-08-23 13:39:42** 的一次常规「特征重要性复核」运行。

该脚本在取数时经由 `SinaSource.fetch_bars` 的**默认落盘副作用**（`save=True`），把新浪**旧端点**（数据冻结于 2024-07-17）的陈数据，通过 `DataLake.save_processed` 的**按年整区覆盖写**（不合并、无缩水保护），一次性打回生产湖，把 ag0/au0/m0 的 2024 分区覆盖为 131/131/130 行。

### 因果链（逐环均有证据）

| # | 环节 | 证据位置 |
|---|---|---|
| 1 | `configs/data/free.yaml` 源优先级 `[sina, pytdx]`，sina 优先命中 | `configs/data/free.yaml:20` |
| 2 | 事故时点（HEAD=`ade7196`）sina 日线走**旧端点** `json.php/IndexService.getInnerFuturesDailyKLine`，该端点数据**冻结于 2024-07-17** | `git show ade7196:hexbroker/data/sources/sina_source.py` L32 |
| 3 | `SinaSource.__init__(save: bool = True)` —— **取数默认落盘** | `git show ade7196:...sina_source.py` L55；当前版 `sina_source.py:81` 仍未改 |
| 4 | `fetch_bars` 末尾 `self.lake.save_processed(bf)`，lake root 默认 `data/raw` = 生产湖 | `git show ade7196:...sina_source.py` L216；当前版 `sina_source.py:289`、`sina_source.py:95` |
| 5 | 当时 `base.py` 只有 `_clip_range`（纯区间裁剪），**无新鲜度/空结果门禁**（`freshness.py` 08-29 10:46 才引入） | `git show ade7196:hexbroker/data/base.py` L37-42 |
| 6 | 脚本请求区间 2018-01-01 ~ 2026-08-17，品种 `['ag0','au0','m0']` | `scripts/refine_lightgbm_champion.py:65-66`；日志 `recheck_stageA_20260823.log` L5 |
| 7 | sina 只返回到 2024-07-17 → `_clip_range` 到 [2018-01-01, 2026-08-17] 不改变结果 → 4772 根 | 日志 L5 `bars=4772` |
| 8 | `save_processed` 按年整区覆盖 → 写 **21 个分区**（3 品种 × 2018~2024）；2025/2026 无数据故未写 | `hexbroker/data/store.py:57-74`（L70 `write_parquet`） |
| 9 | `write_parquet` 原子写但**无行数/缩水校验** | `hexbroker/utils/io.py:38-46` |

**为什么只有 ag0/au0/m0**：因为该脚本的品种集合就是 R7 冠军组 `['ag0','au0','m0']`。其余 15 品种从未被它取数，2024 分区完好。
**为什么止于 2024-07-17**：新浪旧端点的冻结日期，与任何 hardcode 的 `end=` 参数无关。
**为什么 2025/2026 没事**：陈数据里根本没有这两年 → 无对应年份 → `save_processed` 不会创建分区。

---

## 2. 证据链

### E1（决定性）p37 备份保留的 mtime 暴露作案时刻与范围

p37 备份用 `copy2` 生成，**保留了每个分区在 2026-08-29 10:13:45 时的最后写入时刻**。

```bash
python -c "遍历 data/p37_backup_processed_20260829T101345/*/1d/*.parquet，统计 os.path.getmtime"
```

输出（节选）：

```
  21  2026-08-23 13:39:42
   2  2026-08-29 09:39:50
   ...
   1  2026-08-29 17:51:49
```

21 个分区明细（`2026-08-23 13:39:42`）：

```
ag0 2018/2019/2020/2021/2022/2023/2024
au0 2018/2019/2020/2021/2022/2023/2024
m0  2018/2019/2020/2021/2022/2023/2024
```

**精确等于 ag0/au0/m0 × 2018~2024（7 个年份 × 3 品种 = 21），且不含 2025/2026。**

`ag0/1d/2024.parquet` 备份 mtime = `2026-08-23 13:39:42`，与行数 131 → 从该时刻起至 08-29 10:13:45 备份前，**2024 分区再未被写过**。截断就发生在这一刻。

### E2（决定性）日志 bars 数与湖内行数精确吻合

`artifacts/recheck_stageA_20260823.log`（mtime `2026-08-23 13:40:38`，紧接 13:39:42）：

```
[INFO] 尝试源 'sina'（按 free.yaml 优先级）...
[OK] 数据源：sina | symbols=['ag0', 'au0', 'm0'] bars=4772
[OK] 特征：n_rows=4772 symbols=['ag0', 'au0', 'm0'] n_cols=18
```

核对当前湖（未被 08-29 两批改动的列与行数）：

```
ag0 [243, 244, 243, 245, 243, 242, 131] sum= 1591
au0 [243, 244, 243, 245, 243, 242, 131] sum= 1591
m0  [243, 243, 244, 245, 243, 242, 130] sum= 1590
TOTAL = 4772   (log bars=4772)   ✅ 精确相等
```

品种集合、年份范围、行数三者同时命中，排除巧合。

### E3 代码注释自陈端点冻结日期

`hexbroker/data/sources/sina_source.py:38-43`：

```
# ⚠️ 2026-08-28 实测更正（主理人独立取证）：
#   旧端点 ``json.php/IndexService.getInnerFuturesDailyKLine`` **已冻结于 2024-07-17**，
#   仅 6 字段（无持仓量/结算价）。本项目原先正使用此端点 —— 故障切换时会静默喂两年陈数据。
#   新端点 ``jsonp.php/.../InnerFuturesNewService.getDailyKLine`` 实测新鲜到当日
```

截断日 2024-07-17 = 旧端点冻结日，完全对上。

### E4 git 时间线：端点切换晚于事故

```bash
git log --date=iso --pretty="%h %ad %s" -- hexbroker/data/sources/sina_source.py
```

```
0a769d7 2026-08-29 16:03:11 +0800 fix(data): OHLC envelope repair unified ... (P0-13)
1fcb940 2026-08-29 10:46:30 +0800 feat(data): P0-0 取数结果门禁 + P0-1 新浪新端点 + P0-3 修复 AkShare 源
2c8a40b 2026-08-16 17:47:24 +0800 init: HexBroker 期货 AI 量化平台
```

- 事故（08-23 13:39）时 HEAD 为 `1fcb940^ = ade7196`
- `git show ade7196:hexbroker/data/sources/sina_source.py` L32 → `"1d": ("json.php", "IndexService.getInnerFuturesDailyKLine")`（**旧端点**）
- L55 `save: bool = True`；L216 `self.lake.save_processed(bf)`（**写盘副作用已在**）
- `git show 1fcb940 -- sina_source.py` diff 确认 `"1d"` 由 `json.php / IndexService...` 改为 `jsonp.php / InnerFuturesNewService...`
- `git log -- hexbroker/data/freshness.py` 仅有 `1fcb940`（08-29 10:46）→ **事故时无新鲜度门禁**

### E5 任务 C：截断指纹是 ag0/au0/m0 特有，排除全局日历/数据源问题

```bash
python -c "遍历 18 品种 2024.parquet，取 datetime.max()"
```

```
ag0  n=131  2024-01-02 .. 2024-07-17   has_after_0717=False
au0  n=131  2024-01-02 .. 2024-07-17   has_after_0717=False
m0   n=130  2024-01-02 .. 2024-07-17   has_after_0717=False
al0/cf0/cu0/hc0/i0/j0/jm0/ni0/p0/rb0/sc0/sr0/ta0/y0/zn0
     n=242  2024-01-02 .. 2024-12-31   has_after_0717=True
```

其余 15 品种 2024 全年 242 行完整；全湖 2024-07-17 之后确有 **112 个交易日**（`2024-07-18` … `2024-12-31`），与丢失行数 112 精确吻合（243−131=112，242−130=112）。

→ 排除全局日历/源级问题，确认为**特定脚本 × 特定品种集合**的局部污染。

### E6 任务 A 结果：**证伪 F4「18:13 从 p37 备份还原」假说**

字节级 hash 比对：

```
TOTAL same: 6   diff: 156   missing_in_bak: 0
```

`same=6` 恰好等于 08-29 未被重写的 6 个分区（3@08-23 13:39 + 2@08-29 17:51 + 1@08-29 17:19），`diff=156` 恰好等于 18:13（74）+ 20:01（82）。

但字节不同 ≠ 内容不同。进一步做 DataFrame 逐值比对，结论反转了 F4：

```
=== 18:13 批次 (n=74) : 差异 100% 只在 raw_close 列 ===
   ag0 2022.parquet DIFF {'raw_close': 'n=16'}
   ag0 2025.parquet DIFF {'raw_close': 'n=243'}
   al0 2018.parquet DIFF {'raw_close': 'n=243'}
   ... 74/74 全部只有 raw_close 差异，行数/日期轴/其余 13 列全同

=== 20:01 批次 (n=82) : 同样只有 raw_close ===
   ag0 2019.parquet {'raw_close': 'n=6'}
   ... 82/82
```

判别性证据（比对 `raw_close` 是否仍等于 `adj_close`）：

```
                 n     raw==adj      mtime
ag0 2025  CUR   243    0/243         08-29 18:13:49
ag0 2025  BAK   243    243/243       08-17 12:19:46
al0 2018  CUR   243    0/243         08-29 18:13:51
al0 2018  BAK   243    243/243       08-17 15:50:19
```

- **备份 = raw_close ≡ adj_close（回填前复制品状态）**
- **当前 = raw_close ≠ adj_close（已回填真名义价）**

→ 18:13 **不是**「从 p37 备份还原」。它是 `p37` raw_close 存量离线批量回填（08-29 任务①，feat `ece1bff`）的**落盘结果**；p37 备份是回填**前**的快照。备份目录时间戳 `T101345` 是备份动作时刻，与 18:13 落盘时刻不一致属两批次运行（首轮 10:13 dry-run 162/162 `BackupExhaustedError` → 修复 `warm()` 后重跑）。

**这一证伪不影响 2024 归因**：备份内 `ag0/2024.parquet` 已为 131 行且 mtime 08-23 13:39:42，说明**回填前湖里就已截断**。

### E7 排除项

- **p43**（F5）：`scripts/p43_raw_close_spike_repair.py` 对应 20:01 的 82 个分区，差异仅 raw_close、行数守恒；其 19:49 修复计划命中的 6 个 2024 日期在 131 行分区内全部存在 → 它读到时已截断，非肇事者。✅ 排除
- **p42**（F6）：只读，仅写 `artifacts/p42_reconcile.json`。✅ 排除
- **p37 回填**（任务①）：差异 100% 在 raw_close，行数全等（见 E6）。✅ 排除
- **08-29 13:50 cu0/rb0 2026 写入**：盘中自动化正常生产写入，不涉及 2024。✅ 排除
- **2024-07-17 附近的 hardcode 日期**：`scripts/build_extended_data.py` / `fetch_tdx_extension.py` / `hexbroker/data/sources/sina_source.py` / `artifacts/p36_probe_sina.json` 中的 `2024-07-17` 均为端点冻结日的事实记录或探针数据，非写入窗口参数。✅ 排除

---

## 3. 置信度

**高。**

理由：
1. **三重独立指纹同时命中**：作案时刻（08-23 13:39:42）、作案范围（21 分区 = 3 品种 × 7 年份）、作案数据量（4772 = 湖内精确行数）—— 任意一条单独成立都可能是巧合，三者同时成立的概率极低。
2. **代码路径完整闭合**：从 `configs/data/free.yaml` 源优先级 → 旧端点冻结日期在代码注释中被显式记录 → `save=True` 默认落盘 → `save_processed` 整区覆盖 → `write_parquet` 无校验，每一环都有源码/提交证据。
3. **排除性证据干净**：任务 C 证明其余 15 品种完好，与"只有该脚本取数这 3 个品种"的预测完全一致（可证伪预测被证实）。
4. **时间线自洽**：端点在 08-29 10:46 才切换，事故发生在 08-23，事故时点代码确实处于缺陷状态。

**残余不确定性（不足以推翻结论）**：
- 无 `save_processed` 的写入审计留痕（无调用栈记录），13:39:42 的归属基于「日志 mtime 13:40:38 + bars 精确匹配 + 品种集合匹配 + 年份范围匹配」的强关联推断，而非进程级直接证据。若存在另一个同样以 sina 拉 ag0/au0/m0/2018-2024 的脚本在同秒运行，理论上可混淆 —— 但已对所有调用 `SinaSource` 的脚本做过排查，无第二候选。
- 18:13 批次的准确成因（p37 重跑 vs 其他 raw_close 重写）未完全定死，属 08-29 的另一条独立线索，**与本次 2024 截断无因果**（备份内已 131 行）。

---

## 4. 判定

**「可复现的脚本缺陷」，不是一次性人为操作失误。**

三个并存缺陷，任一单独存在都可能被其他环节兜住，叠加后必然出事：

| ID | 缺陷 | 当前状态 |
|---|---|---|
| **D1** | 取数函数默认写生产湖：`SinaSource(save=True)`，root 默认 `data/raw`。研究/训练脚本一次取数 = 直接污染生产 | **❌ 未修**（`sina_source.py:81` 仍 `save: bool = True`） |
| **D2** | `save_processed` 按年整区覆盖写，无合并、无缩水保护 | **❌ 未修**（`store.py:57-74`） |
| **D3** | 取数结果无新鲜度门禁，两年陈数据静默通过 | **✅ 已修**（P0-0，`1fcb940` @ 08-29 10:46 引入 `freshness.py` + `base.py:14/25/28`；端点同时切新端点） |

**D1 + D2 至今仍在线**：只要再来一次「源端返回短数据 + 分析脚本取数」，同样的事故会在别的品种/别的年份重演。项目已为 `BackupRawFetcher` 立了 `save=False` 的红线（08-29 日志 L102：「备源只供 raw，绝不落数据湖」），但**主源没有套用同一红线**，这是红线执行的不一致。

另需指出：本次归因耗时主要花在「谁写的」上，因为 `save_processed` 不留任何写入者信息（无 source/无调用栈/无写前行数），只能靠 mtime 与日志旁证反推 —— 这本身是**可观测性缺陷**。

---

## 5. 修复建议（门禁分层）

按「拦截面 / 改动成本」排序。**L2 是必做项**，它能在不依赖调用方自律的前提下，一次性防住所有源、所有脚本的同类事故。

### L2（最关键，store 层 —— 强烈建议必做）
`DataLake.save_processed`（`hexbroker/data/store.py:57`）增加**分区缩水门禁**：

- 写入前读取目标 `{year}.parquet` 的现有行数 `n_before`
- 若 `n_after < n_before * (1 - tol)`（建议 `tol=0.02`，即允许 2% 以内的正常波动），**拒绝写入并抛 `HexDataError`**，除非调用方显式传 `allow_shrink=True`
- 同步对 `date_range` 起点做同样保护（防前缀截断）

理由：本次事故的特征就是**干净的前缀缩水**（243→131，-46%），任何比例阈值都能拦住。这一层是全湖唯一汇聚点，拦住它就等于拦住所有上游。

### L1（源层，最小改动）
把 `SinaSource` / `AkShareSource` / `PytdxSource` 的 `save` 默认值改为 `False`，与已有红线 `BackupRawFetcher(save=False)` 对齐。取数默认不落盘，落盘必须由调度/回填类脚本显式 opt-in。
重点：所有研究/分析类脚本（`refine_*`、`sentinel_*`、`p2x_*` 探针、`scripts/dev_*`）构造 `SinaSource()` 时显式传 `save=False`。

### L3（流水线/CI 层）
建立**全湖分区行数基线快照**：18 品种 × 9 年度的行数与 `date_range` 落 `data/raw/processed/_ROWCOUNT_BASELINE.json`，CI 与每次交付前跑比对脚本，**任何分区行数回退即红牌**。可复用 `scripts/p6_4_backfill_guard.py` 的判定器骨架。

### L4（可观测层）
`save_processed` 写入时落一行 audit log（sidecar 或 append-only JSONL），含：时间戳、`source`、`symbols`、年份、写前/写后行数、`allow_shrink` 标志、调用栈栈顶 3 帧。
本次归因之所以需要绕道 mtime + 日志旁证，正是因为没有这层留痕。

### L5（已具备，仅需确认覆盖）
`hexbroker/data/freshness.py` 的新鲜度门禁（`base.py:14/25/28`，`check_freshness: bool = True`）请确认对所有 `DataSource` 子类默认开启，且 `max_stale_days` 阈值能覆盖「两年陈数据」这种量级（当前默认 `DEFAULT_MAX_STALE_DAYS` 建议核一下是否足够小）。

### 数据修复（与门禁正交，需主理人拍板）
ag0/au0/m0 的 2024 分区缺失 112 个交易日，需按既有真值重建路径补回：
- 首选：`scripts/p11_truth_rebuild.py` 整年替换（有前置覆盖预检 + 边界连续性红牌，08-29 已在 rb0/2020 与 cu0/rb0/2023 验证过）
- 前置条件：pandadata 连接器授权可用
- 参考先例：08-29 日志 L226（rb0/2020，243 行整年重建 + 零污染 QA）

---

## 附录：本次调查执行的关键命令

```bash
# 任务 A：p37 备份 vs 当前湖 字节 hash
python -c "sha256 遍历 data/raw/processed/*/1d/*.parquet vs data/p37_backup_processed_20260829T101345/*/1d/*.parquet"
# → same=6 diff=156

# 任务 A 深化：DataFrame 逐列值比对（区分「字节不同」与「内容不同」）
python -c "np.isclose 逐列比对，跳过 str/datetime，bool 单独处理"
# → 156 个文件差异 100% 只在 raw_close 列

# 决定性：备份内 mtime 分布（copy2 保留了原始写入时刻）
python -c "Counter(os.path.getmtime(...))"
# → 21 个 @ 2026-08-23 13:39:42 = ag0/au0/m0 × 2018-2024

# 任务 B：git 时间线
git log --date=iso --pretty="%h %ad %s" -- hexbroker/data/sources/sina_source.py
git show ade7196:hexbroker/data/sources/sina_source.py | grep -n "1d\|save\|save_processed"
git show ade7196:hexbroker/data/base.py | sed -n '37,42p'
git log --date=iso --pretty="%h %ad %s" -- hexbroker/data/freshness.py

# 任务 C：截断指纹
python -c "遍历 18 品种 2024.parquet 取 datetime.max()"
# → 仅 ag0/au0/m0 止于 2024-07-17；全湖 07-17 后有 112 个交易日
```
