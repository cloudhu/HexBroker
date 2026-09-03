# P0-A/B 回滚与融合护栏 —— QA fresh-eyes 独立复核报告

- 复核人：严过关（Yan，QA）
- 复核对象：`66bd61a` fix(p0-adj) / `5cb1691` test(p0-adj) / `1887c2b`+`89381c8` docs(p0-adj)
- 日期：2026-09-03（夜盘 20:30 融合前）
- 复核纪律：`scripts/` **源码只读**（变异全部还原，见 §A.3）；只增测试与报告

---

## 0. 结论速览

| 项 | 结论 | 严重度 |
|---|---|---|
| A 变异测试 | 原 23 例杀 6/9；补锁后杀 8/9；存活 1 个（M5，源码缺口，已用探针钉住） | 🟡 |
| B R22 兜底 | **满足**：`legacy` 与护栏前代码逐字等价，脏输入异常面 8/8 一致 | 🟢（附 1 条 🟡 文档不符） |
| C 后门可追溯 | `merge_guard` **确实落盘**（真实 json 18/18 命中），非 print-only | 🟢（原为单点锁，已加固） |
| D 回滚脚本 | 白名单/G5/幂等均合格；fail-closed 为"中止整轮"而非"跳过该格" | 🟢 + 2 条 🟡 |
| E `raw_close` | 隐式保护但**当前零暴露面**（162 分片 0 重复日期，回退分支不可达） | 🟡（非 🔴） |
| F 全量回归 | **tests=1284 / failures=0 / errors=0 / skipped=1** | 🟢 |

**放行判断：PASS —— 护栏可以今晚值班。**
**但有两处任务书前提需纠正**（§D.1 的"16 格已相等"、§F 的 `--basetemp` 命令本身会造 46 条假失败）。

---

## A. 变异测试（A 项，最高优先级）

### A.1 方法

- 测试台：`artifacts/_tmp/qa_yan/mutate_guard.py`
- 纪律：每次只改一处 → 跑测试 → 从 PRISTINE 还原；**锚点必须 `count(anchor) == 1`**
  （Task-1 我曾因锚点命中 docstring 制造过一次假阴性，此处强制唯一性断言）
- 判据：只认 **junitxml 的 failures+errors**，`rc` 不作判据（见 §F.3 环境陷阱）

### A.2 结果表

**第一轮（工程师原有 23 例）**

| ID | 变异 | 结果 | 被谁杀死 |
|---|---|---|---|
| M1 | 关闭 `allow_price_overwrite` 早退（后门失效） | **KILL** | `test_allow_price_overwrite_restores_legacy_behavior`、`test_stage_parse_e2e_allow_price_overwrite_writes_prices` |
| M2 | 旧日期整行改用新帧（护栏核心失效） | **KILL** | `test_guard_keeps_existing_ohlc_and_overwrites_open_interest`、`test_guard_blocks_the_actual_cu0_incident_cell`、`test_guard_is_on_by_default`、`test_stage_parse_e2e_guard_protects_existing_ohlc` |
| M3 | 丢弃新日期（`fresh` 置空） | **KILL** | `test_guard_writes_new_dates_with_all_fields`、`test_guard_preserves_schema_order_dtype_and_sorting`、`test_stage_parse_e2e_guard_protects_existing_ohlc` |
| M4 | 缺必需字段回退 → 改为抛异常（违反 R22） | **KILL** | `test_guard_missing_required_column_falls_back`、`test_guard_missing_protected_column_falls_back` |
| M5 | OI 覆盖加正值校验 `& (oi_overwrite > 0)` | **SURVIVE** | —（无任何用例覆盖） |
| M6 | 去掉 `merge_guard` 落盘字段 | **KILL** | `test_stage_parse_e2e_guard_protects_existing_ohlc`（**仅此 1 条，单点锁**） |
| M7 | 去掉 `[GUARD]` stdout 留痕 | **SURVIVE** | — |
| M8 | 保护列"全不可解析"放宽为"任一格 NaN" | **SURVIVE** | — |
| M9 | `keep="last"` → `keep="first"` | **KILL** | `test_guard_missing_required_column_falls_back`、`test_allow_price_overwrite_restores_legacy_behavior`、`test_stage_parse_e2e_allow_price_overwrite_writes_prices` |

