# QA 独立复核报告：R3 滑点纳入成本门禁口径（2026-08-24）

> 复核人：严过关（QA）｜方式：fresh-eyes 独立复核，未采信工程师结论，逐项独立验证
> 复核对象：`hexbroker/paper/risk_gate.py`、`hexbroker/paper/scheduler.py`、`configs/paper.yaml`、
> `tests/test_risk_gate_cost.py`（新增 2 例）、`tests/test_signal_cooldown.py`（改 1 例）、
> `deliverables/2026-08-24_p0_cost_gate_cooldown.md`（R3 小节）
> 复核基线：HEAD = 15fbd16（acc6cf7 feat + 15fbd16 docs）；只读复核，未修改任何生产代码
> 原则：无证据不翻转；数量级独立复算，不采信工程师口头数字

---

## 一、公式核验（含数量级检查 — 重点）

### 1.1 代码公式（`risk_gate.py::_cost_gate_pass`，R3 起）

```text
fee_part      = notional × (fee_open + fee_close_today)
slippage_part = 2.0 × _min_tick(symbol) × slippage_ticks × multiplier(symbol)   # 仅 slippage_in_cost=True 时计入
round_trip_cost = fee_part + slippage_part
```

**量纲核验**：`min_tick`（元/吨）× `slippage_ticks`（无量纲）× `multiplier`（吨/手）= 元/手（单边）；×2 = 元/手（往返）。与 `CostModel.trade_cost` 单边滑点 `min_tick×slippage_ticks×multiplier×qty` 结构一致（R3 为往返故 ×2）。**公式结构 PASS。**

### 1.2 数量级检查（关键：¥20 vs ¥200 争议）

结论：**生产路径实际为 ¥20/手往返，工程师数字正确；¥200 只在"无品种合约回退"场景出现。**

| 路径 | `_min_tick('rb0')` 取值 | 滑点往返 | 依据 |
|---|---|---|---|
| **生产**（`broker.build_cost_model(paper.yaml)`） | **1.0** | **2×1×1×10 = ¥20** | `build_cost_model` 从 `paper.symbols.rb0.min_tick=1` 构造 contracts → `_min_tick` 命中 contracts 返回 1.0 |
| 测试/复跑脚本（`contracts={"rb0":{min_tick:1.0}}`） | 1.0 | ¥20 | 与生产一致 |
| 回退兜底（symbol 不在 contracts 且不在 `_SPEC_MIN_TICK`） | **10.0**（CostModel 默认 / paper.yaml cost.min_tick） | **¥200** | 仅非上市/未声明品种触发，见遗留风险 R3-1 |

**工程师口头数字勘误**：其消息中"含滑点 2×10×1×10=¥20"算术自相矛盾（2×10×1×10=200≠20）；
实际为 **2×1×1×10=20（min_tick=1）**。**提交文档（15fbd16）表述正确**（明确写 `min_tick(rb0)=1.0` → ¥20），
代码与文档无缺陷，仅工程师聊天摘要写法不严谨，建议口头层面纠正、无需改码。

### 1.3 独立复算（生产参数，price=3038）

| 项 | 值 | 说明 |
|---|---|---|
| notional | 30380.00 | 3038×10 |
| fee_part | 4.557 | 30380×0.00015 |
| slippage_part | 20.00 | 2×1×1×10 |
| round_trip_cost | 24.557 | 工程师报 ¥24.5 ✓ |
| 门槛（2×） | 49.114 | 工程师报 ¥49 ✓ |
| exp_ret=0.170757% → expected_pnl | 51.876 | 0.170757/100×30380 |
| **覆盖倍数（新口径）** | **2.112×** | 工程师报 2.1× ✓ |
| 覆盖倍数（旧纯费口径） | 11.384× | 工程师报 11.4× ✓ |
| 门禁判定 | 51.876 > 49.114 → **通过** | 与"档 3 未被门禁拦截"一致 ✓ |

**结论：数量级正确，工程师数字（¥24.5/¥49/2.1×/11.4×）经独立复算全部吻合。**

### 1.4 与 CostModel 口径一致性

