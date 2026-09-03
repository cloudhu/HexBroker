# Tier-2 备源通道落地（2026-09-03 P0 生产事故修复）

> 实施：寇豆码（Engineer）｜编排/终裁：主理人
> 影响面：`scripts/refresh_pull_local.py`、`hexbroker/data/backup.py`、新增 20 用例
> 全量回归：**1223 passed / 0 failures / 0 errors**（基线 1203 → +20，零回归）

---

## 一、事故复述（结论沿用主理人取证，实施前已独立复核）

| 项 | 实测 |
|---|---|
| 现象 | 主湖 18 品种全部停滞于 2026-09-01，缺 09-02 / 09-03 |
| 直接阻塞 | `refresh_pull_local.py` 只产出 16/18 份 json（rb0 `SEAM_BASIS_CONFLICT`、hc0 `SCALE_UNSTABLE`） |
| 放大器 | `p6_4_apply_persisted_dir.py` 的 P1-a 体检：品种数 < 18 → 整体拒绝融合（护栏正确，未动） |
| 兜底死路 | pandadata MCP 未接线（`~/.workbuddy/mcp.json` = `{"mcpServers": {}}`） |
| 后果链 | 主湖不动 → p22 缓存 max ts 卡 09-01 → P3-C 新鲜度 lag=2 → 全品种信号判过期 → 模拟盘不开新仓 |

### 死锁机理（本次实施的关键认知）

主路的「对齐稳定段」从重叠区**末尾向前回溯**，要求逐日 `aligned` 且 `ratio` 稳定。
换月后新 k 段的长度取决于**湖已经前进了多少天** —— 而湖要前进又必须先通过判定：

- **rb0**：08-31 起主连换月，k 由 1.201347 跳到 1.181055，且 08-31 / 09-01 的
  tqs 与湖名义价偏差 **-1.69% / -1.61%**（远超 `RAW_ALIGN_TOL=0.005`）→ 这两天
  被逐出稳定段 → 新 k 段只能从 09-02 起算。即使湖补到 09-03，稳定段仍只有
  **2 天 < `MIN_OVERLAP`=3** → `SCALE_UNSTABLE`。**前进了也还差一天。**
- **hc0**：08-31 / 09-01 仍 `aligned=True`，新 k 段（2.192413）只有 2 天 →
  `SCALE_UNSTABLE`。湖补到 09-03 后稳定段变 4 天 → **湖先前进就能过**。

→ 前者是**硬死锁**，靠主路永远自愈不了；必须换数据源。

---

## 二、处置：接上项目早已有、但从未接线的 Tier 2 备源

`hexbroker/data/failover.py` 定义了三级切换，其中 **Tier 2 = `BackupRawFetcher`
（sina/akshare）拉名义价 + `graft_adjusted` 续接到湖内既有后复权序列**。
`refresh_pull_local.py`（2026-08-30 起取代 pandadata 成为主路径）**根本没有接入
这套机制** —— 这就是本次事故的根因：主路一遇上换月分歧就无路可走。

### 数据流

```
主路判定失败（SCALE_UNSTABLE / SEAM_BASIS_CONFLICT / NO_OVERLAP …）
   │
   ├─ 1) BackupRawFetcher(sources=("sina","akshare"), save=False, cross_check=False)
   │     拉**名义价**（窗口向前扩展 BACKUP_LOOKBACK_DAYS=60 天，保证与湖有重叠锚点）
   │     ⛔ save=False 硬约束：备源绝不污染主湖
   │
   ├─ 2) graft_adjusted(湖内后复权 close, 备源名义价, max_graft_days=10, tol=1e-6)
   │     ⛔ 严禁自己乘 k —— 换月时 k 会跳变，手工外推必错
   │
   ├─ 3) 按 pandadata 同口径落盘 artifacts/p6_4_pull_<asof>/<sym0>.json
   │     （close = 后复权；date = YYYYMMDD；underlying_symbol = 大写基础合约）
   │
   └─ 4) _SEAM_STATUS.json 记 BACKUP_SINA，_LOCAL_REPORT.md 增「来源」列
```

### 关键设计决策

| 决策 | 理由 |
|---|---|
| **只发射新增日**，不重发重叠日 | 备源只负责把湖往前推；重叠日本就在湖里且逐位一致（sina vs 湖 `raw_close` dev = 0.0） |
| **k 由 graft 结果反解**（`k_d = series[d] / raw[d]`），非手工外推 | 精确还原 graft 的分段因子；换月时会自动跳段，这正是「严禁自乘 k」的价值 |
| **OHLC 按同一 `k_d` 缩放** | 保证 `low ≤ open,close ≤ high` 包络不被破坏 |
| **备源帧必须带完整 OHLC**，缺列即判失败 | ⚠️ 关键：`p6_4_fill_gaps.coerce_schema` 对缺失浮点列一律 `fillna(0.0)` —— 只给 close 会往主湖写 `open=high=low=0` 的**零价 bar**，比不补更糟 |
| **重叠区比值非恒定只告警、不阻断** | rb0/hc0 的换月日天然落在重叠区内（08-31 检出 1 处比值突变），硬卡会让备源永远救不了场。告警写入报告 + `_SEAM_STATUS.json` |
| **接缝跨源名义价连续性复用主路 `_seam_nominal_break`** | 同口径把关：备源续接段若换了主力而湖未换，名义价必然在接缝处跳空 |
| **惰性导入备源模块 + try/except 归因** | R22：模块不可用只让该品种失败，不引入新的全停失效模式 |

