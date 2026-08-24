# QA 独立复核报告：P0-1 开仓成本门禁 + P0-2 信号无变化冷却（2026-08-24）

> 复核人：严过关（QA）｜方式：fresh-eyes 独立复核，未采信工程师结论，逐项独立验证
> 复核对象：`hexbroker/paper/risk_gate.py`、`hexbroker/paper/scheduler.py`、`configs/paper.yaml`、
> `tests/test_risk_gate_cost.py`、`tests/test_signal_cooldown.py`、`scripts/replay_0824_cost_cooldown.py`
> 原则：无证据不翻转；只读复核，未修改任何生产代码；不触碰 git 修复（主理人职责）。

---

## 一、代码审查结论（逐项 PASS / FAIL）

### P0-1 开仓成本门禁（risk_gate.py）

| # | 审查项 | 结论 | 依据 |
|---|---|---|---|
| 1 | 公式数学正确性 | **PASS** | `notional=price×multiplier`（取 CostModel._multiplier 品种级）；`expected_pnl=direction×exp_ret/100×notional`（exp_ret 为带符号百分比）；`round_trip_cost=notional×(fee_open+fee_close_today)`；通过条件 `expected_pnl > rt_cost×min_ratio`。抽样复算：price=3000×mult=10→notional=30000；exp_ret=0.5%→expected=150；rt=30000×0.00015=4.5；150>4.5×2=9 ✓ |
| 2 | 方向一致性校验（p_up 与 exp_ret 符号矛盾拦截） | **PASS** | 多头(p_up≥0.5)+exp_ret≤0→拦截；空头(p_up<0.5)+exp_ret≥0→拦截；含 0 边界（无边际不放行）。双向矛盾均实测拦截，reason=cost_gate_reject |
| 3 | 门禁只作用于新开仓（有持仓风控不受影响） | **PASS** | 触发条件含 `abs(pos_ctx.position)<1e-12` 且 `abs(intent)>1e-12`；有持仓时完全跳过。测试⑤实测：有持仓+exp_ret 极低→风控照常，reason≠cost_gate_reject |
| 4 | 拦截后置 0 + reason 标注 | **PASS** | `cost_rejected and not decision.liquidate` → target_position=0、reason=cost_gate_reject（审计可查） |
| 5 | 向后兼容 | **PASS** | 构造默认 cost=None→门禁跳过；cost_gate_enabled=False→显式关闭（测试④/⑤验证）。注：scheduler 层从 broker.cost 自动注入→生产默认生效（P0 目标行为）；缺 risk_gate 段的旧配置会默认开启（见遗留风险 R2） |

### P0-2 信号无变化冷却（scheduler.py）

| # | 审查项 | 结论 | 依据 |
|---|---|---|---|
| 6 | 只拦「无持仓 + 开仓意图」 | **PASS** | 条件：`_signal_cooldown_enabled and abs(pos_ctx.position)<1e-12 and abs(decision.target_position)>1e-9`（abs 同时覆盖多/空头）；有持仓时冷却完全不介入，S1/S4 平仓照常（测试②实测） |
| 7 | 指纹逻辑：初始无指纹放行 | **PASS** | `_last_sig_fp` 初始为空 dict→首次开仓放行并记录（测试④实测） |
| 8 | 指纹逻辑：信号变化解除冷却 | **PASS** | 容差比较（p_up 差<0.01 且 exp_ret 差<0.001 且 source 相同）；变化→开仓并更新指纹（测试③） |
| 9 | 指纹逻辑：source 变化视为变化 | **PASS** | source 参与指纹；主源↔技术兜底切换视为变化（测试⑤） |
| 10 | 指纹只在实际开仓成交时更新 | **PASS** | 更新条件：`event.is_open and 开仓前无持仓`；预算拒绝（execute_plan 返回 None）不更新→下次可重试；冷却拦截（target=0→plan 无操作→无 event）也不更新→持续拦截（代码走查） |
| 11 | 多品种并发无串扰 | **PASS** | `_last_sig_fp` 为 symbol→fingerprint 字典；独立构造 rb0/ag0 双品种实测：同轮各自开仓（rb0 多 2 手 / ag0 空 1 手），指纹互不覆盖 |

---

## 二、测试运行结果（区分环境性 / 断言性失败）

- **新增 12 例：`tests/test_risk_gate_cost.py`(6) + `tests/test_signal_cooldown.py`(6) → 12/12 全绿，断言性失败 0**
- 全量复跑（`pytest --continue-on-collection-errors`）：**285 passed / 20 failed / 15 errors**
  - 20 failed + 15 errors **全部为环境性**：pyarrow/pydantic/scipy/sklearn/optuna/lightgbm/torch 缺失（ModuleNotFoundError / ImportError），与本次改动无关 — **与工程师声称"15 模块无法收集、20 例 ImportError"完全一致**
  - paper/risk 相关（test_paper_pipeline / test_sell_engine / test_stoploss / test_risk_prev_stop / test_paper_broker / test_paper_sessions / test_trade_intent / test_trade_stats / test_trade_logger / test_pid_lock / test_risk_gate_cost / test_signal_cooldown）**全部通过**；仅 test_paper_signals 4 例因 pyarrow 缺失失败（环境性）
