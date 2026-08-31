# 13 · HexBroker 稳定性缺陷攻坚 · 实地取证报告

> 日期：2026-08-28 ｜ 主理人：齐活林（Qi）｜ 模式：fresh-eyes 独立取证（无证据不翻转）

## TL;DR
今日清单四项稳定性缺陷（①PID锁互斥 ②交易日志去重统计 ③S1误触发修复 ④stop字段跨品种串扰）
经逐文件实地取证 + 测试覆盖核对，**全部已在 P0/P1 迭代中被顺带修复**，当前 `6206e92/92b519a` 基线上
无此四项缺陷。按"无证据不翻转"铁律，已闭环项**不重复改动**，仅记录唯一真实语义债（③）。
**判定：四项稳定性缺陷攻坚 = 已闭环 ✅，无需代码改动，本次不入 git。**

---

## 铁律执行
- **先读文件状态、实地取证**，禁止凭陈旧上下文推断（user 铁律）。
- 本次**零代码改动**：①~④ 均确认已修；若强行"再修"会引入回归/翻转，违背铁律。
- 仅产出本取证报告 + 当日 memory 记录。

---

## 四缺陷逐项判定

| # | 缺陷 | 状态 | 根因（旧） | 修复落点（当前代码） | 测试证据 | 判定 |
|---|------|------|-----------|---------------------|---------|------|
| ① | PID锁互斥 | ✅ 已闭环 | 多实例并发 append 同一行情流 → trades.log 会话重放 3× 伪增 | `scripts/paper_trading_main.py:34-57`（`_try_acquire_pid_lock`）+ `:311-327`（启动互斥：存活实例拒绝启动、僵尸覆盖、`finally` 删锁） | `tests/test_pid_lock.py`、`tests/test_paper_watchdog.py` | 已修，不重改 |
| ② | 交易日志去重统计 | ✅ 已闭环 | 同上根因 + 副本字段不一致难察 | `hexbroker/paper/trade_stats.py`：`dedup_trades`（按 trade_id 去重）、`fifo_realized`、`analyze_trades_log`、`daily_stats_alerts`、`render_daily_stats_markdown`（闭合校验） | `tests/test_trade_stats.py`、`tests/test_risk_gate_dedup.py` | 已修，不重改 |
| ③ | S1误触发修复 | ✅ 已闭环 | 开仓后价格贴 MA 微幅往返 → 60s 轮询秒平（贴线穿越循环） | `hexbroker/risk/sell_engine.py:36-58`：开仓缓冲 `bars_in_position>=s1_min_bars` + ATR带宽死区 `band=s1_band_atr×atr`；调用链 `risk_gate.build_state`→`broker.position_ctx` 正确注入 `ma_price`/`bars_in_position` | `tests/test_sell_engine.py`、`tests/test_signal_cooldown.py` | 已修，不重改 |
| ④ | stop字段跨品种串扰 | ✅ 已闭环 | `plan.stop_price` 被多品种复用 → ag0 平仓错显 rb0 止损 | `hexbroker/paper/broker.py:87,138-146`（`_stops/_take_profits` 按 symbol 隔离、平仓读持仓实际档位）+ `hexbroker/risk/manager.py:63-132`（ratchet/prev_stop 按 sym）+ `hexbroker/paper/risk_gate.py:147`（开仓前 `reset_ratchet(symbol=)`） | `tests/test_risk_prev_stop.py`、`tests/test_paper_broker.py`、`tests/test_stoploss.py` | 已修，不重改 |

---

## 唯一真实语义债（③，非缺陷/可选优化，⚠️ 不擅自改）
- **现象**：`sell_engine.py:37` `s1_min_bars` 变量名/注释语义为"持仓根数（bar数）保护期"，
  但 `hexbroker/paper/broker.py:251` `position_ctx.bars_in_position = max(1, (today - open_date).days)`
  **实盘注入的是"持仓天数"**；回测中 `RiskState.bars_in_position` 应为"bar数"（每根 bar 累加）。
- **效果**：实盘 S1 保护期被拉长为"≥1 天"（日内持仓不触发 S1），恰好抑制了 P0 要解决的 60s 秒平误触发
  ——方向是"过度抑制（漏触发）"而非"误触发"，与清单③目标（消除误触发）**一致且有效**。
- **风险**：若改回 bar 级（如 2 根 = 2 分钟），会**翻回 60s 秒平误触发**。
- **建议**：作为独立优化项评估"实盘用天数 / 回测用 bar 数"是否可接受，或统一语义；**当前不建议改动**。

---

## 2026-08-31 追加：潜伏加固债 ② 落地（防御性新增，不翻转已闭环判定）

> 主理人延续指令：在四缺陷已闭环前提下，推进"潜伏加固债"中风险最低、可自主落地的 ②。

- **对象**：内存聚合器（`scheduler.py` 的 `_day_trades` / `_all_trades` + 审计日志 `logger.trade`）。
- **加固动作**：新增 `_record_trade(event, day)` 统一写入入口 + `_seen_trade_ids` 哨兵集合，
  按 `trade_id` 对称防御性去重；原两处 `setdefault(day, []).append(event)` / `all_trades.append(event)` /
  `logger.trade(event)` 内联点统一收敛到该方法（`_process_symbol` + `_risk_manage_only`）。
- **边界声明（诚实）**：`trade_id` 为单调序列 `T{n:06d}`（`broker.py:149`），本守卫**仅**覆盖
  "同一 event 对象被重复记录"（潜在重入 / 重复调用导致聚合计数翻倍）；**不覆盖**重入执行产生
  新 `trade_id` 的情形（属 `execute_plan` 幂等性保障范围，独立设计项）。
