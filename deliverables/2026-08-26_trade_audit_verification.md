# 交易审计报告 · 独立评审验证（Verification Report）

- **被验证对象**：`deliverables/2026-08-26_trade_audit_report.md`
- **验证日期**：2026-08-26
- **验证方式**：独立重跑审计脚本 + 独立 grep 清点日志 + 逐行通读被引源码与配置（降级模式：本环境未暴露 `TeamCreate` 工具，主理人齐活林直接执行 QA 复核，结论以证据为准）
- **复核工具**：受管 python `3.13.12`（pandas 3.0.5）、`grep`、`Read`
- **可靠性评级**：**HIGH（事实层全部可复跑通过）**；根因层 1 处归因偏差（P1-2）+ 1 处叙事自矛盾（74/81）须修订

---

## TL;DR

原报告的核心结论**成立且可独立复现**：账户净亏 −4.96%、对敲失控（≤60s 平仓 29/40）、止损状态污染、行情缺失未熔断、运行实例与当前源码存在版本/注入差异——均经源码与日志独立确认。但评审发现 **2 处须修订缺陷**：

1. 🔴 **叙事自矛盾**：TL;DR 第 5 条与 P1-2 写"可读 `[意图]` 日志（74）"，但 F5 与 `audit_result.json`/`account.json` 均证真实总数为 **81**（74 仅为 8-24 当日计数）。报告自身前后不一。
2. 🟡 **P1-2 根因归因错误**：错把"审计源不统一"归因为 `trade_intent.py` 翻译层覆盖不全；实际 308 vs 81 来自 `trade_stats.py` 周期去重计数与 `TradeLogger` 逐笔计数两套口径，且 `broker.py:138` 注释明确"审计 JSON 由 TradeLogger 统一输出（单一审计源）"。

另含若干**精度级**瑕疵（详见第四节）：手续费精确值 −3277.97（报告写"约 −3400"）、`configs/paper.yaml` 实际 160 行（报告写 161）、P0-1 行号正确但"平仓路径"措辞不精确（实为统一 `execute_plan`）。

---

## 一、验证方法与复现

| 步骤 | 命令 / 动作 | 结果 |
|---|---|---|
| 复跑审计脚本 | `python scripts/audit_trades_log.py` | 输出与 `data/paper/audit_result.json` **逐字段一致**（见下） |
| 独立清点日志 | `grep -c "成本门禁拦截"` = **128**；`grep -c "getaddrinfo failed"` = **26**；`grep -iEc "启动\|...start"` = **17**；`wc -l` = **3464** | 与 F1/F2/F3/F4 完全吻合 |
| 8-25 门禁原始行 | `grep "成本门禁拦截" trades.log \| head -3` | `exp_ret=-0.3432 price=16841.00 expected_pnl=0.00 round_trip_cost=0.00 notional=252615.00 min_ratio=2.0` |
| 通读源码 | `broker.py`(343) / `risk_gate.py`(244) / `scheduler.py`(628) / `trade_intent.py` / `configs/paper.yaml`(160) | 逐行核对行号与逻辑 |

**复跑脚本关键输出（与已落盘 audit_result.json 一致）**：total_lines=3464, starts=17(1/9/4/3), netfails=26(全 8-24), gates=128(全 8-25), intents=**81**(74/5/2), by_symbol ag0:36/rb0:45, paired_holds=40, hold_le60=29, stop_error_count=22, fee_ratio ag0:1.89/rb0:1.957, 统计 258→308, snap 100000→95040.62, c0:0。

---

## 二、事实表 F1–F13 验证（A 组）

| 项 | 报告主张 | 独立核验 | 结论 |
|---|---|---|---|
| F1 | 总行 3464 | `wc -l`=3464，脚本复跑一致 | ✅ CONFIRMED |
| F2 | 重启 17（1/9/4/3） | grep 启动标记=17，脚本 start_by_day 一致 | ✅ CONFIRMED |
| F3 | 网络失败 26（全 8-24 13:01） | `grep -c "getaddrinfo failed"`=26，netfail_by_day 全 8-24 | ✅ CONFIRMED |
| F4 | 门禁 128（全 8-25） | `grep -c "成本门禁拦截"`=128，gate_by_day 全 8-25 | ✅ CONFIRMED |
| F5 | 可读意图 81（74/5/2） | 脚本 intents=81；**但 TL;DR#5 与 P1-2 写 74** | ⚠️ CONFIRMED(数值) / 报告叙事写错(74) |
| F6 | 8-24 去重 258→308（逐次+10） | stat_sample 258/268/…/308（+10） | ✅ CONFIRMED |
| F7 | ag0:36 / rb0:45 / c0:0 | by_symbol + c0_fills=0 一致 | ✅ CONFIRMED |
| F8 | 配对 40；≤60s:29；≤120s:40 | paired_holds=40, hold_le60=29, hold_le120=40 | ✅ CONFIRMED |
| F9 | 止损错乱 22（ag0 平空显 2917.89） | stop_error_count=22，样本 open_stop=17307.07/close_stop=2917.89 | ✅ CONFIRMED（笔数）；跨品种归因合理但未独立核验 rb0 开多止损值 |
| F10 | ag0/rb0 各仅 6 离散价 | price_n_distinct ag0:6/rb0:6 | ✅ CONFIRMED |
| F11 | 平/开手续费比 ag0:1.89 / rb0:1.96 | fee_ratio ag0:1.89 / rb0:**1.957**（≈1.96） | ✅ CONFIRMED（rb0 精确 1.957） |
| F12 | 净值 100000→95040.62（−4.96%） | snap_first/last 一致；(95040.62−100000)/100000=−4.959% | ✅ CONFIRMED |
| F13 | 已实现 ag0:−3116.04 / rb0:−1843.34（合 −4959.38） | `account.json` realized 完全吻合 | ✅ CONFIRMED |