**第二轮（我补 20 例后，合计 43 例）**：M1–M4、M6–M9 全部 KILL（M7 由新增
`test_backdoor_leaves_trace_on_stdout` 杀死；M8 由新增
`test_guard_still_applies_when_only_part_of_protected_cells_are_nan` +
`test_guard_fallback_predicate_is_all_nan_not_any_nan` 杀死；M6 从 1 条锁加固到 3 条）。
**M5 仍 SURVIVE** —— 它是源码缺口，不是测试缺口。

### A.3 存活变异点名（真实测试缺口）

**M5（🟡，源码缺口，需工程师定夺）**
`scripts/p6_4_fill_gaps.py:813` `valid = oi_overwrite.notna()`
→ 新帧 `open_interest = 0.0` 时，护栏会把主湖既有日期的 OI **静默清零**。
触发路径真实可达：`normalize_new_df` 的 `num()`（L539-542）在「缺列 / 全 NaN /
网关返回 0」三种情况下**一律填 0.0**，而 `new_oi.isna().all()`（L799）判不出 0.0。
**OI 是护栏唯一允许写的字段，而它恰恰是唯一没有取值校验的字段。**
注：这不是本次改动新引入的（legacy 行为同样会把 OI 写成 0.0），护栏严格优于
legacy，故不阻塞今晚。

建议最小修法（我未改源码）：覆盖前校验新值 > 0，不合法则**保留旧值并在 note 记一笔**；
**不要**回退到 legacy —— 那会连同价格列一起被覆盖，等于回到事故行为。

处置：我把它钉成 `tests/test_p6_4_merge_guard_gaps.py::
test_guard_does_not_zero_existing_open_interest_when_new_oi_is_zero`
的 `xfail(strict=False)` 探针。**反证已做**：施加 M5 变异后该探针由 XFAIL 转 **XPASS**
（`artifacts/_tmp/qa_yan/_m5.txt`），证明它不是装饰性空标记 —— 修好后它会以 XPASS
提醒"请删除本标记"。

**M7（🟡，测试缺口，已补锁）**、**M8（🟡，测试缺口，已补锁）**
M8 尤为危险：放宽回退判据 = 让护栏更容易回退 = **回到 cu0/ni0 事故的原始行为**，
而原 23 例对此毫无察觉。已补行为锁 + 源码红线扫描双保险。

### A.4 还原证明

```
$ git diff --stat -- scripts/
（空）
```
变异台自带 PRISTINE 比对，输出 `还原校验: OK`。

---

## B. R22 兜底路径

### B.1 关键论证：`legacy` 无条件计算是否引入新失效模式？

**判据性证据 —— `git show 66bd61a -- scripts/p6_4_fill_gaps.py` 删除的三行：**

```python
-        merged = pd.concat([old, new_year], ignore_index=True)
-        merged = coerce_schema(merged)
-        merged = (
-            merged.drop_duplicates(subset="datetime", keep="last")
-            .sort_values("datetime")
-            .reset_index(drop=True)
-        )
```

与现行 `scripts/p6_4_fill_gaps.py:754-759` 的 `legacy` 计算**逐字等价**（仅换行格式差异）。
→ 所有守卫判定之前的这次 `coerce_schema`，**就是护栏前的原有行为**，不是新增代码。

### B.2 实证：脏输入异常面对比

探针 `artifacts/_tmp/qa_yan/probe_r22_rawclose.py`（护栏版 vs 护栏前实现，同一批输入）：

| 用例 | 护栏版（当前） | 护栏前（66bd61a 前） | 结论 |
|---|---|---|---|
| 正常 | OK rows=1（+note） | OK rows=1 | 一致 |
| 垃圾字符串 datetime（旧帧） | RAISE `DateParseError` | RAISE `DateParseError` | 一致 |
| 垃圾字符串 datetime（新帧） | RAISE `DateParseError` | RAISE `DateParseError` | 一致 |
| 缺 datetime 列（旧帧） | OK rows=2（回退+归因） | OK rows=2 | 一致 |
| 缺 close 列（新帧） | OK rows=1（回退+归因） | OK rows=1 | 一致 |
| 缺 open_interest 列（新帧） | OK rows=1（回退+归因） | OK rows=1 | 一致 |
| 旧帧 datetime 含 NaT | OK rows=2（回退+归因） | OK rows=2 | 一致 |
| 旧帧 datetime 重复 | OK rows=1（回退+归因） | OK rows=1 | 一致 |

