# cu0/ni0 复权污染止血 + 融合脚本根因护栏（2026-09-03，P0）

> 交付人：寇豆码（Kou）｜严重级别：P0 生产事故｜状态：已止血 + 根因护栏已上
> 影响面：主湖 `data/raw/processed/{cu0,ni0}/1d/2026.parquet` 共 **20 个格子**
> 关联：`deliverables/tier2_backup_channel_20260903.md`（同一事故的备源通道侧）

---

## 0. 一句话结论

tqsdk 在换月窗口内给出的后复权价带有 seam 误差，`p6_4_fill_gaps.py` 的
`drop_duplicates(subset="datetime", keep="last")` 让这份误差**整格覆盖**了主湖
既有的后复权序列，造成同一主力段内 `k = adj_close / raw_close` 出现 0.2% 级漂移。
已按白名单回滚 20 格（G1–G5 全判据 ALL PASS），并在融合脚本加装根因护栏：
**既有日期只许覆盖 `open_interest`，禁止覆盖 OHLC/adj_close**。

---

## 1. 污染机理

### 1.1 数据流

```
refresh_pull_local.py（tqsdk 主路）
    └─> artifacts/p6_4_pull_20260903/{sym0}.json     # 每次都带完整 10 日窗口
p6_4_apply_persisted_dir.py --dir ... --trading-day
    └─> p6_4_fill_gaps.py --stage parse <json> --sym XX --seg ... --force --scale 1.0
            ├─ normalize_new_df(...)   → 新帧（10 日窗口：08-21 ~ 09-03）
            ├─ enrich_raw_close(...)   → 备源回填名义价（raw_close）
            └─ merged = concat([old, new]).drop_duplicates("datetime", keep="last")
                       ▲▲▲ 越权覆盖点
```

### 1.2 `keep="last"` 为什么是越权

`old` 是主湖**存量**后复权序列（累积状态量），`new` 是本次拉取的 10 日窗口。
二者在 08-21 ~ 09-01 这 8 个交易日**完全重叠**。

- `pd.concat([old, new])` → 旧行在前、新行在后；
- `drop_duplicates(subset="datetime", keep="last")` → **新帧整行胜出**；
- 于是新帧的 `open/high/low/close/adj_close` 全部覆盖主湖既有值。

问题在于：**新帧的 08-21 / 08-24 两日后复权价本身是错的**（tqsdk 换月窗口
seam 误差），而 08-25 及之后是对的。融合脚本没有任何"新旧谁更可信"的判据，
`keep="last"` 把错误的两格一并写进了主湖。

### 1.3 为什么后复权污染不可自愈

后复权序列是**累积状态量**：`adj_close = k × raw_close`，其中 `k` 在一个主力
段内是常数，换月时跳变。一旦某一格的 `adj_close` 被写错，等价于在段内插入一个
假的 `k` 跳变；后续所有依赖该序列的收益率、因子、信号缓存都会被这个假跳变污染，
且**不会**因为下一次刷新而自动修正（下一次刷新同样只覆盖尾部窗口）。

---

## 2. 三向证据链（复核结论：与 team-lead 判断一致，且我补了一条更强的）

### 证据 1 —— 名义价干净（污染不在名义侧）

用 `BackupRawFetcher(sources=("sina",), save=False, cross_check=False)` 拉取
cu0/ni0 名义价，与主湖 `raw_close` 逐日比对：

| 品种 | 共同交易日 | 最大绝对偏差 |
|------|-----------|-------------|
| cu0  | 15 天（08-14 ~ 09-03） | `0.000000e+00` |
| ni0  | 15 天（08-14 ~ 09-03） | `0.000000e+00` |

→ `raw_close` 完全干净，**污染只可能来自复权因子侧**。
（脚本：`artifacts/_tmp/forensics_nominal.py`，输出 `forensics_nominal.txt`）

### 证据 2 —— 旧值 k 自洽，新值不自洽（本次新增的最强证据）

`k = adj_close / raw_close`。注意 **08-20 → 08-21 本身就是一个主力段边界**
（旧值 k 从 `1.472691834` 跳到 `1.475842123`，即换月 spread 因子 ≈ 1.0021391）。
因此 08-21 与 08-25~09-01 属于**同一个主力段**，段内 k 必须恒定到浮点噪声级。

**cu0**