**A 组结论：F1–F13 全部可复跑确认，无事实性错误。**

---

## 三、派生计算与代码根因验证（B/C 组）

### B 派生计算
| 主张 | 核验 | 结论 |
|---|---|---|
| 净=毛利−费 → 手续费≈−3400 | 末行 净−1563.1=−4841.07 ⇒ 费=−3277.97（**精确值 −3277.97，非 −3400**，偏差 +3.7%） | ⚠️ PARTIAL（量级对，"约 −3400"为粗舍入） |
| 全周期 −4959.38 ≈ 100000−95040.62 | 精确相等 | ✅ CONFIRMED |
| 换手率 308 笔/80 分钟≈4 笔/分 | 与 8-24 时段吻合 | ✅ CONFIRMED |
| 8-25 `expected_pnl` 应按公式≈−866、`rt_cost`≈38，但实测 0.00 | 公式重算：dir×exp_ret/100×notional = 1×(−0.3432)/100×252615 = **−866.9**；fee 部分=252615×(0.00005+0.00010)=37.89，ag0 slippage 部分=2×0.01×1×15=0.3 ⇒ **≈38.2**。==**但当前 `risk_gate.py` 在 `price>0` 且 `exp_ret=−0.3432` 有效时绝不会输出 0.00**== | 🔎 见下"版本差异" |

**关键判定（强化原报告警示）**：8-25 日志 `price=16841.00`、`exp_ret=−0.3432` 均为有效值，而当前 `risk_gate.py:_cost_gate_pass` 在有效输入下必算得 −866.9 / 38.2。日志却显示 `0.00/0.00` ⇒ **8-25 运行实例确实与当前 `risk_gate.py` 不符**（版本/注入差异）。这**正面印证**了原报告第五章"运行实例与当前源码存在版本/注入差异"的审慎警告，而非反驳。原报告将该 0.00 归因于"cost 未注入/版本差异"——**方向正确、结论成立**。（注：源码使 0.00 的路径仅为 `price<=0` 或 `exp_ret` 为 NaN 两处早返回；本例输入均有效，故只可能是运行实例为不同版本。）

### C 代码根因（行号逐行核对）
| 报告主张 | 源码核验 | 结论 |
|---|---|---|
| **P0-1** `broker.py:131` 平仓 `stop` 取自 `plan.stop_price` | `broker.py:124-137` 为唯一 `TradeEvent` 构造点 `execute_plan`，`stop=plan.stop_price`（行 131）对**开+平统一**生效；无独立平仓路径。bug 真实存在（止损不取持仓实际止损） | ✅ CONFIRMED（行号对）；⚠️ 措辞修正：非"平仓路径"，是统一 `execute_plan` |
| **P0-2** `risk_gate.py:212` `price=quote.price if … else 0.0` | 行 212 原文一致；行 213-214 `price<=0 → return False,0.0,0.0,0.0`（仅拒开仓、**不进 HALT**） | ✅ CONFIRMED（行号与逻辑均对） |
| **P1-1** `scheduler.py:237-254` `signal_cooldown` 实现正确但 8-24 未生效 | 行 237-254 确为冷却逻辑；`configs/paper.yaml:69` `signal_cooldown.enabled:true`。代码+配置均存在 | ✅ 代码/配置 CONFIRMED；⚠️ "8-24 运行实例未生效"为运行期主张，**CANNOT VERIFY**（无二进制）；原报告自身已标注版本滞后可能 |
| **P1-2** `trade_intent.py` 翻译层未覆盖全部成交路径→两套计数脱节 | 深挖（`2026-08-26_p1-2_rootcause_deepdive.md`）证：权威源是 `logger.py:81` **无条件** `log_structured(EVT_TRADE)`（结构化 JSON，8-24 原始 841 行 → trade_id 去重 **330**/全周期 **337**）；`[意图]` 行（`logger.py:82`，受 `self._intent` 门控）8-24 仅 **74/330（22%）**。原报告拿可靠的 `[统计]` 去重 308 对比不可靠的 `[意图]` 74，伪矛盾。归因**错**；详见深挖报告 | ❌ 归因 REFUTED；🔎 真实根因=**审计脚本口径错配**（数 `[意图]` 子集而非 trade_id 去重 JSON）+ **多实例重放 2.52× 行膨胀** |