**8/8 一致 → R22 满足（🟢）：没有新增任何"原本能跑、现在崩"的失效模式。**
8 个负例确实都走 `_fallback` 且 note 带归因（`_assert_legacy` 断言
`"护栏未生效" in note` + `"退回既有 keep=last 行为" in note`）。

### B.3 🟡 附带发现：docstring 与实现不符

`merge_year_frames` docstring（L727-740）承诺「datetime 不可归一化（**异常**）→
放弃护栏、不抛异常」。实测：异常型 datetime 在 **L754 的 `legacy` 计算里就抛出**了
（`DateParseError`），位于 L777 的 `try` **之外**，护栏的 `except` 根本没机会执行。
即：**docstring 第 3 条判据的"异常"分支不可达**，只有 NaT / 重复分支可达。
因护栏前行为完全相同，不新增失效模式，不阻塞今晚；但文档需更正，否则将来有人
会误以为"脏 datetime 已被兜底"。

---

## C. 后门可追溯性（`--allow-price-overwrite`）

### C.1 落盘链路（🟢 结论：确实落盘，不是 print-only）

```
merge_year_frames() 返回 note          (L826 / L763 / L766 / L768)
  → stage_parse L933  "merge_guard": guard_note   （写入 records）
  → L944 applied.extend(records)
  → L945 save_applied(applied)
  → L670-674 APPLIED_PATH.write_text(json.dumps(...))
```

### C.2 真实产物实证

```
$ artifacts/p6_4_applied.json
记录条数: 386        含 merge_guard 的记录: 18
  ta0 20260821_20260903 2026 | 护栏生效：保护 160 个既有日期的 OHLC/adj_close，
                                仅覆盖 10 行 open_interest；新增 0 个日期整行写入
  （y0 / zn0 … 同构，18 条全部命中）
```
386 条里其余 368 条是护栏上线前的历史记录（无该字段），符合"既有键不变"的设计。
**18/18 命中 = 今晚这批真实融合留痕完整、可归因。**

### C.3 加固

- **M6 原为单点锁**（仅 `test_stage_parse_e2e_guard_protects_existing_ohlc` 一条，
  且断言是 `any(...)`）。已补 `test_backdoor_leaves_trace_in_applied_json_for_every_record`
  与 `test_guard_note_persisted_on_happy_path_for_every_record`，收紧为**逐条**断言。
- **M7（stdout 无锁）已补** `test_backdoor_leaves_trace_on_stdout`（capsys 断言
  `[GUARD]` + `护栏已显式关闭` + `--allow-price-overwrite`）。

### C.4 🟢/🟡 小项

- `save_applied`（L670-674）用 `write_text` **非原子**，不是 G5 写法。属既有实现、
  非本次改动引入；且护栏本身让"重复应用"无害（旧日期价格受保护），风险被护栏
  顺带对冲 → 记 🟢 观察项。
- 目前无人回读 `merge_guard` 字段（只写不查）。值班若需要"事后审计"，需要另配
  一个查读脚本/告警，否则落盘只是留痕能力。

---

## D. 回滚脚本 `scripts/rollback_adj_cells.py`（代码路径，非数据再核验）

### D.1 ⚠️ 前提纠正：20 格 vs "4 处改动"

任务书假设「另外 16 格是已相等」。**取证结论：不是。**
`artifacts/_tmp/rollback_adj_cells_20260903_174558.json` 的 `cells[]` 20 条，
**Δ% 全部非零**，范围 `+0.232706% … −0.372439%`，最小绝对值 `0.084973%`
（ni0 2026-08-24 `open`）。

即：**20 格 = 5 列 × 2 日期 × 2 品种，全部是被污染的格子**；"4" 指的是
(品种, 日期) **行数**（2 日期 × 2 品种）。整行覆盖事故把 `open/high/low/close/adj_close`
**一并污染**了，只回滚 `close` 是**不够**的 —— 工程师的 20 格白名单是对的，
且与他自己文档 §3「恰好 20 格」/ §17:41「确认 20 格污染」一致。
→ 判 **🟢**，但任务书的表述需要更正，否则将来同类事故会漏掉 OHLC 三列。

### D.2 白名单范围（🟢）

```
ROLLBACK_COLUMNS = ("open","high","low","close","adj_close")   L65
FROZEN_COLUMNS   = ("raw_close","volume","amount","open_interest")  L67
```
零交集 ✔；`apply_plan` 只写 `cell["column"]`（来自 ROLLBACK_COLUMNS）。
我加的 `test_apply_plan_never_touches_frozen_columns_on_any_row` 比脚本自带的
`frozen_before`（L172-197，**只查目标日期**）更严：**全分片逐行逐位**比对 4 个冻结列。