- `trade_cost` 单边滑点 = `min_tick×slippage_ticks×multiplier×qty`；R3 往返 = 2×单边 ✓ 一致。
- 手续费：`trade_cost.fee` 以 fill_price（价+滑点）计费，R3 `fee_part` 以 quote.price 计费 —— 差异 = 费率×滑点×乘数 ≈ 0.0015 元/边，可忽略；且与 P0-1 原口径一致（非 R3 引入）。**PASS（附注）。**
- 参数全部取自注入 CostModel 的公有属性（fee_open/fee_close_today/slippage_ticks）与私有方法（_min_tick/_multiplier），与 CostModel 实际 API 一致；scheduler 复用 `broker.cost`（`build_cost_model` 产物）注入 `set_cost`，不新增记账代码 ✓。

---

## 二、测试核验

### 2.1 运行结果（独立执行）

| 测试文件 | 结果 |
|---|---|
| `tests/test_risk_gate_cost.py` | **8/8 passed**（含 R3 新增 ⑥⑦） |
| `tests/test_signal_cooldown.py` | **6/6 passed**（含改动后 ③） |
| 关联套件（test_paper_broker / test_paper_pipeline / test_cost_model / test_cost_multiplier_spec / test_broker_multiplier_pnl） | **55/55 passed** |

无断言性失败；与工程师"新增+关联 25/25 绿"一致（QA 实测 8+6=14 直接相关 + 41 关联）。

### 2.2 新增用例数值有效性（边界抽查）

- **用例⑥** exp_ret=0.06%：expected_pnl=18.23；纯费门槛 2×4.557=9.11 → 18.23>9.11 **放行**；含滑点门槛 2×24.557=49.11 → 18.23<49.11 **拒开**。**确实落在"仅费过/含滑点拒"区间（0.030%–0.162%），构造数值有效** ✓；同一信号 `slippage_in_cost=False` 放行（QA 实测 reason=rl_intent）✓
- **用例⑦** exp_ret=0.2%：expected_pnl=60.76 > 49.11 → **含滑点仍放行** ✓（QA 实测 reason≠cost_gate_reject）
- 文档声称"被拦截区间 exp_ret ∈ (0.03%, 0.163%)"，QA 独立复算 price∈[3030,3038] 得 **(0.0300%, 0.1617%~0.1620%)** ✓ 一致。

### 2.3 重点裁决：test_signal_cooldown.py 信号变化用例 exp_ret 0.1%→0.3%

**裁决：合理适配，非掩盖问题。**

证据链：
1. **改动未触及冷却语义**。`_last_sig_fp` 指纹比较逻辑、更新时机（仅实际开仓 `event.is_open` 时更新）、容差阈值均未改动（git diff 仅 2 行：exp_ret 0.1→0.3 及对应断言）。
2. **0.1% 在新口径下被拒是预期行为而非回归**。QA 复算：exp_ret=0.1% → expected_pnl=30.38 < 门槛 49.11 → 门禁拦截（reason=cost_gate_reject），这正是 R3 设计目标"低质量信号（覆盖倍数 < 2×）不放行"；0.1% 恰落在新增的拦截区间 (0.03%, 0.163%) 内。
3. **若不改，该测试会因门禁（与冷却无关的组件）而失败**，断言 `position>0` 不成立；改为 0.3%（expected_pnl=91.14 > 49.11 通过门禁）后，用例仍完整覆盖冷却语义：信号变化 → 冷却解除 → 开仓 + 指纹更新为 (0.55, 0.3, "test")。**冷却逻辑若被破坏，该用例仍会红**——测试效力未削弱。
4. 全仓 grep：无其他用例使用 exp_ret=0.1% 依赖旧口径；改动无连带影响。

**结论：测试改动是为了把用例聚焦在冷却语义上、绕开门禁新口径的无关干扰，属于标准测试适配；不构成掩盖冷却缺陷。**

---

## 三、复跑核对（独立执行 `python scripts/replay_0824_cost_cooldown.py --rounds 60`）

