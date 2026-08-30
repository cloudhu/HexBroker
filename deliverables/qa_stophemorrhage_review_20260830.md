# P0 止血三改动 fresh-eyes 独立复核报告（qa-hemorrhage-review，2026-08-30）

## 总判定：PASS

三项止血改动（D2 缩水门禁 / D1 三源 save 默认 False / p43 G4 冻结）代码正确、实跑复现全部通过、
生产湖在复核全程零污染。另发现 4 条非阻断问题（含 1 条 D1 漏改），报主理人裁决。

## TL;DR 表

| # | 复核项 | 判定 | 关键证据 |
|---|--------|------|----------|
| A1 | D2 先查后写 | PASS | store.py:162→165-172(raise)→176(write)，异常先于任何写盘 |
| A2 | D2 误伤排查（11 个调用点） | PASS | rebuild.py:314 merge ⊇ 原分区；无合法缩水未放行的调用点 |
| A3 | D1 影响面（主理人清单验证） | PASS* | 依赖默认点全部安全；*发现 czce_source.py:79 漏改（遗留，无活跃触发点） |
| A4 | p43 blocked 零写入 | PASS | touched 仅由 repairs 构建（p43:253-256）；deny 清空后自动检测仍拦截 |
| A5 | p43 sidecar 全量留痕 | PASS | 10 字段齐全、顶层 list、write_json 原子（io.py:68-76） |
| B6 | 261→5 事故独立复现 | PASS | 抛 PartitionShrinkError 且磁盘仍 261；allow_shrink 后 5；边界 −2.29% 拦 / −1.92% 放 |
| B7 | p43 dry-run（生产湖，零写盘） | PASS | [BLOCKED] ag0, au0, m0；--deny-symbols "" 自动检测 73.87/69.65/74.51% |
| B8 | p43 apply 等价性（临时小湖） | PASS | write_parquet≡to_parquet；blocked 品种逐值不变；sidecar 字段齐 |
| B9 | 全量测试复跑 | PASS | 214 collected / 213 passed / 1 skipped（基线一致）；p37 5F=omegaconf 缺依赖 |
| C10 | 生产湖完好性哨兵 | PASS | 复核前后 162 文件 mtime/size/行数零变化 |

## 逐项判定与取证

### A1. D2 先检查后写盘 — PASS
`hexbroker/data/store.py`：`save_processed` 循环内每年先 `_partition_row_count(path)`（:162，
pyarrow metadata 快路径 + read_parquet 回退）→ 判定 `n_after < n_before×(1−0.02)` 且未传
`allow_shrink` 则 `raise PartitionShrinkError`（:165-172）→ 之后才 `write_parquet`（:176）。
异常携带 symbol/freq/year/n_before/n_after/tolerance 六字段（:67-82）。manifest 仅在循环全部
成功后写（:178-181），中途抛错 manifest 不更新。**唯一**部分写入路径 = 多年循环中前几年已落盘、
后一年抛错（已知已接受，另立排期）；除此之外无先写后查路径。`n_before` 为 None（分区不存在）或
0 时跳过检查，不误判首写（B6 STEP5 实测）。

### A2. D2 误伤排查 — PASS
全仓 `save_processed` 调用点 11 处逐一判定：
- `hexbroker/data/rebuild.py:314`：merge 分支 `out = concat([merged, new_dates])` ⊇ 原分区
  （rebuild.py:285-286），不缩水；missing 分支 `rows_before=0` 且分区不存在 → `_partition_row_count`
  返回 None 跳过检查。
- 三源内部（sina_source.py:290 / akshare_source.py:145 / pytdx_source.py:390 / czce_source.py:251）：
  仅在 `do_save=True` 时触发；D1 后默认 False，显式 save=True 属知情写入，被门禁保护是正确行为。
- `scripts/fetch_data.py:58`：cfg 全窗口重建 + 清洗后显式落盘，正常用法不缩水。
- `scripts/dev_restore_polluted_2026.py:259`：恢复工具，显式落盘意图，如遇缩水被拦属预期保护。
- 测试 fixture（test_rebuild.py:38 / test_failover.py:39 / tests/test_data_manifest.py）均写临时湖。
无任何「可能合法缩水而未传 allow_shrink=True」的调用点。

### A3. D1 影响面 — PASS（附 1 条遗留）
主理人清单独立验证成立：
- 依赖默认实例化点：refine_lightgbm_champion.py:142/144、validate_oos_r7.py:135/137、
  validate_oos_r7_auagm.py:130/132、compare_data_sources.py:51/53、validate_sources.py:130/131、
  p36_smoke_sources.py:57/64、fetch_data.py:47 —— 全部为研究/对比/校验类，或带显式最终
  save_processed（fetch_data.py:58）。
- per-call `fetch_bars(save=…)` 覆盖点全仓 4 处，全部显式 save=False（smoke_czce.py:23、
  paper/quotes.py:156、validate_sources.py:86、p36_smoke_sources.py:98），无 save=True。
- 主理人清单外但已核实安全的：paper/quotes.py:153（显式 save=False）、backup.py:77-86
  （透传参数，BackupRawFetcher 默认 save=False，backup.py:108）、p11_truth_rebuild.py
  （**无** Source 实例化，:5 仅为陈旧 docstring）、全部测试文件显式 save=False。

**新发现（漏改/低风险遗留，报主理人裁决）**：`hexbroker/data/sources/czce_source.py:79` 仍是
`save: bool = True` —— CzceSource 是与三源同构的第四个「取数即落盘」源（fetch_bars 内
`do_save = self.save if save is None else save` → save_processed，:248-251），D1 未覆盖。
当前全仓无依赖默认的调用点（artifacts/smoke_czce.py:17、tests/test_czce_source.py:70/92、
p36_smoke_sources.py:71 全显式 False；backup.py:85 透传 False）→ **无现实风险，不算 FAIL**，
但「取数纯读」原则未贯彻；若未来裸 `CzceSource()` 取数仍会自动窄窗口落盘（届时会被 D2 拦截）。