### D.3 G5 原子写 + `shutil` 导入（🟢）

- L184-186：`tmp = lake_path.with_suffix(".parquet.tmp")` → `to_parquet(tmp)` →
  `tmp.replace(lake_path)`（`Path.replace` = `os.replace`，同分区原子）✔
- **`import shutil`（L47）是活导入，不是死导入**：L93 `shutil.copy2` 用于写前备份。
  `shutil.move` **从未调用**（全文 grep 仅命中 docstring L29 的"不使用 shutil.move"）。
  文档表述准确、无矛盾。
- 行为级验证 `test_apply_plan_writes_only_tmp_then_replaces`：劫持
  `pd.DataFrame.to_parquet` 记录所有落盘路径，**断言每一条都以 `.parquet.tmp` 结尾**
  （即绝不直接写目标分片），且结束后 `.tmp` 零残留。

### D.4 红线扫描器是否有牙齿（🟢，附自检）

`test_rollback_and_guard_scripts_have_no_delete_calls`（test_p6_4_merge_guard.py:452）
确实用 `ast.parse(path.read_text(...))` 扫**源码文本**（非 shell），覆盖
`{unlink, remove, rmtree, move}`，两个脚本都扫 ✔。
但它"永远为空"的断言可能是**空断言**。我补了 `test_redline_scanner_has_teeth`：
把 `os.remove("x")` / `shutil.move("a","b")` 注入真实源码文本后，同一扫描器**必须报命中**。
实测通过 → 扫描器确有牙齿。

### D.5 幂等（🟢）

`test_apply_plan_is_idempotent_and_bit_identical`：
连续两次 `apply_plan`，第二次后 —— `pd.testing.assert_frame_equal` 通过、
**文件 sha256 逐字节相同**、第二轮计划 `Δ` 全为 0、`verify()` 残留为空。

### D.6 fail-closed（🟢 + 🟡）

- 快照缺失 → `FileNotFoundError("权威快照不存在")`（L114），**零写入**（sha256 不变）✔
- 目标日期缺失 → `KeyError("…主湖无 1999-01-04")`（L124），**零写入** ✔
- 🟡 **语义偏差**：任务书写的是"跳过该格 + 显式报错"，实现是**中止整轮**。
  两者都不静默（traceback + 非零退出），但中止整轮意味着**只要 1 格缺失，20 格
  一格都回滚不了**。止血场景下建议改为逐格跳过并汇总报错。
- 🟡 **写后校验**：`apply_plan` 先写盘（L185-186）再校验收冻列（L189-197）。
  若校验失败抛 `RuntimeError`，分片已改写，且**取证 JSON 不落盘**（第 4 步在第 3 步之后）。

### D.7 🟡 真实事故复盘里暴露的一条风险

`artifacts/_tmp/rollback_apply.txt` 显示：**第一次 `--apply` 崩过**
（`KeyError: "['index'] not in index"` @ 当时 L181），崩溃发生在写盘**之前**，
但**写前备份已完成**；修复后第二次成功（`rollback_apply2.txt`）。
现网代码已修（现 L182 `out = di.reset_index()[original_columns]`）。

风险点：`--pre-backup-dir` 默认路径**固定不带时间戳**
（`artifacts/_p2_backup_20260903_pre_rollback`），且 `backup_symbol` 同名覆盖。
若崩溃发生在"写之后、验之前"，重跑会用**已回滚的湖**覆盖改前副本 →
唯一改前副本丢失（权威快照 `_p2_backup_20260903` 仍在，影响有限）。
**建议：pre-backup 目录加时间戳，或已存在则跳过并告警。**

---

## E. `raw_close` 是否该进 `MERGE_PROTECTED_COLUMNS`

**结论：🟡，不是 🔴。**

### E.1 现状

`MERGE_PROTECTED_COLUMNS`（L109-111）= `open/high/low/close/adj_close`，**不含**
`raw_close`。但护栏靠"整行保留旧值"（L808 `merged_old = old_idx.copy()`）
**顺带**保住了名义价 —— 属**隐式**保护、无契约。
已加契约锁 `test_guard_keeps_existing_raw_close_even_though_not_in_protected_list`：
把新帧 `raw_close` 设成复权价复制品，断言旧名义价 107520.0 不被覆盖、
`k` 仍等于 `158682.55/107520`。

