# 序 3 三品种 adj_close 口径重建 —— fresh-eyes QA 复核报告

- 复核人：seq3-qa（未参与实现，独立取证）
- 日期：2026-08-30
- 复核对象：`scripts/p6_4_fill_gaps.py`、`scripts/p11_truth_rebuild.py`、`hexbroker/data/rebuild.py`、`tests/test_p11_caliber_rebuild.py`（全部未 commit）
- 复核性质：**只读验证**。未执行任何 `--apply`，未修改被复核的 4 个文件，未写 `data/raw/processed/**`，未执行 `git commit`。

---

## 1. TL;DR

**判定：`PASS_WITH_CONDITIONS` —— 阻塞项 0，放行前必须满足的条件 4 条。**

代码改动本身**经实跑验证是正确的**：`RAW_SCALE_FIX` 确实被 `scale=1.0` 绕过（三品种常数实测生效/失效）、`raw_close` 继承确为逐日按日期对齐、三重校验删除逻辑在真实生产数据上正确触发且能正确中止、默认路径**逐位精确零回归**、dry-run **sha256 逐字节零写盘**、27 分区备份经校验可用作回滚点。

**但放行范围与放行依据必须收窄/重做**，理由（均有实跑证据，见 §3）：

1. **au0 / m0 的真值文件根本不存在** —— 全库搜索无任何 `*au0*truth*` / `*m0*truth*`。所谓"覆盖 27 个分区"目前只有 ag0 的 9 个分区有真值可跑。**27 分区 apply 现在物理上无法执行。**
2. **实现者提交的 k 范围汇总表 9 年里 7 年不可复现**，其中 6 年声称的下界值在全库中**根本不存在**（命中 0 天）。该表是本次 apply 的主要正确性证据，当前不可信，必须在 apply 前用真实 dry-run 重新生成。
3. **2024 必须显式使用 `p11_ag0_2024_truth_trim130.json`**；用全量真值会被 `--keep-raw-close` 正确中止（rc=1，112 天无源可继承）。且 2024 实际会**删除 1 行（2024-06-10，端午节假日）**，汇总表里写的是"干净"，漏报了这次删除。
4. **ag0 有 5 行 `raw_close` 是非整数**（ag 最小变动价位 1 元/千克，名义价不可能非整），与"raw_close 是唯一正确的列"的前提有出入。实测对 k 的影响约 0.008%，可接受，但须知情。

---

## 2. 验证矩阵

| # | 验证项 | 方法（我实跑的命令/脚本） | 结果 | 关键输出摘录 |
|---|---|---|---|---|
| A.1 | `scale` 传播路径：三品种 `scale=None` vs `1.0` | `artifacts/_seq3qa_probe_a_scale.py`（直接从模块读 `RAW_SCALE_FIX`，非抄录） | ✅ | ag0 None=5078.375284 / 1.0=3501.0，比值恒 1.4505499241；au0 1.2596349592；m0 0.3168296643 |
| A.1b | 真实 truth 文件上的逐行验证 | 同上，`p11_ag0_2019_truth.json` 244 行 | ✅ | ratio 唯一值 `[1.450549924103]`，恒等于 `RAW_SCALE_FIX['ag0']` |
| A.2 | `--no-scale-fix` ≡ `--scale 1.0` | CLI 实跑 4 组 + 单元测试 `test_no_scale_fix_equivalent_to_scale_one` | ✅ | 两组 2019-01-02 行逐字段相同：`raw_close 3711.0 / adj_close 2937.484551`；冲突用例 rc=2 |
| A.3 | 反向测试 `--scale 1.4505499241026856` | CLI 实跑 + 单元级 | ✅ | 输出与 `scale=None` **完全一致**（`adj_close 4260.967992`），透传链路未写死 |
| B.4 | ag0/2019 完整 dry-run | `p11_truth_rebuild.py --persisted .../p11_ag0_2019_truth.json --sym ag0 --year 2019 --no-scale-fix --keep-raw-close --drop-uncovered` | ✅ | 244 行，`[RAW-CLOSE] 继承 244 行`，`[PRECHECK] 将替换 244 行 + 追加 0 行（真值全覆盖 ✅）`，rc=0 |
| B.5 | 独立复算 2019-03-15 三个数 | `artifacts/_seq3qa_probe_b_indep.py`（自读 parquet + 自解析 truth JSON，不调 p6_4） | ✅ | `raw_close = 3596.0` ✅；`adj_close = 2846.4549835591415` ✅；`k = 0.7915614526026533` → 6 位 `0.791561` ✅。湖内污染 k=1.0，消除 20.8% 虚高 |
| B.6 | 继承是**逐日按日期**而非按位置 | 取 truth 第 11~20 行 + `sample(random_state=7)` 打乱日期顺序后复刻继承算法 | ✅ | 打乱后 `inherited_raw == lake_raw_bydate` 全 True；`== partition 前 10 行（按位置）` **False**；无 NaN |
| C.7 | 删除前打印完整清单 + 任一判据不中即中止零写盘 | 精读 `judge_uncovered()` + `--drop-uncovered` 分支；2 个单测；真实 2021/2022/2024 触发 | ✅ | 2021 打印 2 行完整清单（日期/O H L C/k/三项判据）后才执行；`test_drop_uncovered_aborts_when_date_is_real_trading_day`、`_k_is_not_one`、`_without_flag_still_aborts` 全过 |
| C.8 | 独立复算 15 健康品种参考日历 | 自写脚本取并集（未用实现的 `load_reference_calendar`） | ✅ | 2021 = **243** 交易日（15/15 品种全在场）；2022 = **242**（15/15）；2021-05-03 / 2021-10-01 / 2022-04-04 **均不在并集**，且无任何健康品种含这三天 |
| C.9 | 三日期不在 truth 中 | 自解析 `p11_ag0_2021/2022_truth.json` | ✅ | 三个日期均 **False**（不在真值）；且在 ag0 分区中存在，`k = 1.0`，`\|k−1\| = 0.000e+00`，指纹命中 |
| D.10 | 完整测试 | `pytest tests/test_p11_caliber_rebuild.py tests/test_p37_raw_close_backfill.py -q --basetemp=.pytest_tmp/seq3qa` | ⚠️ | **13 passed, 5 failed, 0 skipped**；5 个失败全为 `ModuleNotFoundError: No module named 'omegaconf'`（`hexbroker/config.py:18`），异常类型唯一，与本改动无关 |
| D.10b | 那个 "1 skip" 是什么 | 全文检索 `pytest.mark.skip` / `skipif` + `-rA` 逐用例列举 | ❌ | 测试文件里**没有任何 skip 标记**，实跑 **0 skipped**。"13 passed / 1 skip" 的说法不成立，见 §3 P2-5 |
| D.11 | **关键零回归**：默认路径行为不变 | 差分对比：`git archive HEAD` 导出改前树 → 顶掉 `BackupRawFetcher` 为确定性 stub → old/new 各自跑 5 个用例逐位比对 | ✅ | c1_default / c2_skipnominal / c3_uncovered_abort 三例 `assert_frame_equal(check_exact=True)` **逐列逐位通过**；rc 一致（0/0/1） |
| D.12 | `rebuild_partition(drop_dates=None)` 不删行 + 其他调用方不受影响 | 全库 grep 调用方 + 跑 `hexbroker/data/test_rebuild.py` | ✅ | 调用方仅 `rebuild.py:427`（batch 驱动，位置参数）、`hexbroker/data/test_rebuild.py`（位置参数）、`p11:370`（显式传）。前两处 `drop_dates` 恒为 None。回归测试 **42 passed** |
| E.13 | dry-run 零写盘 | 全部操作前对 27 分区取 sha256 快照，全部操作后复算 | ✅ | 27/27 分区 sha256 **逐字节未变**；无新增、无丢失；`find -newermt` 与 `-newer .seq3qa_marker` 双路交叉验证均为空 |
| F.14 | 自行发现的问题 | 见 §3 | ⚠️ | 共发现 9 项，其中 P1 3 项、P2 6 项 |