### 护栏一律不降级

`RAW_ALIGN_TOL` / `K_TOL` / `MIN_OVERLAP` **一个字未动**（新增用例
`test_guardrail_constants_untouched` 锁定）。备源是**新增的第二数据源**，
不是放宽主路判定。

---

## 三、Fail-closed 清单（任一不可信即该品种失败 → 退出码 3）

| 错误码 | 触发条件 |
|---|---|
| `BACKUP_IMPORT` | 备源模块不可用（只该品种失败，不整批崩） |
| `BACKUP_FETCH` | 备源拉取抛异常（含 `BackupExhaustedError`） |
| `BACKUP_EMPTY` | 未返回该品种 / 名义价序列空 / 无有效正价格 |
| `BACKUP_NO_OHLC` | 无 OHLC 帧或缺 open/high/low/close 列（防零价 bar） |
| `BACKUP_NO_ANCHOR` | 湖内无该品种既有后复权序列（**红线：备源绝不自造后复权**） |
| `BACKUP_GRAFT` | 续接失败（含无重叠日期、非正价格） |
| `BACKUP_ALIGN_INSUFFICIENT` | 重叠样本 < `MIN_OVERLAP`（锚点不可信） |
| `BACKUP_NO_NEW_DATES` | 备源无湖内缺失的新日期 |
| `BACKUP_ANCHOR_STALE` | 锚点日与首个续接日间隔 > 7 天（防陈旧锚点外推） |
| `BACKUP_SEAM_NOMINAL_BREAK` | 接缝跨源名义价断裂（复用主路 P-NEW 护栏） |

---

## 四、实盘验证（只读，未写主湖）

命令：`python scripts/refresh_pull_local.py --asof 2026-09-03 --symbols rb0,hc0
--out-dir artifacts/_tmp/pull_backup_probe_20260903`

| 品种 | 主路判定 | 备源结果 | 锚点日 | k | 续接日 | 发射 close |
|---|---|---|---|---|---|---|
| rb0 | `SEAM_BASIS_CONFLICT` | ✅ BACKUP_SINA | 2026-09-01 | 1.181055 | 20260902 | 3710.874402 |
| hc0 | `SCALE_UNSTABLE` | ✅ BACKUP_SINA | 2026-09-01 | 2.192413 | 20260902 | 7377.468261 |

退出码 **0**（18 品种口径下亦为 0）。

**逐项对质**

- k 与湖内末值逐位一致：rb0 `1.1810548702134274`、hc0 `2.1924125588424404`
  —— 说明备源锚在了**换月后**的新 k 段上，未误用旧段（正是主路会踩的坑）。
- 后复权连续性：rb0 湖 09-01 `close=3748.67` → 续接 09-02 `3710.87`（−1.008%），
  与名义价 3174 → 3142（−1.008%）**完全同幅** → 无假跳空。
- 融合端 dry-run（`p6_4_fill_gaps.py --dry-run`）通过：1 行解析成功，
  `raw_close` 由 sina 回填 3142.0 / 3365.0（覆盖率 100%），
  OHLC 非零 —— **不会写出零价 bar**。
- **主湖零写入**：rb0/hc0 的 `1d/2026.parquet` mtime 仍为 2026-09-02 13:07/13:08，
  `find data -newermt` 无新增文件。

---

## 五、测试清单（新增 20 用例，`tests/test_refresh_pull_local_backup.py`）

| # | 用例 | 覆盖要求 |
|---|---|---|
| 1 | `test_backup_happy_path_grafts_and_scales_ohlc` | 续接值/OHLC 同 k 缩放、info 字段 |
| 2 | `test_backup_fetcher_built_with_save_false` | 备源构造必须 `save=False` |
| 3 | `test_backup_no_anchor_is_rejected` | ⑤ 无锚点拒绝（红线） |
| 4 | `test_backup_fetch_failure_is_rejected` | fail-closed |
| 5 | `test_backup_no_new_dates_is_rejected` | fail-closed |
| 6 | `test_backup_missing_ohlc_frame_is_rejected` | 零价 bar 防线（无帧 / 缺列） |
| 7 | `test_backup_seam_nominal_break_is_rejected` | 接缝跨源断裂 |
| 8 | `test_backup_stale_anchor_is_rejected` | 陈旧锚点外推 |
| 9 | `test_backup_never_writes_the_lake_it_reads` | ④ 不写主湖（mtime + 字节双重快照） |
| 10 | `test_backup_lookback_extends_request_window` | 请求窗口前扩（graft 锚点前提） |
| 11 | `test_backup_excludes_today_bar` | `--exclude-today` 口径一致 |
| 12 | `test_e2e_primary_fails_backup_succeeds_exit_zero` | ① 主路失败+备源成功 → rc 0 |
| 13 | `test_e2e_backup_also_fails_exit_three` | ② 主备皆失败 → rc 3、不留 json |
| 14 | `test_e2e_backup_json_matches_pandadata_contract` | ③ 与真实 `ag0.json` 逐字段对质 |
| 15 | `test_e2e_backup_never_touches_real_lake` | ④ 真实主湖字节级零变化 |
| 16 | `test_e2e_primary_success_never_triggers_backup` | ⑥ 主路成功不触发备源 |
| 17 | `test_e2e_backup_rows_are_flagged_in_report` | 报告「来源」列可辨识 |
| 18-19 | `test_base_symbol_*` | 符号映射 + 缺项兜底（R22） |
| 20 | `test_guardrail_constants_untouched` | ⛔ 护栏阈值锁定 |