### E.2 暴露面实证（关键）

护栏一旦**回退**到 legacy，就是整行覆盖 → `raw_close` 会被新帧的
`raw_close = close`（adj 复制品）顶掉 → `k → 1.0` → 敞口算错（ag0 1.46×）。
所以问题等价于：**回退分支在真实管线上可达吗？**

| 回退分支 | stage_parse 实际可达性 |
|---|---|
| old/new 为空 | 否（new_df 非空才走到写回） |
| 缺必需字段 | 否（`old`/`new_year` 都经 `coerce_schema` 补齐，L905-908） |
| datetime 含 NaT | 否（`normalize_new_df` L533 `dropna(subset=["datetime"])`） |
| **old datetime 重复** | 取决于主湖 |
| OI / 保护列全不可解析 | 否（`coerce_schema` L586-587 `fillna(0.0)`） |

**真实主湖审计（`probe_r22_rawclose.py`）：扫描 162 个分片，含重复 datetime 的分片 = 0。**
→ **5 条回退分支在真实管线上全部不可达**，`raw_close` 当前**零暴露面**。

### E.3 备源通道是否覆盖已有日期的 raw_close

`scripts/refresh_pull_local.py:437` `for d in new_dates:` —— Tier-2 备源（a31299f）
**只写新日期**，不覆盖已有日期任何列（含 `raw_close`）。✔

### E.4 建议（不阻塞今晚）

把 `raw_close` 纳入保护语义（至少在 `note` 里显式声明"名义价同受保护"），
并在主湖出现重复 datetime 时能报警 —— 那是唯一会让回退分支变成真实敞口漏洞的条件。

---

## F. 全量回归

### F.1 ⚠️ 任务书指定命令会产生 46 条**假失败**

```
$ python -m pytest --basetemp=artifacts/_tmp/pytest_basetemp \
         --junitxml=artifacts/_tmp/qa_regression_20260903.xml
46 failed, 1237 passed, 1 xfailed        # EXIT=1
```
失败原因分布（junitxml 解析）：
- **42 条**：`RuntimeError: [护栏] 测试试图写入生产目录 E:\Workspace\HexBroker\artifacts
  （write_parquet）：…/artifacts/_tmp/pytest_basetemp/test_xxx/…`
  → 根因：`conftest.py:116-141` 的 `_guard_production_writes` 拦截对
  `data/` 与 `artifacts/` 的一切写；`--basetemp` 把 pytest 的 `tmp_path` 放到了
  `artifacts/` 下，于是**每个用 tmp_path 的用例都被自己的护栏拦下**。
- 2 条 `FileNotFoundError: …artifacts/_tmp/pytest_basetemp…`（清理竞态，同源）
- 2 条（`test_trade_logger`、`test_lookahead_analysis`）同源于 tmp 根落在 `artifacts/`

**`--basetemp` 必须落在仓库之外。**

### F.2 正确跑法与权威计数

```
$ T=$(mktemp -d)
$ PYTHONPATH=$PWD PYTEST_DEBUG_TEMPROOT="$T" python -m pytest \
      --junitxml=artifacts/_tmp/qa_regression_20260903.xml
1283 passed, 1 xfailed in 101.28s        # EXIT=0
```
junitxml 权威计数（解析 `testsuite` 属性）：

```
tests=1284  failures=0  errors=0  skipped=1
skipped 明细: 1 × pytest.xfail（M5 缺口探针）
```

**计数对账**：1241（改动前基线）+ 23（工程师 `5cb1691`）+ **20（我本次新增）**
= **1284** ✔ 与工程师文档预期 1264 一致（我的 20 是额外增量，非对不上）。
新增的 `test_guard_does_not_write_main_lake` **未 skip**（真实主湖存在），
即"真实主湖零写入"的红线断言实际跑过了。

### F.3 三条环境陷阱（已取证，后人勿踩）

1. **`--basetemp` 必须在仓库外**（§F.1），否则 42 条假失败。
2. **safe-delete 钩子按"本回合删除次数 > 50"拦截**，会让 pytest 在 teardown 阶段
   `rc=1` 但 junitxml 全绿 → **`rc` 不能作判据，只认 junitxml 的 failures/errors**。