**⛔ 未验证项：无。** 清单 14 条全部跑到了证据。唯一的环境限制是 `omegaconf` 缺失导致 p37 的 5 个用例无法执行（既有失败，已确认根因唯一）。

---

## 3. 缺陷清单

### P1 —— apply 前必须处理（3 条）

#### P1-1. au0 / m0 真值数据不存在，"27 分区 apply" 当前无法执行
- **文件/位置**：`artifacts/`（数据缺口，非代码缺陷）
- **证据**：
  ```
  $ ls artifacts/ | grep -iE "au0|m0"       → (空)
  $ find . -name "*au0*truth*" -o -name "*m0*truth*" | grep -v pytest_tmp   → (空)
  $ ls artifacts/p11_ag0_*.json
    p11_ag0_2018..2026_truth.json (9 个) + p11_ag0_2024_truth_trim130.json
  ```
  全库只有 ag0 的 9 年真值。au0 / m0 的 `close_pcr` 真值**一次都没有拉过**。
- **影响**：这次放行的决策被描述成"覆盖 27 个生产数据分区"，但其中 18 个（au0×9 + m0×9）**没有输入数据**，跑起来会在 `--keep-raw-close` 处直接中止或根本无从下手。
- **建议**：**把放行范围显式收窄为 ag0 的 9 个分区**。au0/m0 须先按 pandadata 拉取规程（含 `len(rows)==total_rows` 校验 + 程序化归档）补齐真值、各自跑通 dry-run 后，再单独申请放行。

#### P1-2. k 范围汇总表 7/9 年不可复现，6 年的声称下界值在全库中根本不存在
- **文件/位置**：实现者提交的《ag0 全 9 年 dry-run 汇总表》（本次复核的输入材料）
- **证据**：我独立复算 `k = truth.close_pcr / 分区.raw_close`（`artifacts/_seq3qa_probe_e_table.py` + `_seq3qa_probe_g_anomaly.py` G.4）：

  | 年 | 我实算 k 范围 | 实现者声称 | 声称下界能否在全库命中 |
  |---|---|---|---|
  | 2018 | 0.791561 ~ 0.820598 | 0.794347~0.820635 | ⛔ **0 天** |
  | 2019 | 0.773518 ~ 0.791786 | 0.791561~0.791786 | 命中 80 天，但那是**年末段的值**，被当成了下界 |
  | 2020 | 0.749894 ~ 0.773393 | 0.785561~0.785561 | ⛔ **0 天**（声称值甚至不在实际区间内） |
  | 2021 | 0.716564 ~ 0.750755 | 0.775033~0.786058 | ⛔ **0 天** |
  | 2022 | 0.707424 ~ 0.716711 | 0.753970~0.760234 | ⛔ **0 天** |
  | 2023 | 0.696474 ~ 0.708289 | 0.727143~0.745520 | ⛔ **0 天** |
  | 2024 | 0.693050 ~ 0.696480 | 0.676808~0.700000 | ⛔ **0 天**（0.676808 实为 **2025** 年值，见下） |
  | 2025 | 0.676808 ~ 0.686919 | 0.676808~0.686919 | ✅ 完全一致 |
  | 2026 | 0.678041 ~ 0.686714 | 0.678041~0.686714 | ✅ 完全一致 |

  旁证：声称的 2024 下界 `0.676808` 与 2025 下界**逐位相同**，而 0.676808 实际只在 2025-11-11~12-31（36 天）出现。
- **影响**：这张表是"重建结果对不对"的唯一人工判据。它不可复现 → apply 后**没有可信的比对基线**。注意代码本身没问题，`adj_close` 直接来自 pandadata `close_pcr`，是真值；坏的是**证据**。
- **建议**：apply 前用本报告 §2 我实算的表（或重跑 dry-run 自行导出）替换该表，作为 apply 后的验收基线。同时追查该表是怎么产出的 —— 若是人工整理/估算而非程序导出，则整个"人工汇总 evidence"流程都需要改成程序化。

#### P1-3. 2024 年实际会删除 1 行（2024-06-10），汇总表记为"干净"，属漏报
- **文件/位置**：汇总表 2024 行（记为 `130 | 130(100%) | 130(100%) | 干净`）；实际行为见 `scripts/p11_truth_rebuild.py:296-340`
- **证据**（我用 trim130 真值实跑）：
  ```
  [DROP-CHECK] 待删日期 1 个（参考日历：15/15 个健康品种 2024 年并集，242 个交易日）
  [DROP-CHECK]   1  2024-06-10  ✅  ✅  ✅  7359.0/8994.0/7359.0/8177.0  1.000000000
  [DROP-CHECK] ag0/2024: 三重校验全中 ✅，将剔除 1 个幽灵行（分区 131 → 130 行后再替换）
  ```
  独立核验：2024-06-10 是**周一**，15 健康品种 2024 日历（242 天）中**不存在**；邻近交易日为 06-07 与 06-11（端午节假期）。该行 OHLC 振幅 22%（7359→8994）且 open==low，确为废 bar。**删除本身是正确的。**