| 日期 | 旧 k | 新 k | 相对漂移 |
|------|------|------|---------|
| 08-14 ~ 08-20 | 1.472691834（恒定） | 1.472691834 | 0 |
| **08-21** | **1.475842169** | **1.478999153** | **+0.2139%** |
| **08-24** | **1.475842091** | **1.478577445** | **+0.1853%** |
| 08-25 ~ 09-01 | 1.475842123（恒定） | 1.475842125 | +1.1e-9（噪声） |

**ni0**（同构，方向相反）

| 日期 | 旧 k | 新 k | 相对漂移 |
|------|------|------|---------|
| 08-14 ~ 08-19 | 1.228266118（恒定） | 1.228266118 | 0 |
| 08-20 | 1.225704465 | 1.225704465 | 0 |
| **08-21** | **1.225704489** | **1.223237877** | **−0.2012%** |
| **08-24** | **1.225704451** | **1.223156026** | **−0.2079%** |
| 08-25 ~ 09-01 | 1.225704463（恒定） | 1.225704465 | +1.5e-9（噪声） |

判定：
- **旧值**：段内 k 恒定到 1e-8 级 → 数学自洽；
- **新值**：同一段内 k 在 08-21/08-24 漂移 0.2% 后又在 08-25 跳回段内常数
  → **不自洽**，是典型的"换月窗口过渡期复权因子被多算/少算一次"的 seam 形态。

### 证据 3 —— 反算验证（旧值可复原，新值不可）

以主湖 `raw_close`（已由证据 1 证明干净）为基准反算：

| 品种 日期 | 主湖 raw_close | k_old × raw_close | 备份 close（旧值） | 主湖 close（新值） |
|-----------|---------------|-------------------|-------------------|-------------------|
| cu0 08-21 | 107520.00 | **158682.55** | **158682.55** ✅ | 159021.99 ❌ |
| cu0 08-24 | 107910.00 | **159258.12** | **159258.12** ✅ | 159553.29 ❌ |
| ni0 08-21 | 129200.00 | **158361.02** | **158361.02** ✅ | 158042.33 ❌ |
| ni0 08-24 | 129860.00 | **159169.98** | **159169.98** ✅ | 158839.04 ❌ |

旧值可以**精确复原**（误差 0），新值相对旧值偏离 ±0.2%。

### 反向证据排查

我特意检查了"新值才是对的"的可能性，两条都被排除：

1. **会不会 raw_close 才是脏的、新 adj_close 是对的？**
   → 不会。证据 1 已证明 `raw_close` 与 sina 名义价 15 天零偏差。
2. **会不会 08-21/08-24 属于旧段（换月发生在 08-25）、所以新 k 才对？**
   → 不会。旧值 k 的跳变点在 **08-20 → 08-21**（1.472691834 → 1.475842123），
   08-21 与 08-25~09-01 同段；若换月在 08-25，旧值 k 应当在 08-25 跳变，实测没有。
   而新值的 k 在 08-21 → 08-24 → 08-25 三级台阶（1.478999 → 1.478577 → 1.475842），
   一段之内三个 k，物理上不成立。

**结论：旧值为真、新值为假，回滚方向正确，无矛盾证据。**

---

## 3. P0-A 回滚清单（恰好 20 格）

| 品种 | 日期 | 列 | 回滚前（污染值） | 回滚后（真值） | Δ% |
|------|------|----|-----------------|---------------|-----|
| cu0 | 2026-08-21 | open | 157442.837884 | 157077.310000 | +0.2327 |
| cu0 | 2026-08-21 | high | 159243.365277 | 158903.450000 | +0.2139 |
| cu0 | 2026-08-21 | low | 157250.978408 | 156915.310000 | +0.2139 |
| cu0 | 2026-08-21 | close | 159021.988958 | 158682.550000 | +0.2139 |
| cu0 | 2026-08-21 | adj_close | 159021.988958 | 158682.550000 | +0.2139 |
| cu0 | 2026-08-24 | open | 159627.084229 | 159405.710000 | +0.1389 |
| cu0 | 2026-08-24 | high | 159804.185284 | 159464.740000 | +0.2129 |
| cu0 | 2026-08-24 | low | 158771.095797 | 158490.690000 | +0.1769 |
| cu0 | 2026-08-24 | close | 159553.292123 | 159258.120000 | +0.1853 |
| cu0 | 2026-08-24 | adj_close | 159553.292123 | 159258.120000 | +0.1853 |
| ni0 | 2026-08-21 | open | 157380.453249 | 157968.790000 | −0.3724 |
| ni0 | 2026-08-21 | high | 158581.643625 | 158888.070000 | −0.1929 |
| ni0 | 2026-08-21 | low | 156620.516481 | 156975.970000 | −0.2264 |
| ni0 | 2026-08-21 | close | 158042.333660 | 158361.020000 | −0.2012 |
| ni0 | 2026-08-21 | adj_close | 158042.333660 | 158361.020000 | −0.2012 |
| ni0 | 2026-08-24 | open | 158532.615446 | 158667.440000 | −0.0850 |
| ni0 | 2026-08-24 | high | 160812.425750 | 161155.620000 | −0.2130 |
| ni0 | 2026-08-24 | low | 158017.819571 | 158397.790000 | −0.2399 |
| ni0 | 2026-08-24 | close | 158839.041562 | 159169.980000 | −0.2079 |
| ni0 | 2026-08-24 | adj_close | 158839.041562 | 159169.980000 | −0.2079 |