**C 组结论**：P0-1、P0-2 代码根因**成立且行号精确**；P1-1 代码/配置存在（运行期未生效不可验）；P1-2 归因**REFUTED**——真实根因为审计脚本以 gated `[意图]` 子集（8-24 仅 22% 覆盖）为口径、未对权威结构化 JSON 按 trade_id 去重，并叠加多实例重放 2.52× 膨胀（详见深挖报告）。

---

## 四、报告缺陷清单（须修订）

| # | 严重度 | 位置 | 问题 | 修订建议 |
|---|---|---|---|---|
| D1 | 🔴 | TL;DR#5、P1-2 | "可读 `[意图]` 日志（74）" 与 F5/audit_result 的 **81** 自相矛盾；74 仅为 8-24 当日 | 全文统一为 **81**，或显式写"8-24 当日 74 / 全周期 81" |
| D2 | 🔴 | P1-2 根因 | 原归因 `trade_intent.py` 翻译缺失**错误**；真实为：①审计脚本 `audit_trades_log.py` 以 gated `[意图]` 行（8-24 仅 74/330=22%）为统计口径，未对权威结构化 JSON（`logger.py:81` `log_structured`）按 `trade_id` 去重（真实 337）；②多实例会话重放致原始行 2.52× 膨胀；③日志内 `[统计]` 去重 308 反而正确。F5/F7/F8/F11 均基于 24% 偏样本 | 改为"审计脚本口径错配 + 重放污染"；`audit_trades_log.py` 改为解析结构化 JSON 并按 trade_id 去重重算 F5/F7/F8/F11；强制 `intent=True` 或明示 `[意图]` 非权威；P0-3 单实例锁消除重放 |
| D3 | 🟡 | 第二章 | 手续费写"约 −3400"，精确为 **−3277.97**（末行 净−毛利） | 改"≈ −3278" |
| D4 | 🟢 | 第六章 | `configs/paper.yaml` 写 161 行，实际 **160** 行 | 改 160（不影响引用行号 69 正确） |
| D5 | 🟢 | P0-1 措辞 | "平仓 stop 取自 plan" 实为统一 `execute_plan` 对开+平均取 `plan.stop_price` | 补"统一 `execute_plan` 路径" |
| D6 | 🟢 | 标签碰撞 | 代码注释称冷却为 "P0-2"，报告不足清单 P0-2 为 HALT——两套 P0-2 编号 | 提示编号约定统一（非事实错误） |

**无 REFUTED 的事实性结论**：原报告所有核心发现（对敲亏损、止损污染、行情缺失未熔断、版本差异、重启/网络脆弱性）均经独立验证成立。

---

## 五、对原报告"主理人裁决建议"的复核

- **P0-1（止损污染）**：✅ 确为真实一行级 bug（`broker.py:131` `stop=plan.stop_price`）——接实盘前必须清零，原建议成立。
- **P0-2（行情缺失 HALT）**：✅ `risk_gate.py:212/213-214` 证实仅拒开仓不进 HALT——原建议成立、优先级合理。
- **P0-3（多实例/重启自愈）**：✅ 17 次重启、`paper.pid` 存在风险、`load_snapshot` 可恢复持仓——原建议成立（注意 `broker.py:290-332` 已有快照恢复与损坏备份，但**无 pid 锁/看门狗**）。
- **P1-1（signal_cooldown 生效）**：⚠️ 代码与配置均存在；"8-24 未生效"无法对二进制验证，但建议"git 核对部署版本"正确且必要。
- **P1-2（审计源统一）**：⚠️ 缺口真实，但归因与修复指向应修正为"统一 human-readable 日志计数口径（trade_stats 周期 vs TradeLogger 逐笔）"，而非改 `trade_intent` 翻译。

**复核结论**：原报告主理人裁决建议**整体成立**，P0 三项为真实代码/设计缺陷，对接实盘前清零要求合理；仅 P1-2 的整改技术指向需按 D2 修正。

---

## 六、交付文件清单

- `deliverables/2026-08-26_trade_audit_verification.md` —— 本验证报告
- 证据源（未改动）：`data/paper/trades.log`、`data/paper/account.json`、`data/paper/audit_result.json`、`scripts/audit_trades_log.py`、`configs/paper.yaml`、`hexbroker/paper/{broker,risk_gate,scheduler,trade_intent}.py`

> 验证完成。原审计报告**可靠性 HIGH**：事实层 13/13 全通过且可复跑，代码根因 3/4 精确成立（P1-2 归因偏差已指出），建议按第四节 D1–D6 修订后归档。