- **影响**：删除合法，但汇总表少报 1 次删除（声称总数 3 次，实际 4 次：2021×2 + 2022×1 + 2024×1）。主理人核对行数时会对不上。
- **建议**：更正汇总表，2024 行注明"待删 1（2024-06-10，端午）"。

---

### P2 —— 排期修复（6 条）

#### P2-4. ag0 / m0 存在非整数 `raw_close`，与"raw_close 唯一正确"的前提不符
- **文件/位置**：数据层（`--keep-raw-close` 会逐日继承这些值）
- **证据**（`artifacts/_seq3qa_probe_f_rawclose.py`）：
  ```
  ag0 非整数 raw_close 合计 5 行 / 1990：
    2021-01-04=5698.458576   2021-01-14=5196.516642
    2023-01-09=5256.110301   2023-11-02=5824.892902
    2024-07-04=8016.082994
  m0 非整数 raw_close 合计 5 行 / 1989：
    2019-11-04=2973.776863  2019-11-07=2925.446957  2019-11-12=2884.856630
    2020-07-30=2908.083336  2023-04-07=3569.560269
  对照健康品种 rb0：0 行非整数 / 2098；cu0：7 行 / 2098
  ```
  ag（白银）最小变动价位 1 元/千克，名义价不可能出现 `5698.458576`。同日 `adj_close` 反而是整数（5773.0）。
- **影响评估**：实测这 5 天的 k 落在邻日正常区间内（例：2021-01-14 k=0.749245，邻日 0.749167~0.749513），偏差量级 ≈ 0.008%，**不影响修复目标**。但"raw_close 是三列中唯一正确的列"这句话需要改成"raw_close 是三列中相对最可信的列，含 5/1990 已知异常值"。
- **建议**：apply 后把这几行列入已知数据瑕疵清单，随 序 4 一并处理。

#### P2-5. "13 passed / 1 skip" 的说法不成立，实际是 13 passed / 0 skipped
- **文件/位置**：`tests/test_p11_caliber_rebuild.py`（实现者自述）
- **证据**：
  ```
  $ pytest tests/test_p11_caliber_rebuild.py tests/test_p37_raw_close_backfill.py -rA --tb=no -q
  PASSED ... (13 条，逐条列出)
  FAILED tests/test_p37_raw_close_backfill.py::test_dry_run_makes_no_writes
  FAILED ...::test_apply_rewrites_raw_close_only_and_manifest_semantics
  FAILED ...::test_apply_idempotent_second_run
  FAILED ...::test_warm_up_uses_real_data_range_not_future_window
  FAILED ...::test_low_coverage_partition_skipped_loudly
  5 failed, 13 passed in 7.36s
  ```
  全文检索 `pytest.mark.skip` / `skipif`：**无**。
- **影响**：轻微。测试数量对得上（13），仅 skip 计数错误。但"声称的测试统计与实际不符"本身是证据质量问题，在 P1-2 的背景下值得记录。
- **建议**：更正自述；若原本设计过一个 skip 用例（例如依赖网络的端到端用例），应显式加 `pytest.mark.skip(reason=...)` 而不是口头记为 skip。

#### P2-6. `--keep-raw-close` 把"`raw_close` 为 NaN"误报成"日期在既有分区中不存在"
- **文件/位置**：`scripts/p11_truth_rebuild.py:262-271`
- **证据**：`mapped = truth["datetime"].map(by_date)` 的 NaN 同时来自「日期缺失」和「该日 raw_close 本身是 NaN」两种情形，但错误信息只说前者：
  ```
  [FAIL] --keep-raw-close：真值 N 行中 M 个日期在既有分区中不存在（首个 X，末个 Y）
  ```
- **影响**：当前不可触发（实测三品种 raw_close 无 NaN、无 0，见下），但一旦触发会把排障引向错误方向（"去检查真值拉取区间"，而实际是分区里有 NaN）。中止方向是安全的，不会写坏数据。
- **建议**：区分两种 NaN，分别给错误信息。

#### P2-7. `raw_close == 0.0` 会被静默继承（当前不可触发，但无防护）
- **文件/位置**：`scripts/p11_truth_rebuild.py:272-275`
- **证据**：`coerce_schema` 对 float 列 `fillna(0.0)`，所以分区里的 `raw_close` 理论上是 `0.0` 而不是 NaN。`mapped.isna()` 检测不到 `0.0`，代码会**静默继承 0.0**，导致 `k = adj/0 = inf`。
  实测当前三品种均为 0 行 `raw_close==0` / 0 行 NaN，故**当前不可触发**。
- **建议**：加一道 `<= 0` 的显式拒绝（成本极低，且这类哨兵值一旦出现后果是 inf 污染整列）。

#### P2-8. "k 只在换月日跳变，段内恒定" 这一模型假设，与实测数据不符
- **文件/位置**：本次复核的背景假设（也是 `RAW_SCALE_FIX` 证伪论证的一部分）
- **证据**（`artifacts/_seq3qa_probe_g_anomaly.py` G.3，按 `dominant_id` 分段统计）：
  ```
  2021 AG2106.SHF  n=87  k_std=0.003603  k 唯一值 19 个
  2023 AG2402.SHF  n=69  k_std=0.000956  k 唯一值 32 个
  2019 AG1912.SHF  n=128 k_std=0.000448  k 唯一值 4 个
  结论：k(比率) 段内恒定? False    (adj-raw)(差额) 段内恒定? False
  ```
  逐日 k 跳变检测也显示 2023 有 63 次跳变、其中 2023-09-06~11-07 是**连续 40 个交易日**每天变。
- **影响**：不否定这次修复（`adj_close` 直接取自 pandadata 真值，与 k 是否恒定无关），但"用 k 范围做正确性判据"的效力比假设的弱得多 —— 这正好解释了 P1-2 为什么容易出现对不上的数。另外 `RAW_SCALE_FIX` 的证伪论证中"实测 2025 k=0.986、2026 k=0.9937 偏离 1"这一条，其论据基础"段内 k 恒定"本身站不住，建议改用更硬的论据（pandadata 单合约名义价逐比对，这条已经是硬的）。
- **建议**：把背景文档里的口径模型改成实测描述（"k 段内缓慢漂移，跨段有跳变"），避免后人拿错误模型做判据。