### 明确保留（不回滚）

| 项 | 处置 | 理由 |
|----|------|------|
| `raw_close` | 不动 | 证据 1 已证 15 天零偏差 |
| `volume` / `amount` | 不动 | 本次融合未标记问题 |
| `open_interest`（08-21 ~ 09-01，16 格） | **保留新值** | P2-4 方案 A 授权的持仓量修正 |
| 08-25 ~ 09-01 的 1.1e-9 级 k 噪声（60 格） | **保留新值** | 浮点噪声，非语义污染；回滚到快照反而制造无谓 diff |
| 09-02 / 09-03 新增行 | **保留** | 本次融合的正常增量 |
| rb0 / hc0 备源续接段 | **不触碰** | 与本次事故无关（Tier-2 备源通道产出） |

### 执行方式

新增**显式修复脚本** `scripts/rollback_adj_cells.py`：

- 默认 dry-run，必须显式 `--apply` 才写盘；
- 只改白名单内的 20 格，其余一律不动；
- 写前先把主湖 cu0/ni0 整目录备份到
  `artifacts/_p2_backup_20260903_pre_rollback/`（各 10 个文件，含 manifest.json）；
- 写用 `tmp + os.replace` 原子替换（G5），**无 `shutil.move`、无删除调用**；
- 每次执行落一份取证 JSON（改前/改后/Δ%/来源快照路径）到
  `artifacts/_tmp/rollback_adj_cells_<ts>.json`；
- 写后复核：目标格 == 快照值，且 `raw_close/volume/amount/open_interest`
  逐位未变（不一致直接 `RuntimeError` 中止并留痕）。

> 之所以走"显式修复脚本 + 取证 JSON"而不是让融合再覆盖回去：后复权是累积
> 状态量，任何隐式覆盖都会让污染不可追溯。见 §4。

---

## 4. P0-B 根因护栏设计

### 4.1 契约

新增 `scripts/p6_4_fill_gaps.py::merge_year_frames(old, new_year, *,
allow_price_overwrite=False)`，替换原 `concat + drop_duplicates(keep="last")`：

| 情形 | 行为 |
|------|------|
| **既有日期**（`old` 中已有该 `datetime`） | 整行保留 `old` 的值；**唯一例外**：`open_interest` 允许被新值覆盖 |
| **新日期** | 整行采用 `new_year`，全字段写入 |
| `--allow-price-overwrite` | 恢复旧行为（新帧可整行覆盖价格列），note 大声留痕 |

### 4.2 关键设计决策

1. **允许覆盖 `open_interest` 是硬要求，不是妥协。**
   P2-4 方案 A 明确授权用新数据修正持仓量；若把持仓量也一起保护了，
   等于把已经修好的 16 格 OI 又退回错误值。护栏的列白名单因此是
   `MERGE_OI_ONLY_COLUMNS = ("open_interest",)`，受保护列是
   `MERGE_PROTECTED_COLUMNS = ("open", "high", "low", "close", "adj_close")`。
   `raw_close` 既不在可覆盖名单也不在受保护名单 → 天然保持旧值（名义价已有
   独立的 P1-c 回填通道负责）。