3. `pytest-of-Administrator/garbage-*` 堆积（实测已 82 个）会吞掉终端汇总行。
   根治：每次跑给一个全新的 `PYTEST_DEBUG_TEMPROOT`（实测 5/5 稳定）。

---

## 遗留项汇总

| # | 严重度 | 项 | 处置 |
|---|---|---|---|
| 1 | 🟡 | M5：新帧 OI=0.0 静默清零主湖 OI（护栏唯一写通道无校验） | 探针已钉（`xfail`，修复后转 XPASS）；最小修法见 §A.3，**待工程师定夺** |
| 2 | 🟡 | 回退分支（= 事故行为）边界原无锁（M8） | **已补**行为锁 + 源码红线 |
| 3 | 🟡 | `[GUARD]` stdout 留痕原无锁（M7） | **已补** capsys 断言 |
| 4 | 🟡 | `merge_guard` 原为单点锁、且只断言 `any()` | **已补**逐条断言（现 3 条锁） |
| 5 | 🟡 | docstring 承诺"datetime 异常 → 回退不抛异常"不可达 | 文档待更正（行为与护栏前一致，不新增风险） |
| 6 | 🟡 | `raw_close` 隐式保护、无契约 | 已加契约锁；真实暴露面为 0；建议纳入保护语义 |
| 7 | 🟡 | 回滚脚本 fail-closed = 中止整轮（非跳过该格）；写后校验 | 建议改逐格跳过；不阻塞今晚 |
| 8 | 🟡 | pre-backup 目录固定、重跑会覆盖改前副本 | 建议加时间戳或已存在则跳过 |
| 9 | 🟢 | `save_applied` 非原子写 | 既有实现；护栏已对冲重复应用风险，观察 |
| 10 | 🟢 | `merge_guard` 只写不查 | 若需事后审计告警，需另配查读脚本 |

---

## G. 与工程师并行提交的同步核验

复核期间工程师又提了两个 commit，已核实**均为 docs-only**，未动 `scripts/`：

```
d1e720a docs(p0-adj): 增补 §11 名义价保护契约（raw_close 纳入受保护列，待 GO）
        deliverables/cu_ni_rollback_20260903.md | 113 +++
7ca9cb8 docs(p0-adj): §11 补 L561 量纲级直证、Option B 解耦、R8 与全湖预检证据
        deliverables/cu_ni_rollback_20260903.md | 165 +++--
```

当前 HEAD 源码复核（与我复核时的基线一致，**结论不过时**）：

```
scripts/p6_4_fill_gaps.py:109-111
MERGE_PROTECTED_COLUMNS: tuple[str, ...] = (
    "open", "high", "low", "close", "adj_close",
)                       # ← 仍不含 raw_close
```

即：**E 项在源码层面尚未落地**（工程师文档标注"待 GO"），我的 🟡 判定与契约锁
对当前 HEAD 成立。若 GO 通过、把 `raw_close` 加进 `MERGE_PROTECTED_COLUMNS`，
请注意两点：

1. `MERGE_GUARD_REQUIRED_COLUMNS`（L113-115）由 ` MERGE_PROTECTED_COLUMNS` 拼接而成，
   加列会**同时收紧回退判据** —— 任一侧缺 `raw_close` 即触发回退（= 回到事故行为）。
   必须同步确认主湖与所有数据源都必有该列，否则把"保护"变成了"更容易回退"。
2. `test_guard_column_contract` 断言 `set(MERGE_PROTECTED_COLUMNS) == {5 列}`，
   加列后该用例会变红 —— 这是**预期**的，需连同本文档 §E 一并更新。

同步核验回归（当前 HEAD，护栏三件套）：`42 passed, 1 xfailed`（xfail 即 M5 探针）。

---

## 附：本次新增文件

- `tests/test_p6_4_merge_guard_gaps.py`（13 例）—— M5 探针 + M7/M8 补锁 + C/E 项加固 + 主湖零写入红线
- `tests/test_rollback_adj_cells_contract.py`（7 例）—— D 项：白名单 / G5 / 幂等 / fail-closed / 红线扫描器自检
- `artifacts/_tmp/qa_yan/mutate_guard.py` —— 变异测试台（含三条环境陷阱注释）
- `artifacts/_tmp/qa_yan/probe_r22_rawclose.py` —— B/E 项取证探针
- `artifacts/_tmp/qa_regression_20260903.xml` —— 全量回归权威计数