#### P2-9. 2024→2025 年边界存在 −0.96% 的 k 跳变（其余边界 ≤0.11%）
- **文件/位置**：数据层，序 4 已跟踪
- **证据**（我实算的跨年衔接）：
  ```
  2018→2019 Δk=+0.000000 (+0.00%)   2019→2020 Δk=-0.000724 (-0.09%)
  2020→2021 Δk=+0.000861 (+0.11%)   2021→2022 Δk=+0.000000 (+0.00%)
  2022→2023 Δk=-0.000418 (-0.06%)   2023→2024 Δk=-0.000000 (-0.00%)
  2024→2025 Δk=-0.006685 (-0.96%)   ← 异常
  2025→2026 Δk=+0.000000 (+0.00%)
  ```
  根因是 2024 分区只到 2024-07-17（缺 112 天），复权链在此断开。
- **影响**：apply 后任何跨越 2024-07-17 → 2025-01-02 的下游计算都会吃到这 −0.96% 的假跳空。已在 序 4 范围内，但**apply 后必须显式告知下游**。
- **建议**：随 序 4 修复；在修复前于 2024/2025 分区旁留 sidecar 说明。

---

### 补充观察（不构成缺陷，但值得记录）

- **`assert cur is not None`**（`p11_truth_rebuild.py:258`）：用 assert 承担控制流不变式，Python `-O` 下会被剥离。位置在 `if not replacing: return 1` 之后，实际不可达，属风格问题。
- **参考日历退化风险**：`load_reference_calendar` 只在"一个品种都没有"时中止（`if not used`）。若只有 1/15 个品种有该年分区，并集就退化成单品种日历，"15 品种并集更权威"的论证失效。代码已在 `[DROP-CHECK]` 输出 `{len(used)}/15`，实测 2021/2022/2024 均为 15/15，可接受。
- **三重校验的判据①恒真**：`judge_uncovered` 的迭代集是 `set(cur) - set(truth)`，所以 `c1_not_in_truth` 由构造保证为 True。实现者在 docstring 里**明确承认了这一点**（"由构造保证恒真，仍显式记录以便审阅"），**没有**在文档里夸大它的作用 —— 这点做得对，不算缺陷。
- **D2 缩水门禁不会误拦**：`DEFAULT_SHRINK_TOLERANCE = 0.02`，各年净变化 2021 −0.82%、2022 −0.41%、2024 −0.76%，均远低于 2%，且 `rebuild_partition` 未传 `allow_shrink`。已核对过阈值与调用点。

---

## 4. 放行建议

### 是否可以放行 `--apply` 覆盖 27 个分区？

**否 —— 但不是因为代码有问题，而是因为 27 个分区里有 18 个没有输入数据。**

**可以有条件放行：ag0 的 9 个分区（2018→2026）。条件如下：**

**C1（范围）** —— **只放行 ag0 的 9 个分区**。au0 / m0 的真值文件不存在（P1-1），必须先补齐真值 + 各自跑通 dry-run 后另行申请。

**C2（2024 专用真值）** —— 2024 **必须**使用 `artifacts/p11_ag0_2024_truth_trim130.json`，不能用 `p11_ag0_2024_truth.json`（后者会在 `--keep-raw-close` 处以 rc=1 中止：112 个日期无源可继承，零写盘，行为正确）。

**C3（登记删除）** —— 本次 apply 实际会删除 **4 行**（不是汇总表写的 3 行）：2021-05-03、2021-10-01、2022-04-04、2024-06-10。四行均已通过三重校验且我独立核验为法定节假日 + k==1。apply 前应把 2024-06-10 补进待删清单（P1-3）。

**C4（验收基线）** —— apply 前用本报告 §2 我实算的 k 范围表替换原汇总表（P1-2），apply 后逐年核对行数与 k 范围。

**执行顺序与核验点（建议）：**
1. apply **严格升序 2018 → 2026，一次一年**，每步确认 `rc=0` 再进入下一年；任一年非 0 立即停止，不要继续。
2. 预期结果：

   | 年 | 行数 before → after | 删除 | 我实算 k 范围（验收基线） |
   |---|---|---|---|
   | 2018 | 243 → 243 | 0 | 0.791561 ~ 0.820598 |
   | 2019 | 244 → 244 | 0 | 0.773518 ~ 0.791786 |
   | 2020 | 243 → 243 | 0 | 0.749894 ~ 0.773393 |
   | 2021 | 245 → 243 | 2 | 0.716564 ~ 0.750755 |
   | 2022 | 243 → 242 | 1 | 0.707424 ~ 0.716711 |
   | 2023 | 242 → 242 | 0 | 0.696474 ~ 0.708289 |
   | 2024 | 131 → 130（trim130 真值） | 1 | 0.693050 ~ 0.696480 |
   | 2025 | 243 → 243 | 0 | 0.676808 ~ 0.686919 |
   | 2026 | 156 → 156 | 0 | 0.678041 ~ 0.686714 |

   ag0 合计 1990 → 1984 行。
3. apply 后**至少抽验一行**：ag0/2019-03-15 应为 `raw_close == 3596.0`、`adj_close == 2846.4549835591415`、`k == 0.791561`（我独立复算过，见 B.5）。
4. **回滚点已验证可用**：`artifacts/p3_caliber_backup_20260830T122935/` 含 27 个分区，与我复核前的湖内 sha256 **逐字节一致**（27/27，0 不一致，0 缺失）。出问题可直接还原。

**代码层面无需任何修改即可放行** —— P1 三条全部是"放行材料/范围"问题，不是代码缺陷；P2 六条均可在 apply 后排期。

---

## 5. 附录：关键命令完整 stdout

### A-1. `artifacts/_seq3qa_probe_a_scale.py`（scale 传播路径实测）