2. **R22：口径不可用 → 放弃护栏、退回既有行为，绝不新增停摆失效模式。**
   以下任一情形触发回退，并在返回的 `note` 里写明归因（静默回退也是事故）：

   | # | 触发条件 | note |
   |---|---------|------|
   | 1 | `old` 或 `new_year` 为空 | 无需生效（无重叠行可保护） |
   | 2 | 任一侧缺 `MERGE_GUARD_REQUIRED_COLUMNS` 中任一字段 | 必需字段缺失 `[...]` |
   | 3 | `datetime` 归一化抛异常 | datetime 口径不可用：`<ExcType>` |
   | 4 | `old` / `new` 的 `datetime` 含 `NaT` | 旧帧/新帧 datetime 含 NaT |
   | 5 | `old` / `new` 的 `datetime` 有重复 | 旧帧/新帧 datetime 有重复 |
   | 6 | `new_year.open_interest` 全不可解析 | 新帧 open_interest 全不可解析 |
   | 7 | `old` 的保护列全不可解析 | 旧帧保护列（OHLC/adj_close）全不可解析 |

   回退路径**不抛异常**，返回既有 `keep="last"` 的结果。这样护栏只会让融合
   "少覆盖"，永远不会让融合"跑不起来"。

3. **NaT 是"日期口径不可用"的真实形态，不是垃圾字符串。**
   上游 `normalize_new_df` 用 `pd.to_datetime(..., errors="coerce")`，坏日期会
   变成 `NaT` 而非抛异常。因此判据 4 以 `NaT` 为主（这是写测试时实测发现的，
   最初按"不可解析字符串"设计，实测该分支不可达）。

4. **确需修 OHLC 时走显式通道，不靠融合隐式覆盖。**
   新增 `--allow-price-overwrite` 开关（默认关闭），CLI help 明确标注
   "仅用于取证后的显式修复，日常刷新/apply 严禁使用"。配合
   `scripts/rollback_adj_cells.py` 的取证 JSON，形成"谁在什么时候、基于什么
   证据、改了哪些格子"的完整链条。

5. **护栏结论落进 `p6_4_applied.json`。**
   每条 applied 记录新增 `merge_guard` 字段（**追加键**，既有键不变），
   记录"保护 N 个既有日期 / 覆盖 M 行 open_interest / 新增 K 个日期"或回退归因，
   供事后审计。同时 `stage_parse` 逐品种打印 `[GUARD]` 行。

6. **不改变既有备份与原子写能力。** `backup_year_file` 与 `tmp.replace()`
   完全保留；`coerce_schema` 未改动。

### 4.3 为什么护栏要装在融合层而不是拉取层

- 拉取值本身没错——tqsdk 的 10 日窗口是对的，错的是"用它覆盖存量"这个动作；
- 装在融合层可以同时保护 p6-4 历史补洞、日常 apply、以及未来任何新拉取源；
- 装在拉取层则每个源都要各自实现一遍，且挡不住"源对了但融合越权"的情形。

---

## 5. 验证结论

### 5.1 回滚前后逐位校验（`artifacts/_tmp/validate_rollback.py`）

| 判据 | 内容 | cu0 | ni0 |
|------|------|-----|-----|
| G1 | 变化集恰好等于授权清单（5 列 × 2 日期 = 10 格/品种） | PASS | PASS |
| G2 | 其余格与回滚前逐位一致（含 1.1e-9 噪声、raw_close、volume、OI） | PASS | PASS |
| G2b | 08-25~09-01 融合噪声保留（未误回滚），6/6 天 | PASS | PASS |
| G3 | 列集合/列顺序/dtype/行数(160)/行序与回滚前完全一致 | PASS | PASS |
| G4 | 09-02 / 09-03 新增行存在且与回滚前逐位一致 | PASS | PASS |
| G5 | 目标格 == 权威快照 `_p2_backup_20260903`（10/10 格） | PASS | PASS |
| 附加 | 冻结列（raw_close/volume/amount/open_interest）零改动 | PASS | PASS |
| 附加 | 段内 k 相对跨度 < 1e-6 | 5.303e-08 PASS | 3.118e-08 PASS |

**总判据：ALL PASS。** 污染前段内 k 跨度为 0.21%（cu0）/ 0.21%（ni0），
回滚后为 5.3e-8 / 3.1e-8 —— 降低 4 个数量级，序列重新自洽。

### 5.2 p22 重建（`--trading-day`，EXIT=0）

```
[KLINE] 融合完成：成功 18 / 失败 0
[P22]  重建信号缓存尾折（p22_tail_ext --force）...
[OK]   tail_ext 缓存 9439 条（v8 8624 + 尾 815）
[DONE] P22-1 尾折扩展研究实验完成。
[OK]   P步-A/B 完成：K线融合 + 信号缓存重建
EXIT=0
```

