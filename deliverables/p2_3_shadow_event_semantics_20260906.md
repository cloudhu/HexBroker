# P2-3 影子止损事件语义 + P3-3 运行时慢预检告警 · 执行报告

> 2026-09-06 · 主理人齐活林亲自执行（原派工因 429 用量限制失效：`eng-shadow` / `software-engineer` 均未产出任何代码；Expert「SoftwareCompany」已取消，按默认 Agent 模式接管）。
> 上下文：P0-A 进程守护收口已完成（`9c3bad8`→`28a5805`），三时段自动化已带 `--watchdog --max-restarts 20`。

---

## 一、P2-3 影子止损事件语义（时间敏感项，周一开盘前收口）

### 1.1 缺陷与取证

| 项 | 事实 |
|---|---|
| 数据用途 | `data/paper/shadow_stops.jsonl` 是 09-21「是否翻 `risk.shadow_stops:false`（转真实平仓）」的**唯一决策依据** |
| 缺陷 | `SHADOW_FIELDS` 9 个字段全是 tick 级快照，**无事件语义**；300s 去重只控制写盘节奏，一个持续 6h 的触发段仍产生 ~72 行 |
| 后果 | 分析侧直接 count 行数回答「止损触发了几次」→ **高估 1–2 个数量级**，翻旗决策建立在错误事实上 |
| 关键窗口 | ⚠️ 已核实该 jsonl **当前不存在（0 行）**——观察期 09-07 周一开始，**现在改零数据损失、无需回溯脚本**；周一开盘后窗口关闭 |

### 1.2 设计（复用现有语义，不新增参数）

- **事件边界 = tick 级触发状态连续性**：上一 tick 未触发、本 tick 触发 → 新事件；连续触发 → 同段延续。价格离开触发区哪怕一个 tick，旧事件即结束。该判定**独立于去重窗口**（去重只管写盘节奏）。
- `SHADOW_FIELDS` 插入 `"event_id"`（紧随 `ts`）与 `"first_hit"`（bool）。`event_id` 格式 `<symbol>:<KIND>:<首tick isoformat>#<单调序号>`——同段恒定、跨段必异、可从记录反解。
- `first_hit` 语义 = 「本事件段首条**落盘**记录」：`pending_first` 标志只在真正 append 记录时消费/翻转。若新事件首 tick 撞去重窗被拦（<300s 的二次穿越），该段首条**落盘**记录仍标 `True`——分析侧按 `event_id` 分组后 `sum(first_hit)` 恒等于事件数。
- ⛔ 未触碰 `evaluate_trigger()` 判定语义、未引入「只记极值」规则（原作者明确注释过的红线）；⛔ 未改变去重节奏。

### 1.3 实现要点（fail-open，R22）

- 新增状态 `_in_trigger` / `_event_state` / `_event_seq`，配**独立锁 `_event_lock`**，与 `_lock`（去重哨兵/落盘）**永不同持**（无嵌套 → 无死锁环）。
- 状态损坏（非 dict / 缺 key / 任何异常）→ 退化「按新事件处理」+ debug 日志，**绝不抛、绝不中断 tick**。
- `reset_dedup()` 同步清事件状态（否则复位后首段被误判延续）。
- **顺带抓到并修掉一个真 bug**：`scan()` 在未命中时提前 `return []`，**绕过了事件段关闭逻辑**——「价格离开触发区」这一事件边界永远丢失，离开后的再次触发会被并进旧段（两次真实触发算成一次，低估事件数）。修复：提前 return 前显式调 `_mark_event_closed(symbol, set())`。该 bug 由新测试 `test_new_event_after_price_leaves_trigger_zone` 首跑抓出。

### 1.4 验证