```
==============================================================================
A.0  RAW_SCALE_FIX 实际内容（从模块读，非抄录）
==============================================================================
{'m0': 0.31682966433418885, 'au0': 1.2596349591656173, 'ag0': 1.4505499241026856}

==============================================================================
A.1  scale=None vs scale=1.0 vs scale=RAW_SCALE_FIX[sym]  （合成输入 base=3500）
==============================================================================
[SCALE] ag0 close_pcr → 既有原始口径 系数 1.45054992
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
[SCALE] ag0 close_pcr → 既有原始口径 系数 1.45054992

--- ag0 (RAW_SCALE_FIX=1.4505499241026856) ---
  scale=None   adj_close = [5078.375284, 5079.825834, 5081.276384]
  scale=1.0    adj_close = [3501.0, 3502.0, 3503.0]
  scale=const  adj_close = [5078.375284, 5079.825834, 5081.276384]
  ratio(None/1.0)  = [1.4505499241, 1.4505499241, 1.4505499241]
  None == scale=const ? True   (反向测试：透传链路未写死)
  scale=1.0 恰等于输入 close ? True
  scale=None   raw_close = [5078.3753, 5079.8258, 5081.2764]
  scale=1.0    raw_close = [3501.0, 3502.0, 3503.0]
[SCALE] au0 close_pcr → 既有原始口径 系数 1.25963496
[SCALE] au0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
[SCALE] au0 close_pcr → 既有原始口径 系数 1.25963496

--- au0 (RAW_SCALE_FIX=1.2596349591656173) ---
  scale=None   adj_close = [4409.981992, 4411.241627, 4412.501262]
  scale=1.0    adj_close = [3501.0, 3502.0, 3503.0]
  scale=const  adj_close = [4409.981992, 4411.241627, 4412.501262]
  ratio(None/1.0)  = [1.2596349592, 1.2596349592, 1.2596349592]
  None == scale=const ? True   (反向测试：透传链路未写死)
  scale=1.0 恰等于输入 close ? True
  scale=None   raw_close = [4409.982, 4411.2416, 4412.5013]
  scale=1.0    raw_close = [3501.0, 3502.0, 3503.0]
[SCALE] m0 close_pcr → 既有原始口径 系数 0.31682966
[SCALE] m0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
[SCALE] m0 close_pcr → 既有原始口径 系数 0.31682966

--- m0 (RAW_SCALE_FIX=0.31682966433418885) ---
  scale=None   adj_close = [1109.220655, 1109.537484, 1109.854314]
  scale=1.0    adj_close = [3501.0, 3502.0, 3503.0]
  scale=const  adj_close = [1109.220655, 1109.537484, 1109.854314]
  ratio(None/1.0)  = [0.3168296643, 0.3168296643, 0.3168296643]
  None == scale=const ? True   (反向测试：透传链路未写死)
  scale=1.0 恰等于输入 close ? True
  scale=None   raw_close = [1109.2207, 1109.5375, 1109.8543]
  scale=1.0    raw_close = [3501.0, 3502.0, 3503.0]

==============================================================================
A.1b 真实 truth 文件（ag0 2019）scale=None vs 1.0，逐行前 3 行
==============================================================================
[INFO] load_persisted_rows -> 244 行, 列=['date', 'symbol', 'underlying_symbol', 'exchange', 'dominant_id', 'open', 'high', 'low', 'close', 'volume', 'open_interest', 'amount', 'settlement', 'pre_settlement', 'limit_up', 'limit_down', 'day_session_open', 'method']
[INFO] normalize_sym_arg('ag0') -> underlying='AG' sym0='ag0'
[SCALE] ag0 close_pcr → 既有原始口径 系数 1.45054992
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
[INFO] 行数 None=244  1.0=244
  datetime    adj_None     adj_1.0   ratio
2019-01-02 4260.967992 2937.484551 1.45055
2019-01-03 4319.526162 2977.854185 1.45055
2019-01-04 4339.045551 2991.310729 1.45055
[INFO] ratio 唯一值 = [1.450549924103]
[INFO] ratio 是否恒等于 RAW_SCALE_FIX[ag0] = True
```

### A-2. CLI 层 `--no-scale-fix` / `--scale` 等价性与反向测试

```
---------- flags: [--no-scale-fix] ----------
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
symbol   datetime        open        high         low       close   volume  amount  open_interest  raw_close   adj_close  limit_up  limit_down  is_rollover
   ag0 2019-01-02 2928.777375 2943.025481 2919.278637 2937.484551 306902.0       0       730802.0     3711.0 2937.484551     False       False        False
rc=0
---------- flags: [--scale 1.0] ----------
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
symbol   datetime        open        high         low       close   volume  amount  open_interest  raw_close   adj_close  limit_up  limit_down  is_rollover
   ag0 2019-01-02 2928.777375 2943.025481 2919.278637 2937.484551 306902.0       0       730802.0     3711.0 2937.484551     False       False        False
rc=0
---------- flags: [--scale 1.4505499241026856] ----------
[SCALE] ag0 close_pcr → 既有原始口径 系数 1.45054992
symbol   datetime        open        high         low       close   volume  amount  open_interest  raw_close   adj_close  limit_up  limit_down  is_rollover
   ag0 2019-01-02 4248.337798 4269.005388 4234.559406 4260.967992 306902.0       0       730802.0     3711.0 4260.967992     False       False        False
rc=0
---------- flags: [<无，走 RAW_SCALE_FIX>] ----------
[SCALE] ag0 close_pcr → 既有原始口径 系数 1.45054992
symbol   datetime        open        high         low       close   volume  amount  open_interest  raw_close   adj_close  limit_up  limit_down  is_rollover
   ag0 2019-01-02 4248.337798 4269.005388 4234.559406 4260.967992 306902.0       0       730802.0     3711.0 4260.967992     False       False        False
rc=0

########## A.2b --no-scale-fix 与 --scale 1.5 冲突 ##########
[FAIL] --no-scale-fix 与 --scale 1.5 冲突（前者等价 --scale 1.0）
rc=2
```

> 注意：`--keep-raw-close` 下四种变体的 `raw_close` 恒为 `3711.0`（继承值），未被 scale 污染 —— 说明 raw_close 的继承发生在 scale 换算之后，唯一正确的列确实受保护。

### B-4. ag0/2019 完整 dry-run（生产湖，只读）

```
$ PYTHONPATH=E:/Workspace/HexBroker python scripts/p11_truth_rebuild.py \
    --persisted artifacts/p11_ag0_2019_truth.json --sym ag0 --year 2019 \
    --no-scale-fix --keep-raw-close --drop-uncovered

[DRY-RUN] 预览模式，不写盘（加 --apply 执行重建）
[PARSE] 真值文件 244 行，列: ['date', 'symbol', 'underlying_symbol', 'exchange', 'dominant_id', 'open', 'high', 'low', 'close', 'volume', 'open_interest', 'amount']...
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
[PARSE] 目标真值 244 行，2019-01-02 ~ 2019-12-31
[RAW-CLOSE] ag0: 从既有分区逐行继承 raw_close 244 行（跳过 enrich_raw_close，唯一正确列零损伤）
[PRECHECK] 既有分区 244 行：将替换 244 行 + 追加 0 行（真值全覆盖 ✅）
[DRY-RUN] 将整年替换 ag0/1d/2019.parquet (244 行)；预览:
symbol   datetime        open        high         low       close   volume  amount  open_interest  raw_close   adj_close  limit_up  limit_down  is_rollover
   ag0 2019-01-02 2928.777375 2943.025481 2919.278637 2937.484551 306902.0       0       730802.0     3711.0 2937.484551     False       False        False
   ag0 2019-01-03 2934.318305 2980.228869 2927.194252 2977.854185 693698.0       0       757080.0     3762.0 2977.854185     False       False        False
   ag0 2019-01-04 2973.104816 3015.849134 2962.814517 2991.310729 769274.0       0       744256.0     3779.0 2991.310729     False       False        False
### RC=0
```