**关键验证**：重建是在护栏开启的情况下跑的，`p6_4_applied.json` 里 18 条记录
全部留下 `merge_guard` 留痕，例如

```
cu0 2026 | 护栏生效：保护 160 个既有日期的 OHLC/adj_close，仅覆盖 10 行 open_interest；新增 0 个日期整行写入
ni0 2026 | 护栏生效：保护 160 个既有日期的 OHLC/adj_close，仅覆盖 10 行 open_interest；新增 0 个日期整行写入
```

重建后重跑 §5.1 的逐位校验 → **ALL PASS**，20 格回滚结果在重建中幸存。
（若无 P0-B 护栏，这一步会把污染值原样写回去 —— 顺序不可颠倒。）

### 5.3 缓存健康度（`artifacts/_tmp/cache_health_check.py`，只读）

```
[health] main cache: rows=9439 symbols=18 ts_max=2026-09-03
[health]   NaN counts: symbol=0, ts=0, p_up=0, exp_ret=0, is_effective=0
[health]   duplicate (symbol,ts) rows: 0
[health]   is_effective=True: 9129 / 9439

    sym     latest ts         lag      gate  p_up
    ag0    2026-09-03           0      PASS  0.266667
    au0    2026-09-03           0      PASS  0.999999
    cu0    2026-09-03           0      PASS  0.066667
    hc0    2026-09-03           0      PASS  0.133333
     m0    2026-09-03           0      PASS  0.000001
    ni0    2026-09-03           0      PASS  0.766667
    rb0    2026-09-03           0      PASS  0.566667

[health] blocked symbols: 0/18
```

→ **lag=0，0/18 被阻断**。

### 5.4 全量回归（无路径参数，junit 解析值）

| 口径 | tests | failures | errors | skipped |
|------|-------|----------|--------|---------|
| **最终（含本次新增）** | **1264** | **0** | **0** | 0 |
| 排除本次新增文件 | 1241 | 0 | 0 | 0 |
| **本次净增** | **+23** | 0 | 0 | 0 |

⚠️ **基线口径差异（重要）**：team-lead 给的基线是 1188，我在当前 HEAD 实测
改动前为 **1241**。差异 53 可完整归因到 4 个尚未计入 1188 的历史提交：

| 提交 | 内容 | 测试增量 |
|------|------|---------|
| `09ac4a8` | feat(data): tqsdk 持仓量改用 close_oi（P2-4） | +15（与 `9e80778` 合计） |
| `9e80778` | test(p2-4): 补 tqsdk OI 口径反例契约用例 | ↑ |
| `a31299f` | feat(p0-backup): Tier-2 备源通道（我的上一轮） | +20 |
| `3da8662` | fix(p0-backup): QA fresh-eyes 三道 fail-closed 护栏 | +18 |
| — | **合计** | **+53** |

1188 这个数字来自 `4a90503` 的提交信息原文「新增 4 用例，全量 1188」，
即它是**那个提交时点**的计数，之后 4 个提交又加了 53 例。
**本次改动相对当前 HEAD 的净增是 +23、0 失败、0 错误**。

> 回归执行注意：不要只看终端末行汇总。`pytest-of-Administrator/garbage-*`
> 临时目录堆积会触发 safe-delete 钩子，把汇总行吞掉并让 `EXIT=1`。
> 必须以 `junitxml` 的 `testsuite` 属性为准（本表全部为 XML 解析值）。

---

## 6. 执行时间线（2026-09-03）

| 时刻 | 动作 | 结果 |
|------|------|------|
| 17:41 | 只读取证：备份 vs 主湖逐格 diff、k 序列复算、反算验证 | 确认 20 格污染，与 team-lead 判断一致 |
| 17:43 | sina 名义价交叉核对（证据 1） | 15 天 × 2 品种，最大偏差 `0.000000e+00` |
| 17:45 | **P0-B 护栏**落地 `scripts/p6_4_fill_gaps.py`（**先于回滚**，保证重建不会再次污染） | — |
| 17:45 | 写前备份 → `artifacts/_p2_backup_20260903_pre_rollback/`（cu0/ni0 各 10 文件） | — |
| 17:45 | **P0-A 回滚 20 格**（`scripts/rollback_adj_cells.py --apply`，tmp+os.replace） | 取证 JSON `artifacts/_tmp/rollback_adj_cells_20260903_174558.json` |
| 17:46 | 回滚后 G1–G5 逐位校验 | ALL PASS；段内 k 跨度 5.3e-8 / 3.1e-8 |
| 17:47 | 新增护栏测试 23 例（含 8 个 R22 回退负例） | 23 passed |
| 17:52 | p22 重建 `p6_4_apply_persisted_dir.py --dir artifacts/p6_4_pull_20260903 --trading-day` | 18/18，EXIT=0，9439 条缓存 |
| 18:02 | 重建后复验 G1–G5 | ALL PASS（20 格幸存） |
| 18:05 | 缓存健康度 | lag=0，0/18 blocked |
| 18:10 | 全量回归（最终代码） | 1264 passed / 0 failed / 0 errors |
| 18:12 | 三分离提交（feat / test / docs）+ `git fsck --no-dangling` | 见 §9 |