| 项 | 结果 |
|---|---|
| 既有 25 条回归 | ✅ 全绿（判定语义/落盘格式/账户零变更/去重/真实平仓路径全部未破坏） |
| 新增 8 条 | ✅ 全绿：①持续触发跨 3 窗→1 个 event_id、仅首条 True ②离开再触发→新 id+True ③首 tick 被拦→首条落盘仍 True ④同 tick 双命中→独立 id ⑤跨品种隔离 ⑥状态损坏 fail-open ⑦reset 同步清 ⑧字段顺序契约 |
| **变异 KILL**（影子树，仓库原文件只读） | 把「续条复用 event_id」变异成「每条新 id」→ **`test_continuous_trigger_yields_one_event_and_first_hit_only_once` 精确判红**（唯一一条，其余 32 绿）；对照未变异 33/33 绿；变异后仓库原文件 sha256 前 16 位 `3026c5818014ed21` 不变 |

---

## 二、P3-3 运行时慢预检告警（采纳 software-engineer 建议）

### 2.1 动机

非阻塞预检实测 ~0.01s，退化成阻塞锁（`LK_NBLCK`→`LK_LOCK`）实测 ~9s 且**返回值语义完全不变**（阻塞重试抛的 EDEADLOCK 恰在 errno 白名单内）——时延护栏 `test_lock_probe_returns_promptly_when_lock_held` 只在跑测试时生效，而三个时段是无人值守的，退化后运行时零信号。

### 2.2 实现

- `main()` 预检调用侧（`--watchdog` 路径）加 `time.perf_counter()` 计时。
- 超过 `SLOW_PROBE_WARN_SEC = 2.0`（正常 0.01s / 退化 9s，差 3 个数量级，2.0s 留足余量）→ 打 `[LAUNCH][WARN] 单实例预检耗时 X.XXs，超过阈值 2.0s —— 典型成因是抢锁退化成阻塞锁…`。
- ⛔ R22：**只打印，绝不改变控制流**——不影响 `running` 判定、不 return/raise。

### 2.3 验证

| 项 | 结果 |
|---|---|
| 新增 2 条 | ✅ 正向：sleep 2.5s → 告警出现且 rc==0、后续拉起照常（控制流不变）；反向：正常速度 → **不得**出现告警（防告警变新噪声源） |
| launcher 全套 22 条 | ✅ 全绿 |
| **变异 KILL** | 删除告警分支（`if False:`）→ 正向用例**精确判红**、反向不受影响；阈值趋零变异 SURVIVE（1μs 低于 perf_counter 两次调用开销，无判别意义，已弃用该变异并如实记录）；仓库原文件 sha256 前 16 位 `8e84bd364f761945` 不变 |

---

## 三、Git 与文件清单

- feat 提交：代码 + 测试（4 文件）；docs 提交：本报告。显式 `git add`、`git fsck --no-dangling` 无输出、未 stash / 未 gc。
- 变更文件：
  - `hexbroker/paper/shadow_stops.py`（+~90：SHADOW_FIELDS 两字段、事件状态三容器、`_resolve_event`/`_mark_event_emitted`/`_mark_event_closed`、提前 return 关闭 bug 修复、`_emit` 日志带 event_id）
  - `tests/test_paper_shadow_stops.py`（+8 用例，25→33）
  - `scripts/launch_trading_window.py`（+~20：`import time`、`SLOW_PROBE_WARN_SEC=2.0`、预检计时+告警）
  - `tests/test_launch_trading_window.py`（+2 用例，20→22）
- ⚠️ 交接说明：本轮由主理人亲自实现（429 限制致成员失败），QA fresh-eyes 独立复核**未执行**——建议 429 重置后（09-07 08:22 UTC+8）补一轮交叉复核，重点：`_resolve_event` 的锁边界、`pending_first` 消费时序、`_mark_event_closed` 对多 symbol 的清理范围。

## 四、遗留与风险

- 极端场景：新事件首 tick 被去重拦截且该段**从未**落盘过（事件时长 < 300s 且恰逢窗口内）→ 该事件在 jsonl 中无记录，与修复前行为一致（整段丢失），无回退。
- `event_id` 含 `#<单调序号>`，跨进程重启归零——但 id 携带 ts，重启不会与历史 id 冲突。
- 观察期 09-07 开始后，09-21 复盘口径：**事件数 = unique(event_id) 计数**（或 `sum(first_hit)`），绝不再直接 count 行数。