### B-5/B-6/C-8/C-9. `artifacts/_seq3qa_probe_b_indep.py`（独立复算）

```
==============================================================================
B.5  独立复算 ag0 2019-03-15 的 raw_close / adj_close / k
==============================================================================
[湖内] ag0/2019 行数=244  范围 2019-01-02 ~ 2019-12-31
[湖内] 2019-03-15 命中行数 = 1
[湖内] raw_close = 3596.0
[湖内] adj_close = 3596.0
[湖内] k(adj/raw) = 1.0   <-- 污染形态，应≈1.0

[TRUTH] 文件 p11_ag0_2019_truth.json: rows=244
[TRUTH] params = {'underlying_symbol': 'AG', 'start_date': '20190101', 'end_date': '20191231', 'method': 'close_pcr'}
[TRUTH] 2019-03-15 命中行数 = 1
[TRUTH] close (close_pcr 后复权真值) = 2846.4549835591415

[独立复算] raw_close  = 3596.0   (期望 3596.0)
[独立复算] adj_close  = 2846.4549835591415   (期望 2846.4549835591415)
[独立复算] k          = 0.7915614526026533
[独立复算] k 四舍五入 6 位 = 0.791561   (实现者声称 0.791561)
[判定] raw==3596.0 ? True
[判定] adj==2846.4549835591415 ? True
[判定] k 6 位 == 0.791561 ? True
[判定] 重建后 k 与污染 k 的差 = 0.208439（20.8% 虚高被消除）

[全 2019 独立复算] 真值∩分区 = 244 行；k_new 范围 0.773518 ~ 0.791786
[对照] 实现者声称 2019 k 范围 0.791561 ~ 0.791786

==============================================================================
B.6  raw_close 继承是否逐日按日期对齐（而非按位置）
==============================================================================
[构造] 取真值第 11~20 行（10 行）并随机打乱；打乱后日期顺序 = ['2019-01-28', '2019-01-23', '2019-01-16', '2019-01-18', '2019-01-17']...
[SCALE] ag0 系数 1.0（不换算，原样采用 close_pcr 后复权真值）
  datetime  inherited_raw  lake_raw_bydate  partition_first10_raw
2019-01-28         3720.0           3720.0                 3711.0
2019-01-23         3668.0           3668.0                 3762.0
2019-01-16         3710.0           3710.0                 3779.0
2019-01-18         3711.0           3711.0                 3778.0
2019-01-17         3706.0           3706.0                 3744.0
2019-01-29         3733.0           3733.0                 3743.0
2019-01-25         3652.0           3652.0                 3751.0
2019-01-21         3661.0           3661.0                 3732.0
2019-01-24         3651.0           3651.0                 3714.0
2019-01-22         3644.0           3644.0                 3715.0
[判定] 继承值 == 按日期取到的湖内值 ? True
[判定] 继承值 == 按位置取的前 10 行 ? False  (False = 确认是按日期而非按位置)
[判定] 继承后有无 NaN ? False

==============================================================================
C.8  独立复算 15 健康品种参考日历
==============================================================================

--- 2021 ---
  参与并集品种 15/15: ['al0', 'cf0', 'cu0', 'hc0', 'i0', 'j0', 'jm0', 'ni0', 'p0', 'rb0', 'sc0', 'sr0', 'ta0', 'y0', 'zn0']
  缺分区: []
  各品种行数: {'al0': 243, 'cf0': 243, 'cu0': 243, 'hc0': 243, 'i0': 243, 'j0': 243, 'jm0': 243, 'ni0': 243, 'p0': 243, 'rb0': 243, 'sc0': 243, 'sr0': 243, 'ta0': 243, 'y0': 243, 'zn0': 243}
  并集交易日数 = 243
  2021-05-03 在并集中 ? False   (期望 False = 非交易日，可删)   ✅
      含该日的健康品种: 无
  2021-10-01 在并集中 ? False   (期望 False = 非交易日，可删)   ✅
      含该日的健康品种: 无

--- 2022 ---
  参与并集品种 15/15: ['al0', 'cf0', 'cu0', 'hc0', 'i0', 'j0', 'jm0', 'ni0', 'p0', 'rb0', 'sc0', 'sr0', 'ta0', 'y0', 'zn0']
  缺分区: []
  各品种行数: {'al0': 242, 'cf0': 242, 'cu0': 242, 'hc0': 242, 'i0': 242, 'j0': 242, 'jm0': 242, 'ni0': 242, 'p0': 242, 'rb0': 242, 'sc0': 242, 'sr0': 242, 'ta0': 242, 'y0': 242, 'zn0': 242}
  并集交易日数 = 242
  2022-04-04 在并集中 ? False   (期望 False = 非交易日，可删)   ✅
      含该日的健康品种: 无

==============================================================================
C.9  三个待删日期是否真的不在 truth JSON 中
==============================================================================

--- p11_ag0_2021_truth.json: 243 个日期，2021-01-04 ~ 2021-12-31 ---
  2021-05-03 在真值中 ? False   (期望 False) ✅
  2021-10-01 在真值中 ? False   (期望 False) ✅
  2021-05-03 在 ag0/2021 分区中: raw=5333.0 adj=5333.0 k=1.0 |k-1|=0.000e+00 (✅ k==1 指纹命中)
  2021-10-01 在 ag0/2021 分区中: raw=4615.0 adj=4615.0 k=1.0 |k-1|=0.000e+00 (✅ k==1 指纹命中)

--- p11_ag0_2022_truth.json: 242 个日期，2022-01-04 ~ 2022-12-30 ---
  2022-04-04 在真值中 ? False   (期望 False) ✅
  2022-04-04 在 ag0/2022 分区中: raw=5038.0 adj=5038.0 k=1.0 |k-1|=0.000e+00 (✅ k==1 指纹命中)
```

### D-10. 完整测试输出