---

## 7. 改动文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `scripts/p6_4_fill_gaps.py` | feat | 新增 `merge_year_frames` + `MERGE_*` 常量 + `--allow-price-overwrite`；`stage_parse` 接入护栏并打印 `[GUARD]`、applied 追加 `merge_guard` |
| `scripts/rollback_adj_cells.py` | feat（新增） | P0-A 显式修复脚本：白名单逐格回滚 + 写前备份 + 原子写 + 取证 JSON |
| `tests/test_p6_4_merge_guard.py` | test（新增） | 23 例：护栏正例 / R22 回退负例 / 显式放行 / stage_parse 端到端 / 无删除调用 AST 红线 |
| `deliverables/cu_ni_rollback_20260903.md` | docs（新增） | 本文档 |

---

## 8. 残留风险

| ID | 风险 | 处置 / 建议 |
|----|------|------------|
| R1 | tqsdk 换月窗口 seam 误差的根因未消除，只是被护栏挡住 | 护栏是"不许覆盖"，不是"源变对"。建议下一步做 `rollover_spreads` 精确化，或改用 tqsdk 的 `get_kline_serial` + 显式换月表重建后复权 |
| R2 | `--allow-price-overwrite` 是后门，误用会绕过护栏 | 每次使用都会在 `[GUARD]` 行与 `p6_4_applied.json` 留痕；建议后续加"使用后 24h 内必须补交付说明"的纪律 |
| R3 | 护栏按"整年分片"粒度回退，单个坏日期会让该年文件失去保护 | 实测触发条件（NaT / 重复日期 / 缺列）在生产数据上均不成立；若将来触发，`[GUARD]` 行会大声打印，不会静默 |
| R4 | 09-02 / 09-03 的 `raw_close` 由 sina 回填，若 sina 当晚停更会滞后一天 | 与 Tier-2 备源通道同一风险（见 `tier2_backup_channel_20260903.md` R1） |
| R5 | 08-25 ~ 09-01 保留的 1.1e-9 级 k 噪声是本次融合引入的浮点误差 | 相对量级 1e-9，远低于任何信号阈值；如需彻底清除，需显式重算整个主力段（走 §4.2-4 的显式通道） |
| R6 | 回滚脚本目前白名单硬编码在 `DEFAULT_PLAN` | 已支持 `--symbol` / `--date` 覆盖，未来同类事故可直接复用 |

---

## 9. 提交清单（feat / test / docs 三分离，未 push）

| # | Hash | 类型 | 内容 | diffstat |
|---|------|------|------|----------|
| 1 | `66bd61a` | feat | `scripts/p6_4_fill_gaps.py`（P0-B 护栏）+ `scripts/rollback_adj_cells.py`（P0-A 显式修复，新增） | 2 files, +506 / −8 |
| 2 | `5cb1691` | test | `tests/test_p6_4_merge_guard.py`（新增，23 例） | 1 file, +468 |
| 3 | `1887c2b` | docs | `deliverables/cu_ni_rollback_20260903.md`（新增） | 1 file, +381 |
| 4 | （本节） | docs | 补提交清单与 §6 引用修正 | 1 file |

- 全部使用显式 `git add <path>`，**未使用** `-A` / `-u`；
- **未执行** `git gc` / `repack` / `prune`；
- `git fsck --no-dangling` → **输出为空（EXIT=0）**；
- **未 push**。

> 本表记录 P0-A/B 主体的四次提交。其后 §11 的文档更新与「`raw_close` 纳入
> 受保护列」的代码 / 测试变更属于后续裁决项，各自独立提交，见 `git log`。