---

## 六、改动文件与 diff 要点

### `scripts/refresh_pull_local.py`

- 模块 docstring：新增「Tier-2 备源通道」章节（死锁机理 + 4 步处置 + 红线）。
- 新增常量：`BACKUP_SOURCES` / `BACKUP_LOOKBACK_DAYS=60` /
  `BACKUP_MAX_GRAFT_DAYS=10` / `BACKUP_MAX_ANCHOR_GAP_DAYS=7` /
  `BACKUP_STATUS_PREFIX` / `BACKUP_SOURCE_TAG`。
- 新增 `_atomic_write_text`（G5：`tmp + os.replace`，零删除调用）—— 全脚本三处
  落盘（品种 json / `_LOCAL_REPORT.md` / `_SEAM_STATUS.json`）统一改走原子写。
- 新增 `_base_symbol`（`TQ_SYMBOLS` 缺项时去尾 `0` 兜底，防整批失败）。
- 新增 `_backup_load_anchor` / `_backup_pull_symbol`（备源通道主体）。
- `main()`：主路失败后新增 **2b 备源分支**；成功/失败均按新格式记入
  `report` / `seam_status_map` / `backup_detail`；报告新增「来源」列与
  「🟡 备源补数明细」小节；`_SEAM_STATUS.json` 新增 `backup` / `backup_detail`
  两个**追加键**（既有 `asof` / `statuses` / `lead_window` 语义不变）。

### `hexbroker/data/backup.py`

- `RawPull` 新增**可选**字段 `frame: pd.DataFrame | None = None`（缺省 None，
  向后兼容）；`_pull_one` 透传 `frame=sub`。
- ⚠️ 必要性：融合端 `coerce_schema` 对缺失浮点列 `fillna(0.0)`，只给 close 会
  写出零价 bar。备源必须提供完整 OHLC（详见 §二 决策表）。

---

## 七、残留风险与建议（供主理人终裁）

### 🟡 R1：新浪 09-03 日线可能迟到 → rb0/hc0 今晚仍可能只补到 09-02

16:05 实测：sina 末日 = **2026-09-02**，而 tqs 已有 09-03。若 20:00 自动化时
sina 仍未发布 09-03，则 rb0/hc0 只前进到 09-02，其余 16 品种到 09-03 →
P3-C 对 rb0/hc0 仍判 lag=1 → **这两个品种的信号仍被拦**。

- 缓解：备源通道是**幂等增量**的 —— 明早 08:00 那轮会自动补 09-03。
- 建议：今晚 20:00 后核对 `_LOCAL_REPORT.md` 的「发射区间」列，确认
  rb0/hc0 是否与其他品种同步到 09-03。

### 🟡 R2：rb0 需要连续多日备源，不是一晚能收口

新 k 段（1.181055）需累计 **3 个对齐日**才能满足 `MIN_OVERLAP`。即使今晚补到
09-02、明晚补到 09-03，稳定段仍只有 2 天 → rb0 **至少还要走 09-04 一轮备源**
（09-04 起稳定段 = {09-02, 09-03, 09-04} = 3 天 → 主路自愈）。
hc0 无此问题（湖前进后稳定段 4 天 ≥ 3，下一轮即回主路）。

- 这正是备源通道**按品种、按日幂等兜底**的价值所在，无需额外处置。

### 🟡 R3：续接段是 provisional，主源恢复后必须重建

`graft_adjusted` 产出的续接段未经主源（pandadata）真值校验。本次 rb0/hc0 的
重叠区检出 1 处比值突变（08-31 换月），告警已写入报告与 `backup_detail`。
主源恢复后应重建 08-31 起的窗口 —— **建议登记为待办**，不要遗忘。

### 🟢 R4（观察项，非本次引入）：陈旧 json 残留

`out_dir` 按 asof 分目录，同 asof 重跑时**若某品种本轮失败**，上一轮成功留下的
json 不会被清理（项目禁止删除类调用），融合脚本会照常拾取。建议编排层在
重跑前换用新的 out_dir，或人工核对。本次未改动此既有行为。

### 🟢 R5（观察项，非本次引入）：tqs 09-03 close 与 09-02 完全相同

实测 rb0 tqs 09-02 = 09-03 = 3142.0。可能为真平盘，也可能为未刷新 bar ——
属主路既有行为，本次未触碰。若明早发现该值被修正，需另行取证。