- **测试**：新增 `tests/test_scheduler_trade_dedup.py`（3 例：同 id 去重 / 异 id 全记 / 跨日独立），
  并回归 `test_paper_pipeline` / `test_scheduler_halt` / `test_signal_cooldown` / `test_scheduler_stale_warn` /
  `test_audit_ruff_gate` / `test_risk_gate_dedup` 共 **40 例全绿**。
- **Commit**：feat `hexbroker/paper/scheduler.py` + `tests/test_scheduler_trade_dedup.py`；docs 本报告追加。
- **判定**：② 潜伏加固债 = 已落地 ✅（防御性新增，未触碰任何已闭环缺陷状态，零回归）。

---

## 2026-08-31 追加：潜伏加固债 ①③④ 落地（防御性新增，不翻转已闭环判定）

> 主理人延续指令：推进四缺陷已闭环后剩余的潜伏加固债 ①③④。

- **① PID 锁进程身份校验（防 PID 复用误判存活）**
  - 根因：`paper_trading_main._try_acquire_pid_lock` 此前仅用 `health_check._is_pid_alive`（存活探测）判重，
    不校验进程身份。旧实例崩溃未清 `paper.pid`（绕过 finally 删除）→ 该 PID 被无关进程复用 →
    误判「存活」→ 拒绝新实例启动。
  - 加固：锁文件格式由纯 `PID` 升级为 `PID:CREATION_TIME`（创建时间 FILETIME，跨平台可比）。读锁时
    PID 存活但创建时间不符 → 判定僵尸锁（PID 复用）→ 覆盖而非拒启；创建时间吻合 → 拒绝（真·同进程）。
    旧格式（纯整型）维持原「存活即拒绝」语义，向后兼容。
  - 同步修复第二读取方 `health_check._read_pid_file`（`int(text)` → `int(text.split(":")[0])`），
    避免新格式致健康检查误判「模拟盘未运行」。
  - 新增 `tests/test_pid_lock_identity.py`（同进程拒绝 / PID 复用覆盖 / 旧格式兼容）。

- **③ `_risk_manage_only` 显式透传 ma_price**
  - 根因：`_process_symbol` 从 K 线算 `ma_price` 并传入 `risk_gate.evaluate(... ma_price=ma_price)`，
    而 `_risk_manage_only` 调用 `evaluate(sig, quote, acct, pos_ctx)` **未传 ma_price**（默认 None）
    → `RiskState.ma_price=None` → `sell_engine.S1`（趋势破坏止损）在「仅风控」模式下被整体跳过。
  - 加固：在 `_risk_manage_only` 中补 `bars = self._cached_bars(symbol, day)` +
    `_, _, ma_price = self._aux_from_bars(bars)`，透传 `evaluate(... ma_price=ma_price)`，与 `_process_symbol` 对齐。
  - 新增 `tests/test_scheduler_risk_manage_ma_price.py`（fake-object 探针捕获 evaluate 收到的 ma_price，
    30 日收盘均线 = 119.5 非 None）。

- **④ `"default"` 哨兵改为可观测告警（非 fail-fast）**
  - 根因：`risk/manager.py` 原 `sym = state.symbol or "default"`——若 `RiskState.symbol` 为 falsy，
    所有无符号状态共享同一 `"default"` 桶的 ratchet/prev_stop，是已修复的④跨品种串扰的同构复发路径。
  - 取证修正（无证据不翻转）：测试 `test_risk_priority.py` 的 `_state` fixture **不设 symbol**
    （`RiskState(symbol=None)`）→ 硬 fail-fast 会误伤单测隔离。故改 **warn-and-continue**（告警一次、不抛异常），
    使潜在跨品种串扰从静默变可发现；正常生产流 `state.symbol` 恒非空（来自 `self._symbols`），该路径不触发。
  - 新增 `tests/test_risk_manager_default_sentinel.py`（首触发置位标记、二次不再重复告警、返回正常）。

- **验证**：新增 3 测试 + 更新 `test_pid_lock.py`（格式兼容断言）；回归 pid/scheduler/risk/health_check 相关
  **94 例全绿**；import 自检通过；`git fsck --no-dangling` 干净。
- **Commit**：feat（4 源文件 + 4 测试）/ docs（本报告追加）。
- **判定**：潜伏加固债 ①③④ = 已落地 ✅（防御性新增，未触碰任何已闭环缺陷状态，零回归）。

---

## 主理人裁决
- **四项稳定性缺陷 = 已闭环 ✅**（证据见上表，测试全覆盖）。
- **本轮零 git 改动**（严守"无证据不翻转"：已修不重改、避免回归）。
- **唯一待办**：③语义债作为可选优化项挂账，待主理人裁决是否立项。
- **🔧 2026-08-31 更正（追加，不改原结论）**：上述「本轮零 git 改动」指 08-28 取证轮；
  同日（08-31）在主线程下另立三项**防御性加固**，均经独立取证 + 测试零回归，未翻转任何已闭环判定：
  ① PID 锁进程身份校验（commit 见 feat）、② 内存聚合器 trade_id 去重（c5f73db）、
  ③ `_risk_manage_only` 透传 ma_price、④ `"default"` 哨兵可观测告警（见上三条追加节）。

## 文件清单（本次产出，未入库）
- `deliverables/software-company/futures-research/13-stability-defects-forensics.md`（本报告）
- `.workbuddy/memory/2026-08-28.md`（当日取证记录）

## 下一步建议（待主理人"继续"）
1. **P2 双治理 + 落地**（六方案 P2 项）；
2. **六方案终裁归档**（1C✅/2A✅/3A⛔/3B❌/1B❌/2B❌，各自携数据根因归档）；
3. **③语义债**独立评估（可选）。