| 档位 | 工程师声称 | QA 独立复跑 | 核对 |
|---|---|---|---|
| 1 修复前（min_bars=1 band=0 无门禁无冷却） | 30开 30平 | 30开 30平（末态 0.0） | ✓ |
| 2 仅 cb37333（min_bars=2 band=0.1×ATR） | 3开 2平 | 3开 2平（末态 1.0） | ✓ |
| 3 cb37333 + P0-1 + P0-2 | 1开 1平 | 1开 1平（末态 0.0） | ✓ |

- 日志佐证：档 3 全程打印「信号未变化…冷却拦截重复开仓」，**未出现「成本门禁拦截」** —— 与复算一致（exp_ret=0.171% 覆盖 2.112× > 2×，门禁放行；重复开仓由冷却消除）。
- 注意：复跑脚本 `_paper_cfg` 的 risk_gate 段未显式写 `slippage_in_cost`，走 scheduler 默认 true → R3 口径已生效；若未来想复跑"纯费口径"对比，需在脚本中显式加 `slippage_in_cost: false`（小改进建议，非缺陷）。

---

## 四、边界推演

| # | 边界 | 结论 | 依据 |
|---|---|---|---|
| ① | `cost=None` 仍整段跳过 | **PASS** | `evaluate` 触发条件含 `self._cost is not None`；构造 `cost=None` 实测 exp_ret=0.001% 照常开仓（reason=rl_intent，无 cost_gate_reject）→ 向后兼容不变 |
| ② | `slippage_in_cost=False` 回纯费口径 | **PASS** | `slippage_part=0.0` → round_trip=fee_part；实测 exp_ret=0.06% 放行（18.23>9.11，reason=rl_intent）、exp_ret=0.001% 拒开（纯费口径正确拦截）——开关双向均生效 |
| ③ | 新用例数值确实落在"仅费过/含滑点拒"区间 | **PASS** | 见 §2.2；exp_ret=0.06% ∈ (0.030%, 0.162%) ✓ |
| ④ | round_trip_cost≤0 防御 | **PASS** | 存在（`<=0 → 放行`）；正常参数下恒 >0，不构成漏洞 |
| ⑤ | 开关默认值一致性 | **PASS** | RiskGate 默认 true、scheduler 读取 `slippage_in_cost` 默认 true、paper.yaml 显式 true，三处一致 |

---

## 五、QA 裁决

### 裁决：**PASS**（R3 滑点纳入成本门禁口径，通过独立复核）

- 公式结构与 CostModel 一致，数量级经独立复算无误（生产 rb0 滑点往返 ¥20，非 ¥200）；
- 新增用例数值真实落在目标区间，测试改动为合理适配；
- 复跑三档数字与工程师完全一致；边界①cost=None、②开关回退、③区间抽查全部通过。

### 遗留风险（不阻断，建议跟进）

- **R3-1（低）**：`cost.min_tick: 10.0`（paper.yaml）与 `CostModel.min_tick=10.0` 兜底值对 rb 类品种物理上不正确（rb 真实 tick=1 元/吨）。当前生产品种 ag0/rb0/c0 均在 `paper.symbols` 显式声明 min_tick（0.01/1/1），`build_cost_model` 构造 contracts 覆盖 → 生产正确；但**未来新增品种若漏声明 min_tick**，`_min_tick` 回退 10.0 → 滑点 10× 虚高（如 rb1 → ¥200/手）。方向为**过严拦截（保守）**，不会造成不安全开仓；建议：(a) `_SPEC_MIN_TICK` 补 rb/c 等常用品种，或 (b) 新增品种强制显式 min_tick，或 (c) 未知品种 fail-loud。
- **R3-2（提示）**：`fee_part` 按 quote.price 计费，与 `trade_cost` 按 fill_price 计费存在 ≤0.003 元/手的理论差（P0-1 遗留口径，非 R3 引入），可接受。
- **R3-3（提示）**：工程师聊天摘要"2×10×1×10=¥20"算术笔误（应为 2×1×1×10），文档正确；建议对外表述以文档为准。

### 提交核对
- acc6cf7（feat）+ 15fbd16（docs），HEAD=15fbd16，与任务描述一致；只读复核未产生任何代码改动。
