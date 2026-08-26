# R3-1 min_tick 兜底表补全 — QA 独立复核报告（fresh-eyes）

- **复核人**：Edward（QA Engineer，software-r31）
- **日期**：2026-08-24
- **复核对象**：commit `308ef49`（feat）+ `5a0c73a`（docs），HEAD=`5a0c73a`
- **复核方式**：只读复核，未改动任何生产代码；独立执行代码审查 + 测试运行 + 边界推演
- **复核结论**：**PASS**（含 2 项非阻塞建议 / 已知遗留，详见 §6）

---

## 1. 代码审查（逐项）

### ① 兜底表 rb/c 取值 vs paper.yaml 显式声明 — ✅ 一致

| 品种 | paper.yaml（configs/paper.yaml symbols 段） | `_SPEC_MULTIPLIER` | `_SPEC_MIN_TICK` |
|---|---|---|---|
| rb0 | `multiplier: 10, min_tick: 1`（L98-102） | `"rb": 10.0` ✅ | `"rb": 1.0` ✅ |
| c0 | `multiplier: 10, min_tick: 1`（L107-111） | `"c": 10.0` ✅ | `"c": 1.0` ✅ |

- 兜底值完全对齐 paper.yaml 显式声明，无凭空取值。
- 未改动 au(0.02/×1000)、ag(0.01/×15)、m(1.0/×10) 既有物理值——符合「无证据不翻转」铁律。
- 未覆盖品种（cu0 等）仍回退全局默认（10.0），保守方向不破坏（独立验证见 §3.4）。

### ② `_warn_fallback_once` 实现 — ✅ 达标（1 项次要说明）

- **只 warn 一次（set 去重）**：模块级 `_WARNED_FALLBACK: set[str]`，key=`f"{kind}:{_short_symbol(symbol)}"`。独立验证：400 次不同未声明品种调用仅产生 2 条 warning（MIN_TICK:cu + MULTIPLIER:zn 各 1 条）；同一 symbol 连续 50 次仅 1 条。
- **不刷屏**：key 按 `_short_symbol` 归一化，`cu0`/`cu2509`/`cu2510` 共享同一 key → 整个 cu 家族只告警一次，有界、不随 tick 增长。✅
- **对 None symbol 不 warn**：`key` 为空即 return。独立验证 `None`/`""` 均 0 条 warning。✅
- **线程安全（次要说明）**：`in` 检查 + `add` 非原子，极端并发下同一新 key 首次回退可能重复告警（最多多打一条日志），不影响任何计算值（值本身确定性）。回测/纸面/RL 均为单线程逐 symbol 调用，实际不触发。属可接受的良性竞态，非缺陷。

### ③ 接入点 `_min_tick`/`_multiplier` — ✅ 仅回退时告警

- contracts 命中：`if symbol and self.contracts and symbol in self.contracts: return ...` 提前返回，**不**经过告警路径。✅
- 规格表命中：`if key in _SPEC_*: return ...` 提前返回，**不**告警。✅
- 仅规格表未命中、即将回退全局默认时调用 `_warn_fallback_once`。✅
- 独立验证：contracts 命中 + 规格表命中两条路径均 0 条 warning；仅 cu0 回退时 1 条。✅

### ④ 向后兼容 — ✅ 行为不变（除修复项与新增告警）

- `CostModel.from_config` 在 `cfg.backtest is None` 时返回 `cls()`（contracts=None）→ 与改动前相同路径，仅：
  - rb/c 的 min_tick 由回退 10.0 → 规格表 1.0（**本次修复目的**）；
  - 未声明品种回退时多一条一次性 warning（新行为，非破坏性）。
- au/ag/m、cu0 等其余品种取值完全不变。
- 全仓检索确认：除 `tests/test_cost_multiplier_spec.py` 外，无其他测试依赖「rb/c 回退=10.0」的旧行为（`test_broker_multiplier_pnl.py` 用 `slippage_ticks=0.0`+显式全局参数，`test_paper_broker.py`/`test_paper_pipeline.py` 均在 contracts 显式声明 rb0/c0 min_tick=1，走 contracts 命中路径，不受影响）。✅

---

## 2. 测试核验（独立运行）

环境：Python 3.13.14 / pytest 9.1.1（win32）。结果经 `--junitxml` 解析确认（终端输出被环境 safe-delete 包装干扰，非测试失败）。

| 套件 | 结果 | 说明 |
|---|---|---|
| `tests/test_cost_multiplier_spec.py`（R3-1 主文件） | **7/7 通过** | 含原 2 例更新 + 新增 2 例 + 既有 3 例，全绿 |
| `tests/test_cost_model.py` | 6/6 通过 | 成本模型精确性 |
| **cost 相关合计** | **13/13 通过** | 与工程师声明一致 ✅ |
| `tests/test_risk_gate_cost.py` | 8/8 通过 | 成本门禁 |
| `tests/test_broker_multiplier_pnl.py` | 5/5 通过 | 乘数/P&L |
| `tests/test_paper_broker.py` | 14/14 通过 | 纸面撮合 |
| `tests/test_signal_cooldown.py` | 6/6 通过 | 信号冷却 |
| `tests/test_paper_pipeline.py` | 11/11 通过 | 纸面管线 |
| **paper/risk/broker 相关合计（本复核选取）** | **44/44 通过** | ✅ |
| `tests/test_paper_signals.py` | 23 例中 **4 failed** | **全部为环境性**：`ImportError: pyarrow/fastparquet 缺失`（parquet 数据读取路径），与 cost.py 无关；非断言性失败 |