- **结论：无断言性失败；工程师"新增 12/12 全绿 + 环境缺依赖"说法核实属实**

---

## 三、复跑数字核对（独立执行 `python scripts/replay_0824_cost_cooldown.py --rounds 60`）

| 档位 | 工程师声称 | QA 独立复跑 | 核对 |
|---|---|---|---|
| 1 修复前（min_bars=1 band=0 无门禁无冷却） | 30开 30平 | 30开 30平（末态 0.0） | ✓ |
| 2 仅 cb37333（min_bars=2 band=0.1×ATR） | 3开 2平 | 3开 2平（末态 1.0） | ✓ |
| 3 cb37333 + P0-1 + P0-2 | 1开 1平 | 1开 1平（末态 0.0） | ✓ |

**真实代码路径核验（非 mock 硬编码）**：脚本构造**真实** RiskGate / PaperBroker / TradingScheduler / PlanManager / TradingSession，驱动**真实** `TradingScheduler._process_symbol` 决策链（信号→风控 evaluate→冷却→planner→broker.execute_plan→logger）。Mock 仅限数据源（固定行情/固定信号/固定 K 线）；门禁与冷却为生产代码本体。唯一 patch 为 `broker.position_ctx` 的 bars_in_position 递增（模拟 8/24 审计口径：每 60s tick 计 1 根），属数据层模拟，不绕过门禁/冷却逻辑。**结论：PASS，复跑数字可信。**

日志佐证：档 3 全程打印「信号未变化…冷却拦截重复开仓」，未出现「成本门禁拦截」— 与文档"rb0 exp_ret=0.171% 通过门禁（约 11.4× > 2×）"一致；冷却才是档 3 开/平次数下降的主因。

---

## 四、边界推演

1. **止损后再开（S1/硬止损平仓后信号未变）**：冷却拦截重开 = **符合设计**（正是消除 60s 贴线穿越循环的预期行为）。注意：指纹无时间衰减，日内信号恒定则当日不再重入（见遗留风险 R1）。
2. **exp_ret 为 NaN/None/inf**：NaN → 拒开不 crash ✓；None → `float(None)` 抛 TypeError（但 SignalEngine 三条路径均不产生 None：主源 `float(row)` / 技术兜底 float 计算 / 中性信号 0.0，实际不可达）；+inf → 会放行（无 isfinite 防御，理论缺口，实际不可达）。**PASS（附加固建议，见 R4）**。
3. **多品种并发**：symbol→fp 字典隔离，双品种实测无串扰 ✓。
4. **方向翻转边界**：p_up 跨 0.5 边界时差 ≥0.09 > 容差 0.01；exp_ret 翻转符号差 > 容差；即便极端落在容差内（exp_ret≈0）也会被成本门禁方向一致性拦截 — 双层防护，不会误拦合法多空翻转 ✓。
5. **容差边界**：`_signal_fp_same` 用 `<` 非 `<=`，恰好等于容差的信号变化按"变化"放行（保守方向，无碍）。

---

## 五、QA 裁决

**✅ 通过（附 2 项建议级风险，不阻塞上线）**

- 核心逻辑（公式、作用域、冷却边界、向后兼容、多品种隔离）逐项验证 PASS；
- 新增测试 12/12 全绿，无断言性失败；全量失败均为环境缺失；
- 三档复跑数字与工程师声称完全一致，且确认走真实决策链；
- 依「无证据不翻转」铁律：**现有证据支持工程师结论，予以确认**。

---

## 六、遗留风险清单

| # | 风险 | 级别 | 说明 |
|---|---|---|---|
| R1 | 冷却无时间衰减 | 设计确认项 | 止损平仓后信号不变则当日不再重入（aggressive，属 P0 目标行为）；如需恢复再入能力，建议增加"价格重新远离 MA/时间窗"解除条件 |
| R2 | 缺配置段时默认开启 | 观察项 | scheduler 在 cfg 无 risk_gate/signal_cooldown 段时默认 enabled=True（注入 cost 后门禁生效）；新配置显式声明无影响，其他环境旧配置会静默变更行为，建议文档标注或改显式 opt-in |
| R3 | 成本口径不含滑点 | 建议 | round_trip_cost 仅含手续费；rb0 单边滑点 1 tick=¥10/手，往返 ¥20 > 手续费 ¥4.5。rb0 信号覆盖倍数 11.4×（仅费）→ 含滑点约 2.1×，仍 >2 但余量薄；低质量信号可能被放行致实际亏损。建议 round_trip_cost 纳入滑点或单列滑点成本项 |
| R4 | _cost_gate_pass 健壮性 | 建议 | 缺 `math.isfinite` 守卫（+inf 放行）与 None 防御（TypeError）；当前信号链路不可达，建议顺手加固 |
| R5 | 容差边界 `<` vs `<=` | 无碍 | 恰好等于容差按变化放行，保守方向 |
| R6 | 环境依赖缺失 | 环境性 | 本沙箱缺 pyarrow/pydantic/scipy 等，20 failed+15 errors 与本次无关；生产完整依赖环境需全量复跑确认（工程师已注明基线 344 绿） |

---

*复核完成时间：2026-08-24。所有验证命令均为只读，未修改生产代码。*