### A4. p43 blocked 零写入 — PASS
- `build_plan` 中 deny 名单命中（p43:112-118）与污染率超限（:119-128）均在任何 repairs 生成前
  提前返回 `repairs=[]`。
- apply 路径 `touched` 仅由 `p["repairs"]` 构建（p43:253-256）→ blocked 品种不进写盘循环（B8 实测
  BAD0 分区逐值不变）。
- `--deny-symbols ""` → `"".split(",")` 过滤空串（:184-185）→ 空元组 → deny 短路失效 → 污染率
  自动检测仍生效（B7 实测三品种全被拦）。

### A5. p43 sidecar — PASS
新条目 10 字段齐全（p43:291-307）：ts / tool / n_partitions / n_rows / residual_events_after /
k_cap / deny_symbols / repairs / gated / blocked；repairs 逐行 7 子字段（sym0/date/year/old_raw/
new_raw/k/adj_close，B8 实测）。顶层强制 list（:283-290，含非 list 防御）。写盘走
`write_json`（:307）＝ tempfile + os.replace 原子写（utils/io.py:68-76）。

## B 项实跑记录

### B6. 261→5 独立复现 — PASS
自建 fixture（非工程师测试文件），脚本 `C:\Users\Administrator\AppData\Local\Temp\qa_repro_shrink.py`：
- 首写 261 行 → 再写 5 行：抛 `PartitionShrinkError`，六属性全对
  （symbol=XX0, freq=1d, year=2024, n_before=261, n_after=5, tolerance=0.02），**磁盘仍 261 行**；
- `allow_shrink=True` → 磁盘 5 行；
- 边界语义：255 行（−2.29%）拦、256 行（−1.92%）放（261×0.98=255.78）；
- 全新分区首次写入不触发门禁。

### B7. p43 dry-run 对生产湖 — PASS（零写盘）
- 默认：`[BLOCKED] 3 个品种：ag0, au0, m0`；`[PLAN] 18 品种（G4 冻结 3）| 修复 0 处`；退出码 0。
- `--deny-symbols ""`：自动检测命中 —— ag0 73.87%（1470/1990）、au0 69.65%（1386/1990）、
  m0 74.51%（1482/1989），与主理人数字逐一吻合。

### B8. apply 等价性（临时小湖）— PASS
脚本 `C:\Users\Administrator\AppData\Local\Temp\qa_p43_apply.py`：
- `write_parquet` 与 `df.to_parquet` 对同一 df 产物等价（60 行 / 9 列同列序 / dtype 全同 / 值全等）；
- GOOD0（k 恒定 + 1 毛刺）apply：毛刺 raw 100→50 修复（k=2.0），行数/列序不变，复扫 0 事件，退出码 0；
- BAD0（k≡1 污染 67%）blocked：分区逐值不变（零写入）；
- sidecar 出现、顶层 list、10 字段齐全、repairs 逐行 7 子字段、blocked 记录 BAD0 及原因。

### B9. 全量测试复跑 — PASS
`Python311\python.exe -m pytest hexbroker/data tests/test_p43_raw_close_repair.py
tests/test_data_store_shrink_guard.py tests/test_p11_truth_rebuild.py -q`
→ 214 collected，213 passed + 1 skipped，退出码 0，与基线 214 passed / 1 skipped 一致。
p37 独立验证：tests/test_p37_raw_close_backfill.py 5 项 F，根因
`ModuleNotFoundError: No module named 'omegaconf'`（hexbroker/config.py:18 `from omegaconf
import …`），确认是环境缺依赖、与本轮改动无关（store.py 不依赖 omegaconf，hexbroker/data
214 项全过）。

## C 项生产湖完好性 — PASS
复核前快照（`data/raw/processed`）：162 个 parquet；ag0/au0/m0 各年行数
（2024 = 131/131/130，与事故后状态一致）。复核后复测：**0 个文件 mtime/size 变化、0 新增/删除、
行数全等**。复核全程仅对生产湖做只读访问 + p43 dry-run（零写盘路径），无任何污染。

## FAIL 清单
（无）

## 漏报/新发现问题清单（均非阻断，报主理人裁决）
1. **D1 漏改第四同构源**：czce_source.py:79 `save: bool = True` 未同步改 False。当前无活跃
   调用点依赖默认（全仓 CzceSource 实例化 5 处均显式 False 或透传 False），无现实风险；
   但「取数纯读」原则未贯彻，未来裸实例化会自动落盘（会被 D2 拦窄窗口，全年窗口则直接覆盖）。
   建议同批修正或书面豁免。
2. **p43 空 apply 也写 sidecar**：`total == 0 and not blocked` 才早退（p43:248）→ 当 total==0
   且有 blocked 时 `--apply` 会走完全程并追加一条 n_partitions=0 的 sidecar 记录。留痕行为
   可解释，但 sidecar 记录数不再严格等于「有实际写入的次数」，消费方需容忍 n_partitions=0 条目。
3. **p11_truth_rebuild.py:5 docstring 陈旧**：仍描述「AkshareSource(save=True) 默认落盘」，
   实际该脚本无任何 Source 实例化，易误导后来者。文档级。
4. **`_partition_row_count` 损坏文件路径**：分区文件存在但损坏时，pyarrow 快路径异常被吞后
   fallback `read_parquet` 的非 FileNotFoundError 异常会直接上抛 save_processed（不写盘，
   行为安全方向），但报错信息不含「分区损坏」上下文。极边缘。