### 未纳入版本控制的生产数据改动

主湖 `data/raw/processed/{cu0,ni0}/1d/2026.parquet` 的 20 格回滚不在 git 管理
范围内（`data/` 被忽略），落在临时备份与取证 JSON 中：

- `artifacts/_p2_backup_20260903_pre_rollback/{cu0,ni0}/1d/`（回滚前，各 10 文件）
- `artifacts/_tmp/rollback_adj_cells_20260903_174558.json`（逐格改前/改后/Δ%）
- `artifacts/_tmp/validate_rollback.txt` / `validate_rollback2.txt`（G1–G5 判据输出）
- `artifacts/_tmp/forensics_cu_ni.txt` / `forensics_nominal.txt`（三向证据链原始输出）

---

## 10. 全局一致性复核（IS_PASS）

| 检查项 | 结果 |
|--------|------|
| 跨文件 import 一致性（无循环依赖、无缺失 import） | PASS |
| 接口契约：`merge_year_frames(old, new, *, allow_price_overwrite)` 调用方签名一致 | PASS |
| `stage_parse` 新增关键字参数有默认值，既有调用方不受影响 | PASS |
| 数据流：`coerce_schema` → `merge_year_frames` → `tmp.replace` 类型与列序守恒 | PASS |
| 无重复实现（`merge_year_frames` 是 `drop_duplicates` 的唯一封装点） | PASS |
| 删除类红线审计 `audit_no_delete_calls('hexbroker')` → 0 违规 | PASS |
| AST 扫描两个改动脚本零 `unlink`/`remove`/`rmtree`/`move` | PASS |
| 无 `pass` / `TODO` / `...` 占位 | PASS |
| 全量回归 1264 passed / 0 failed / 0 errors（junit 解析值） | PASS |

**IS_PASS: YES**

---

## 11. 名义价保护契约（`raw_close` 纳入受保护列）

> 本节记录 **P2 裁决项**：把 `raw_close` 从「隐式受保护」升级为「显式契约 + 测试锁定」。
> 裁决人：主理人；定性：**P2、当晚执行**；状态：**待 GO**（QA 正在对同一文件做变异测试）。

### 11.1 威胁等级 0：Tier-2 备源通道不是写入者

`SAFE_COLS` 在两处定义且一致（`refresh_pull_local.py:117`、
`p6_4_apply_persisted_dir.py:76`）：

```python
SAFE_COLS = ["date", "underlying_symbol", "open", "high", "low", "close",
             "volume", "open_interest"]          # ← 没有 raw_close
```

而备源通道（Tier-2，`a31299f`）的发射循环（`refresh_pull_local.py:437`）只遍历
graft 产出的 `new_dates`：

```python
for d in new_dates:        # 仅新增日，既有日期一个都不发射
```

→ **备源通道既不产出 `raw_close` 字段，也不发射既有日期**，对存量日名义价的
**威胁等级 = 0**。这一条决定了本项可以判 P2 而非 P0/P1。

### 11.2 真凶链路：主路 tqsdk 才是把名义价带到存量日的那只手

```
refresh_pull_local.py:724
    emit_dates = sorted(set(tail_dates) | set(ext_rows.index))
                            ▲ tail_dates = 对齐稳定段的【重叠日】= 湖里已存在的日期
        ↓ json（含存量日，但 SAFE_COLS 无 raw_close）
p6_4_apply_persisted_dir.py:398
    → p6_4_fill_gaps.py --stage parse --force --scale 1.0
        ↓
p6_4_fill_gaps.py:869   enrich_raw_close(new_df, sym0)
        ↓ 对 new_df 的【每一行】用 sina/akshare 回填名义价（含存量日）
p6_4_fill_gaps.py  merge_year_frames(old, new_year)
        ↓ P0-B 之前：drop_duplicates(keep="last") → sina 的 raw_close 整格覆盖存量日
        ↓ P0-B 之后：整行保留 old → raw_close 隐式保住
```

**本次 cu0/ni0 事故的 08-21 ~ 09-01 正是沿这条链路进来的**——只不过 `raw_close`
恰好与湖内值零偏差（§2 证据 1：sina vs 主湖 15 天 × 2 品种，偏差
`0.000000e+00`），所以只污染了复权侧，名义侧侥幸没出事。