```
$ PYTHONPATH=E:/Workspace/HexBroker python -m pytest \
    tests/test_p11_caliber_rebuild.py tests/test_p37_raw_close_backfill.py \
    -p no:cacheprovider --basetemp=.pytest_tmp/seq3qa -rA --tb=no -q

PASSED tests/test_p11_caliber_rebuild.py::test_scale_none_keeps_raw_scale_fix_for_ag0
PASSED tests/test_p11_caliber_rebuild.py::test_scale_one_bypasses_raw_scale_fix_for_ag0
PASSED tests/test_p11_caliber_rebuild.py::test_scale_one_does_not_touch_unlisted_symbol
PASSED tests/test_p11_caliber_rebuild.py::test_keep_raw_close_inherits_existing_column
PASSED tests/test_p11_caliber_rebuild.py::test_keep_raw_close_dry_run_writes_nothing
PASSED tests/test_p11_caliber_rebuild.py::test_keep_raw_close_aborts_on_unknown_truth_dates
PASSED tests/test_p11_caliber_rebuild.py::test_keep_raw_close_missing_partition_aborts
PASSED tests/test_p11_caliber_rebuild.py::test_drop_uncovered_deletes_ghost_rows
PASSED tests/test_p11_caliber_rebuild.py::test_drop_uncovered_aborts_when_date_is_real_trading_day
PASSED tests/test_p11_caliber_rebuild.py::test_drop_uncovered_aborts_when_k_is_not_one
PASSED tests/test_p11_caliber_rebuild.py::test_drop_uncovered_without_flag_still_aborts
PASSED tests/test_p11_caliber_rebuild.py::test_no_scale_fix_conflicts_with_explicit_scale
PASSED tests/test_p11_caliber_rebuild.py::test_no_scale_fix_equivalent_to_scale_one
FAILED tests/test_p37_raw_close_backfill.py::test_dry_run_makes_no_writes - M...
FAILED tests/test_p37_raw_close_backfill.py::test_apply_rewrites_raw_close_only_and_manifest_semantics
FAILED tests/test_p37_raw_close_backfill.py::test_apply_idempotent_second_run
FAILED tests/test_p37_raw_close_backfill.py::test_warm_up_uses_real_data_range_not_future_window
FAILED tests/test_p37_raw_close_backfill.py::test_low_coverage_partition_skipped_loudly

5 failed, 13 passed in 7.36s
```

5 个失败根因唯一性核验：
```
$ grep -c "ModuleNotFoundError: No module named 'omegaconf'" artifacts/_seq3qa_pytest1.txt  → 5
$ grep -oE "^E +[A-Za-z_.]*(Error|Exception|Warning)" ... | sort | uniq -c
      5 E   ModuleNotFoundError
```
全部为 `hexbroker/config.py:18: from omegaconf import DictConfig, OmegaConf`。环境确认 `import omegaconf` → `ModuleNotFoundError`。**与本改动无关。**

### D-11. 零回归差分对比（HEAD vs 改后，确定性 stub 顶掉备源）

```
=================== 差分对比 ===================
----- c1_default（不加任何新开关，走 enrich_raw_close）-----
  分区结果: IDENTICAL ✅
  old: [RC] 0
  new: [RC] 0
----- c2_skipnominal -----
  分区结果: IDENTICAL ✅
  old: [RC] 0
  new: [RC] 0
----- c3_uncovered_abort（真值缺一天 → 必须中止零写盘）-----
  分区结果: IDENTICAL ✅
  old: [RC] 1
  new: [RC] 1
----- c4_dropflag（--drop-uncovered）-----
  old: argparse: error: unrecognized arguments: --drop-uncovered   ← 新开关，旧版不识别，符合预期
  new: [RC] 0
----- c5_keepraw（--keep-raw-close）-----
  old: argparse: error: unrecognized arguments: --keep-raw-close   ← 同上，符合预期
  new: [RC] 0

=== c1/c2 精确值比对（非 CSV 文本，逐位） ===
c1_default: 逐列逐位精确相同 ✅  (rows=5)
c2_skipnominal: 逐列逐位精确相同 ✅  (rows=5)
c3_uncovered_abort: 逐列逐位精确相同 ✅  (rows=5)
```

方法说明：`git archive HEAD | tar -x -C <临时目录>` 导出改前树（只读，不动工作区），两侧各以自身 ROOT 为 `sys.path[0]` 独立进程运行，用同一个 stub 顶掉 `hexbroker.data.backup.BackupRawFetcher` 使 `enrich_raw_close` 完全确定性，最后用 `pd.testing.assert_frame_equal(check_exact=True)` 比对落盘分区。

### D-10b/D-12. rebuild 相关回归测试

```
$ PYTHONPATH=E:/Workspace/HexBroker python -m pytest \
    hexbroker/data/test_rebuild.py tests/test_p11_truth_rebuild.py \
    tests/test_p11_caliber_rebuild.py -q --basetemp=.pytest_tmp/seq3qa2 --tb=short
..........................................                               [100%]
```
42 passed。

`rebuild_partition` 调用方清单（确认除 p11 外均为位置参数、`drop_dates` 恒为 None）：
```
./hexbroker/data/rebuild.py:427:        report.results.append(rebuild_partition(root, it, truth))
./hexbroker/data/test_rebuild.py:165,182,195,209,223,231,247,265   (全部位置参数)
./scripts/p11_truth_rebuild.py:370:    res = rebuild_partition(Path(args.data_root), item, truth,
                                           drop_dates=drop_dates or None)
```

### E-13. dry-run 零写盘（sha256 前后快照）

```
快照分区数 = 27
  ag0: 9 个分区
  au0: 9 个分区
  m0: 9 个分区

# 全部操作后复算
分区数 before/after = 27 / 27
内容变化: 无 ✅
新增/丢失: 无 ✅

=== mtime 交叉验证（应全部早于基线 2026-08-30 13:38:24）===
$ find data/raw/processed/ag0 data/raw/processed/au0 data/raw/processed/m0 -name "*.parquet" -newermt "2026-08-30 13:38:24"
(空)
$ find data/raw/processed -newer .seq3qa_marker -name "*.parquet"
(空)
```

覆盖的操作：9 年 dry-run ×2 轮、6 个探针脚本、3 轮 pytest。生产湖 27 分区 sha256 逐字节未变。

### 全 9 年 dry-run 汇总（full truth，`--drop-uncovered`）