**环境性失败判定**：4 个失败均为 pandas read_parquet 缺 pyarrow/fastparquet 引擎，属本机依赖缺失；测试路径不涉及成本模型，基线一致，与 R3-1 改动无因果关系。**未发现任何断言性失败**。

---

## 3. 边界推演（独立脚本验证）

### 3.1 大量不同未声明品种循环 → 告警有界 ✅
400 次调用（cu0..cu199 min_tick + zn0..zn199 multiplier）→ 仅 2 条 warning（每 kind:key 1 条），`_WARNED_FALLBACK` 大小=2。去重生效、不刷屏。

### 3.2 contracts 含品种但缺 min_tick 字段 → 静默回退全局默认 ⚠️（见 §6 遗留-1）
`self.contracts[symbol].get("min_tick", self.min_tick)`：当 contracts 含该 symbol 但其 dict 缺 `min_tick` 键时，**静默**回退全局 10.0，**不经过** `_warn_fallback_once`（独立验证：`contracts={"rb0": {"multiplier": 10.0}}` → `_min_tick("rb0")==10.0`，0 条 warning）。此路径与 R3-1 修复的 10× 虚高同源（同一静默风险），但生产 paper.yaml 全部 symbol 均显式声明 min_tick，实际不触发。

### 3.3 `_short_symbol` 规范化命中新表 ✅
独立验证：`"SHFE.rb"→"rb"`、`"rb0"→"rb"`、`"RB0"→"rb"`、`"DCE.c"→"c"`、`"c0"→"c"`、`"au2408"→"au"`、`"SHFE.au"→"au"`、`None/""→None`、`"IF2509"→"if"`。带点前缀/尾部数字/大小写均可归一化并命中新表。

### 3.4 核心修复数值核验 ✅
- rb0 单边滑点：`trade_cost(3038, 1, open, "rb0")` → slip=**10.0**（=1.0×1×10×1），成交价 3039.0 ✅
- c0 平仓 slip=10.0；往返滑点 = **20.0**（修复前 200.0，10× 虚高消除）✅
- 未覆盖品种 cu0：min_tick=**10.0**、multiplier=**10.0**（保守回退不变）✅
- contracts 优先：`{"rb0":{"min_tick":2.0,"multiplier":20.0}}` → 2.0/20.0；同短名 rb1 未配置 → 兜底 1.0 ✅

### 3.5 其他
- 测试隔离（次要说明）：`_WARNED_FALLBACK` 为模块级全局状态，跨测试持久。当前 R3-1 测试不断言 warning，无影响；若未来新增「断言告警」的测试需先清空该 set。
- 文档小瑕疵（非代码问题）：`test_r3_1_min_tick_fallback_rb_c` docstring 中「单边滑点 2×10×1×10=¥200」将往返口径写进「单边」表述，易读歧义；断言本身正确（单边 10.0、往返 20.0），不构成缺陷。

---

## 4. 复核裁决

### ✅ PASS

- 兜底表 rb/c 取值与 paper.yaml 显式声明一致（rb/c: multiplier=10, min_tick=1）。
- `_warn_fallback_once`：按 (kind, key) 去重只告警一次、不刷屏、None symbol 不告警（均已独立验证）。
- 告警仅挂接在回退路径，contracts/规格表命中均不告警。
- 向后兼容：除修复项（rb/c min_tick 10→1）与新增一次性告警外，行为不变。
- 测试：R3-1 文件 7/7、cost 相关 13/13、paper/risk/broker 选取 44/44 全绿；4 个 paper_signals 失败均为 pyarrow 环境性缺失，与本次改动无关。
- 修复目标达成：rb0/c0 兜底滑点 ¥200 → ¥20（10× 虚高消除）。

## 5. 遗留 / 建议（均非阻塞，不构成 FAIL）

1. **（建议）contracts 缺 min_tick 字段的静默回退**：`contracts[symbol].get("min_tick", self.min_tick)` 路径缺字段时静默回退全局 10.0 且不告警，与本次修复的静默风险同源。生产 paper.yaml 全量显式声明故当前不触发；建议后续在 `from_config` 或该 get 路径补一条 warning（或配置校验），消除该静默缺口。
2. **（建议）告警状态测试隔离**：`_WARNED_FALLBACK` 为模块级全局，未来若新增告警断言测试需显式清空；可考虑提供 reset 钩子或 fixture。
3. **（备注）并发竞态**：check-then-add 非原子，极端并发下同 key 首次回退可能重复告警（不影响值），当前单线程场景无实际影响。
4. **（备注）测试 docstring 口径**：`test_r3_1_min_tick_fallback_rb_c` 的「单边/往返」表述可优化，断言不受影响。

## 6. 测试环境备注

- 终端 pytest 输出被环境 `[safe-delete]` 包装干扰（在 `[100%]` 后吞掉汇总行），本次全部结果改用 `--junitxml` 解析确认，避免误判。
- 全量 20 failed / 15 errors 的环境性结论与工程师基线声明一致（本复核聚焦 cost/paper/risk/broker 受影响面，未全量复跑）。