> ⚠️ **那是运气，不是保证。** sina 停更或口径变更时，同一条链路会静默改写
> 名义价；而项目铁律是「风控 / 保证金 / 敞口一律用名义价 `raw_close`」
> （反例：ag0 k=0.6839，用错即低估敞口 1.46×）。名义价被静默改写的爆炸半径
> **大于**复权价。

### 11.3 隐式保护 vs 显式契约：三条暴露面

`raw_close` 既不在 `MERGE_OI_ONLY_COLUMNS` 也不在 `MERGE_PROTECTED_COLUMNS`。
它当前安全，靠的是护栏的实现方式「**整行保留 `old`，只放行 `open_interest`**」
—— 是**副作用**，不是契约。

| # | 暴露面 | 后果 |
|---|--------|------|
| 1 | R22 的 7 条回退判据任一触发（空帧 / 缺列 / datetime 归一化异常或含 NaT 或重复 / 新帧 OI 全 NaN / 旧帧保护列全 NaN） | 护栏**整体弃守** → `raw_close` 连同 OHLC 一起被 sina 值覆盖 |
| 2 | `--allow-price-overwrite` | 同上 |
| 3 | 将来有人扩 `MERGE_OI_ONLY_COLUMNS`、或把实现改成「新值非空即用新值」 | `raw_close` 静默失守，而常量断言 `test_guard_column_contract` **不会报警**（它只断言 OI_ONLY / PROTECTED 两个常量，`raw_close` 不在其中） |

**第 3 条最危险：没有任何测试锁住 `raw_close` 的受保护状态。**
（此项已同步 QA 作为变异点 M1：把 `MERGE_OI_ONLY_COLUMNS` 改成
`("open_interest", "raw_close")`，验证 23 例是否会漏检 —— 预期漏检。）

### 11.4 为什么纳入 `MERGE_GUARD_REQUIRED_COLUMNS` 不新增回退触发路径

把 `raw_close` 加进 `MERGE_PROTECTED_COLUMNS` 会**连带**把它加进
`MERGE_GUARD_REQUIRED_COLUMNS`（`REQUIRED = ("datetime",) + PROTECTED + OI_ONLY`）。
隐患：若某年分片缺 `raw_close` 列，会触发「必需字段缺失 → 护栏整体回退」，
对历史补洞路径（补 2018–2024 缺口）反而是**降级**。

**已验证此隐患不成立**——`stage_parse` 在调用护栏前对 `old` 与 `new_year`
都执行了 `coerce_schema`，而 `coerce_schema` 对缺失的 `raw_close` 有兜底
（`p6_4_fill_gaps.py:548-549`）：

```python
elif col in ("raw_close", "adj_close"):
    df[col] = df["close"] if "close" in df.columns else 0.0
```

→ `raw_close` 列**恒在**，纳入 REQUIRED **不会**新增「缺列 → 护栏弃守」这条
R22 回退触发路径。历史补洞路径同样安全。

### 11.5 变更清单（待 GO 后执行，最小变更）

| # | 位置 | 改动 |
|---|------|------|
| 1 | `MERGE_PROTECTED_COLUMNS` | 增加 `"raw_close"`（`MERGE_GUARD_REQUIRED_COLUMNS` 自动跟上） |
| 2 | 旧帧保护列全 NaN → 回退判据 | 统计口径须覆盖 `raw_close` |
| 3 | `note` 里的 `overwritten` 统计 | 同上，覆盖 `raw_close` 的 NaN 行 |
| 4 | `test_guard_column_contract` | 补 `raw_close` 常量断言 |
| 5 | `tests/test_p6_4_merge_guard.py` | **增 1 例**：构造新帧 `raw_close` 与旧帧不同 → 断言合并后旧帧 `raw_close` 逐位保留 |

其中 **第 5 条是本单的核心价值**——补的正是「没有任何测试锁住 `raw_close`」
这个洞，不可省略。

### 11.6 并发控制

QA 正在对 `scripts/p6_4_fill_gaps.py` 做变异测试，其 M1 / M2 变异点
（`MERGE_OI_ONLY_COLUMNS` / `MERGE_PROTECTED_COLUMNS`）**正是本节要改的行**。
为避免两种事故（QA 的「改坏→跑测试→还原」把改动一并还原；或改动后 QA 的
M1/M2 基线漂移致变异结果失去可比性），**本节变更须等主理人 GO 后落盘**，
硬底线 19:45。文档写作先行，因为 QA 复核产出落在
`deliverables/cu_ni_rollback_qa_review_20260903.md`，与本文件不重叠。