```
################ ag0/2018 ################
[PRECHECK] 既有分区 243 行：将替换 243 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2019 ################
[PRECHECK] 既有分区 244 行：将替换 244 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2020 ################
[PRECHECK] 既有分区 243 行：将替换 243 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2021 ################
[DROP-CHECK] 待删日期 2 个（参考日历：15/15 个健康品种 2021 年并集，243 个交易日）
[DROP-CHECK]   #  date        ①真值未覆盖  ②非交易日  ③k==1      O/H/L/C                     k
[DROP-CHECK]   1  2021-05-03  ✅           ✅         ✅         5473.0/5473.0/5330.0/5333.0  1.000000000
[DROP-CHECK]   2  2021-10-01  ✅           ✅         ✅         4720.0/4731.0/4595.0/4615.0  1.000000000
[DROP-CHECK] ag0/2021: 三重校验全中 ✅，将剔除 2 个幽灵行（分区 245 → 243 行后再替换）
[PRECHECK] 既有分区 245 行（剔除 2 幽灵行后 243 行）：将替换 243 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2022 ################
[DROP-CHECK] 待删日期 1 个（参考日历：15/15 个健康品种 2022 年并集，242 个交易日）
[DROP-CHECK]   1  2022-04-04  ✅           ✅         ✅         5034.0/5074.0/5016.0/5038.0  1.000000000
[DROP-CHECK] ag0/2022: 三重校验全中 ✅，将剔除 1 个幽灵行（分区 243 → 242 行后再替换）
[PRECHECK] 既有分区 243 行（剔除 1 幽灵行后 242 行）：将替换 242 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2023 ################
[PRECHECK] 既有分区 242 行：将替换 242 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2024 ################
[FAIL] --keep-raw-close：真值 242 行中 112 个日期在既有分区中不存在（首个 2024-07-18，末个 2024-12-31）—— 禁止填 NaN / 禁止回退到 adj 复制品，中止（零写盘）。请检查真值拉取区间是否与既有分区交易日一致
rc=1                            ← 用全量真值会正确中止；序 3 必须用 trim130
################ ag0/2025 ################
[PRECHECK] 既有分区 243 行：将替换 243 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
################ ag0/2026 ################
[PRECHECK] 既有分区 156 行：将替换 156 行 + 追加 0 行（真值全覆盖 ✅）   rc=0
```

2024 使用 trim130 真值：
```
################ ag0/2024 with TRIMMED truth, --drop-uncovered ################
[PARSE] 目标真值 130 行，2024-01-02 ~ 2024-07-17
[RAW-CLOSE] ag0: 从既有分区逐行继承 raw_close 130 行（跳过 enrich_raw_close，唯一正确列零损伤）
[DROP-CHECK] 待删日期 1 个（参考日历：15/15 个健康品种 2024 年并集，242 个交易日）
[DROP-CHECK]   1  2024-06-10  ✅           ✅         ✅         7359.0/8994.0/7359.0/8177.0  1.000000000
[DROP-CHECK] ag0/2024: 三重校验全中 ✅，将剔除 1 个幽灵行（分区 131 → 130 行后再替换）
[PRECHECK] 既有分区 131 行（剔除 1 幽灵行后 130 行）：将替换 130 行 + 追加 0 行（真值全覆盖 ✅）
rc=0

################ ag0/2024 TRIMMED, 不带 --drop-uncovered ################
[FAIL] 既有分区 131 行中 1 个日期未被真值覆盖（首个 2024-06-10）—— 替换将残留脏行，中止（零写盘）。请检查真值拉取区间/品种是否完整；若确认是节假日幽灵行，可加 --drop-uncovered 走三重校验剔除
rc=1                            ← 不加开关时仍一律中止，零回归 ✅
```

### F. raw_close 完整性审计（`artifacts/_seq3qa_probe_f_rawclose.py`）

```
年        行数     非整数   <=0  NaN      整数%       min       max  非整数样例
2018    243       0     0    0   100.0%   3396.00   3926.00
2019    244       0     0    0   100.0%   3500.00   4829.00
2020    243       0     0    0   100.0%   2937.00   6635.00
2021    245       2     0    0    99.2%   4600.00   5939.00  2021-01-04=5698.458576; 2021-01-14=5196.516642
2022    243       0     0    0   100.0%   4021.00   5403.00
2023    242       2     0    0    99.2%   4776.00   6209.00  2023-01-09=5256.110301; 2023-11-02=5824.892902
2024    131       1     0    0    99.2%   5753.00   8539.00  2024-07-04=8016.082994
2025    243       0     0    0   100.0%   7608.00  18319.00
2026    156       0     0    0   100.0%  13545.00  30891.00
[汇总] ag0 非整数 raw_close 合计 5 行

m0:  非整数 5 行（2019×3、2020×1、2023×1）
au0: 1924 行非整数 —— 但 au 报价含 2 位小数（0.02 元/克变动价位），故非整属正常，不计入缺陷
对照：rb0 = 0 / 2098 行非整数；cu0 = 7 / 2098

raw_close 零值/NaN 扫描（--keep-raw-close 会静默继承 → k=inf 的风险）：
  ag0: 1990 行, raw_close==0 的 0 行, NaN 的 0 行
  au0: 1990 行, raw_close==0 的 0 行, NaN 的 0 行
  m0:  1989 行, raw_close==0 的 0 行, NaN 的 0 行
```

### G. 复权口径判别（P2-8 证据）

```
--- 2019 ---
               n     k_std  k_nuniq      d_std  d_nuniq
dominant_id
AG1906.SHF    82  0.001406        5  17.802546       70
AG1912.SHF   128  0.000448        4  79.358811      118
AG2002.SHF    30  0.000000        1  12.098657       28
AG2006.SHF     4  0.000302        4   5.035187        4
  结论倾向: k(比率) 段内恒定? False   (adj-raw)(差额) 段内恒定? False

--- 2021 ---
AG2102.SHF     2  0.000609        2   8.693071        2
AG2106.SHF    87  0.003603       19  60.847412       84
AG2112.SHF   127  0.002598        7  77.068913      122
AG2206.SHF    27  0.003232        5  21.716276       25
  结论倾向: k(比率) 段内恒定? False   (adj-raw)(差额) 段内恒定? False
```

### 备份完整性校验

```
备份文件样例: ['ag0\\1d\\2018.parquet', 'ag0\\1d\\2019.parquet', 'ag0\\1d\\2020.parquet']
备份校验: 一致 27, 不一致 0, 缺失 0  (共 27)
✅ 备份可用作回滚点
```

### 复核用临时产物（未 commit，可删）

- `artifacts/_seq3qa_probe_a_scale.py`、`_seq3qa_probe_b_indep.py`、`_seq3qa_probe_d_diff.py`、`_seq3qa_probe_e_table.py`、`_seq3qa_probe_f_rawclose.py`、`_seq3qa_probe_g_anomaly.py`
- `artifacts/_seq3qa_lake_snapshot_before.json`（复核前 27 分区 sha256 快照）
- `artifacts/_seq3qa_dryrun_ag0_2019.txt`、`artifacts/_seq3qa_dryrun_all_years.txt`、`artifacts/_seq3qa_pytest1.txt`

HEAD 导出树与差分临时目录已在复核结束时清理。
