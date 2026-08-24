# P0 开仓成本门禁 + 信号无变化冷却（2026-08-24）

## 背景
模拟盘全天净亏 ¥5,129.42，根因：S1 趋势破坏被日线 MA20 贴线穿越反复触发 →
60s 开-平-开-平循环（164 闭环），手续费占净亏 66.8%。
已修复（勿回滚）：`cb37333`（S1 开仓缓冲 + MA 带宽死区 + _prev_stop 按品种隔离）、
`27f3572`（is_open 统一 bool）。
本任务补齐两个决策链断点：
1. 信号帧 `exp_ret` 是死字段，开仓决策从不消费（无成本/净期望检查）；
2. 信号恒定不变时仍反复驱动新开仓（无冷却）。

## P0-1 开仓成本门禁（`hexbroker/paper/risk_gate.py`）
- 仅对**新开仓**（position≈0 且 intent≠0 且信号有效）施加。
- 公式（单手口径，乘数取自 `CostModel`，不新增记账代码）：
  - `notional = price × multiplier(symbol)`
  - `expected_pnl = direction × exp_ret/100 × notional`（exp_ret 为日收益百分比，带符号）
  - `round_trip_cost = notional × (fee_open + fee_close_today)`
  - 通过条件：`expected_pnl > round_trip_cost × cost_gate_min_ratio`（默认 2.0）
- 同时校验「p_up 方向与 exp_ret 符号一致性」：多头却预期下跌 / 空头却预期上涨 →
  模型自相矛盾，自动拦截。
- 不通过 → `intent=0`，`decision.reason="cost_gate_reject"`（供日志审计）。
- 向后兼容：不传 `cost` → 门禁跳过（现有测试语义不变）；`cost_gate_enabled=false` 可关停。
- 接线：scheduler 从 `broker.cost` 复用注入（`RiskGate.set_cost`），无需改主入口组件工厂。

## P0-2 信号无变化冷却（`hexbroker/paper/scheduler.py`）
- 指纹：`(p_up, exp_ret, source)`，按品种记录「上一轮实际开仓」的信号。
- 本轮信号与上轮指纹相同（`p_up` 差 < `p_up_tol` 且 `exp_ret` 差 < `exp_ret_tol` 且 source 相同）
  → 即使风控返回开仓意图也跳过（`decision.target_position=0`，`reason="signal_cooldown"`）。
- 边界：
  - 只拦「无持仓 + 意图开仓」；**已有持仓的风控动作（止损/止盈/S1-S5）永远照常**；
  - 信号变化（指纹不同）→ 解除冷却，正常开仓并更新指纹；
  - 初始无指纹 → 允许开仓并记录指纹；
  - 技术兜底与主源切换（source 不同）→ 视为信号变化；
  - 指纹仅在**实际开仓成交**时更新（预算拒绝不更新 → 下次可重试）。

## 配置（`configs/paper.yaml`）
```yaml
risk_gate:
  cost_gate_enabled: true
  cost_gate_min_ratio: 2.0
signal_cooldown:
  enabled: true
  p_up_tol: 0.01
  exp_ret_tol: 0.001
```

## 复跑验证（`scripts/replay_0824_cost_cooldown.py`，60 轮/60s 步长）
场景：rb0 价格 3038↔3036 交替、MA20=3037、p_up=0.733333、exp_ret=0.170757%、ATR≈40、
费率 0.00005/0.00010、乘数 10，走真实调度器决策链：

| 档位 | 配置 | 开仓 | 平仓 | 末态持仓 |
|---|---|---|---|---|
| 1 修复前 | min_bars=1 band=0 无门禁无冷却 | 30 | 30 | 0.0 |
| 2 仅 cb37333 | min_bars=2 band=0.1×ATR | 3 | 2 | 1.0 |
| 3 cb37333+P0-1+P0-2 | min_bars=2 band=0.1×ATR + 门禁 + 冷却 | 1 | 1 | 0.0 |

说明：档 2 平仓由 S4 时间止损（持仓 20 根未盈利）驱动；档 3 信号恒定 → 冷却拦截每次
平仓后的重复开仓（1开1平，不再循环）。rb0 信号 exp_ret=0.171% 的净期望收益本身覆盖
往返成本（约 11.4× > 2×），成本门禁未拦截该信号——成本门禁主要拦截 ag0 等
低质量/方向矛盾信号（方向一致性校验）。

## 单元测试（新增 12 例）
- `tests/test_risk_gate_cost.py`（6）：覆盖成本→开仓；不覆盖→拒开+reason 标注；
  方向矛盾→拒开；不传 cost→门禁跳过；cost_gate_enabled=false→关闭；已有持仓→不拦截风控。
- `tests/test_signal_cooldown.py`（6）：信号未变+无持仓→不开仓；信号未变+有持仓→风控照常；
  信号变化→开仓并更新指纹；初始无指纹→开仓；source 变化→视为变化；容差内微变→仍拦截。

## 测试结果
- 新增 12/12 通过。
- 本环境缺少可选依赖（pydantic/pyarrow/sklearn/scipy/torch/optuna/lightgbm），
  15 个测试模块无法收集、20 个用例因 ImportError 失败——均为环境缺失，与本次改动无关；
  paper/risk 相关全部通过（含 test_paper_pipeline / test_sell_engine 等）。
- 生产环境全量 pytest 需在完整依赖环境复跑确认（基线 344 绿）。

## 提交说明（git 仓库对象损坏，无法在本地生成 commit）
本工作区 `.git` 对象库缺历史提交对象（HEAD=27f3572 的 commit/tree/blob 缺失，
`git write-tree` 报 invalid object；`git fetch origin` 因无凭据失败；且
`公众号文章/*` 14 个已跟踪文件被其他进程修改、原 blob 不可恢复），无法在本地完成提交。
建议在完整仓库中按以下两个逻辑提交落地：

1. `feat(p0): cost gate + signal cooldown for open-order`
   文件：`hexbroker/paper/risk_gate.py`、`hexbroker/paper/scheduler.py`、
   `configs/paper.yaml`、`tests/test_risk_gate_cost.py`、`tests/test_signal_cooldown.py`、
   `scripts/replay_0824_cost_cooldown.py`
2. `docs(p0): P0-1/P0-2 成本门禁+信号冷却实现说明与复跑验证`
   文件：`deliverables/2026-08-24_p0_cost_gate_cooldown.md`（本文档）

---

## R3 滑点纳入成本门禁口径（2026-08-24 追加）

### 背景
P0-1 门禁的 `round_trip_cost` 原仅含手续费（`notional×(fee_open+fee_close_today)`），
未计入滑点。rb0 单边滑点 1 tick=¥10/手、往返 ¥20 > 往返手续费 ¥4.5：rb0 信号
exp_ret=0.171% 对成本门禁的覆盖倍数由费口径 11.4× 降至含滑点约 2.1×（余量薄，
低质量信号可能被放行）。本次把滑点纳入往返成本口径。

### 公式（`hexbroker/paper/risk_gate.py::_cost_gate_pass`，R3 起取代上方纯费口径）
```text
round_trip_cost = 手续费部分 + 滑点部分
手续费部分 = notional × (fee_open + fee_close_today)
滑点部分   = 2 × min_tick(symbol) × slippage_ticks × multiplier(symbol)   # 单边滑点 × 双边
```
参数取自注入的 CostModel（scheduler 从 broker.cost 复用注入 `set_cost`，不新增记账代码）：
- `fee_open` / `fee_close_today`（费率，rb0=0.00005/0.00010）；
- `min_tick(symbol)`（品种级私有方法 `_min_tick`，rb0=1.0）；
- `slippage_ticks`（rb0=1.0）；
- `multiplier(symbol)`（品种级私有方法 `_multiplier`，rb0=10.0）。

开关：`configs/paper.yaml` → `risk_gate.slippage_in_cost`（默认 `true`）；
`false` 时滑点部分按 0 计，恢复纯费口径。`cost=None`（未注入）时门禁整段跳过（向后兼容不变）。

### 影响
- rb0 往返成本由 ¥4.5 → ¥24.5（含滑点 ¥20）；门禁门槛由 2×¥4.5=¥9 → 2×¥24.5=¥49。
- 仅费口径通过、含滑点被拒的信号（rb0 多头 exp_ret ∈ (0.03%, 0.163%)）现在会被拦截，
  低质量信号不再被放行。
- 8/24 rb0 信号 exp_ret=0.171%：expected_pnl=¥51.9 > ¥49 → 仍通过；覆盖倍数 11.4× → 约 2.1×。

### 复跑结果（`scripts/replay_0824_cost_cooldown.py --rounds 60`）
| 档位 | 配置 | 开仓 | 平仓 | 末态持仓 |
|---|---|---|---|---|
| 1 修复前 | min_bars=1 band=0 无门禁无冷却 | 30 | 30 | 0.0 |
| 2 仅 cb37333 | min_bars=2 band=0.1×ATR | 3 | 2 | 1.0 |
| 3 cb37333+P0-1+P0-2 | min_bars=2 band=0.1×ATR + 门禁（含滑点）+ 冷却 | 1 | 1 | 0.0 |

档 3 维持 1开1平（rb0 含滑点覆盖倍数约 2.1× 仍 > 2×，门禁未拦截该信号；重复开仓
由信号冷却消除，与 R3 前一致）。

### 测试
- `tests/test_risk_gate_cost.py` 新增 2 例（共 8）：
  - ⑥ 仅费口径通过、含滑点口径被拒（exp_ret=0.06%）→ reason=cost_gate_reject；
    同一信号 `slippage_in_cost=False` → 放行（验证开关）；
  - ⑦ 含滑点仍通过（exp_ret=0.2%）→ 开仓。
- `tests/test_signal_cooldown.py` 信号变化用例的信号 B 由 exp_ret=0.1% 调至 0.3%：
  0.1% 在新口径下被成本门禁拦截（与冷却语义无关）；0.3% 仍通过门禁且保持「信号变化 → 开仓」语义。
- paper/risk 相关用例全绿；全量 pytest 环境性失败（缺 pyarrow/pydantic 等）与本次无关。

### 提交
- `feat(hexbroker): R3 滑点纳入成本门禁往返成本口径`
  文件：`hexbroker/paper/risk_gate.py`、`hexbroker/paper/scheduler.py`、
  `configs/paper.yaml`、`tests/test_risk_gate_cost.py`、`tests/test_signal_cooldown.py`
- `docs(hexbroker): R3 滑点成本口径说明`
  文件：`deliverables/2026-08-24_p0_cost_gate_cooldown.md`（本文档）

---

## R3-1 min_tick 兜底补全（2026-08-24 追加，QA R3 复核遗留）

### 背景
`hexbroker/backtest/cost.py` 的品种规格兜底表 `_SPEC_MIN_TICK` 原仅覆盖
au(0.02)/ag(0.01)/m(1.0)。当 `CostModel.contracts` 未提供某品种（回测/RL 环境、
或品种未显式声明）时，`_min_tick(symbol)` 走兜底：rb/c 不在表内 → 回退全局
`min_tick=10.0` → 单边滑点 10×1×10×1=¥100、往返 ¥200（正确应为 1×1×10×1=¥10
单边、往返 ¥20），滑点成本 10× 虚高。方向保守（不会不安全开仓），但口径虚高。

生产模拟盘路径不受影响：`configs/paper.yaml` symbols 段已显式声明
ag0(mult=15,min_tick=0.01)、rb0(mult=10,min_tick=1)、c0(mult=10,min_tick=1)，
contracts 优先于兜底表。

### 改动（`hexbroker/backtest/cost.py`）
- `_SPEC_MIN_TICK` 补 `"rb": 1.0, "c": 1.0`（依据 paper.yaml rb0/c0 `min_tick: 1`
  显式声明；无证据不翻转其他品种物理值，其余品种保持回退全局默认=保守方向）。
- `_SPEC_MULTIPLIER` 补 `"rb": 10.0, "c": 10.0`（依据 paper.yaml rb0/c0
  `multiplier: 10` 显式声明；与全局默认一致，仅为表完整性）。
- `_min_tick`/`_multiplier` 兜底回退时新增告警：品种未在规格表声明、回退全局
  默认时 `logging.warning` 一条（提示「回退全局 min_tick/multiplier」），
  按 (kind, symbol) 去重仅告警一次，避免静默 10× 虚高且不刷屏；`symbol=None`
  不告警。

### 影响
- rb/c 兜底路径滑点从 ¥200 修正为 ¥20（10× 虚高消除），与 paper.yaml 显式声明口径一致。
- 未声明品种（如 cu0）仍回退全局默认（保守方向不破坏），并可见告警提示。

### 测试（`tests/test_cost_multiplier_spec.py`，新增 2 例 + 原 2 例断言更新）
- 新增 `test_r3_1_min_tick_fallback_rb_c`：`_min_tick("rb0")==1.0`、
  `_min_tick("c0")==1.0`、`_multiplier("rb0")==10.0`、未覆盖品种 `_min_tick("cu0")==10.0`
  （保守回退）、rb0 单边滑点 ¥10/往返 ¥20。
- 新增 `test_r3_1_contracts_provided_take_priority`：contracts 提供时优先
  （`{"rb0":{"min_tick":2.0,"multiplier":20.0}}` → 2.0/20.0）；同短名未配置合约
  （rb1）仍走兜底表 1.0。
- 原 `test_spec_min_tick_fallback_when_contracts_none` / `test_spec_multiplier_fallback_when_contracts_none`
  更新：rb/c 断言由「回退全局 10.0」改为「规格表 1.0/10.0」，并补 cu0 回退断言。
- 结果：cost 相关 13/13 通过；paper/risk/broker 相关 49/49 通过；
  全量 pytest `20 failed, 289 passed, 11 skipped, 15 errors`——failed/errors 均为
  本环境缺 pyarrow/pydantic/RL 依赖导致，与本次改动无关（基线一致）。

### 提交
- `feat(hexbroker): R3-1 补全 _SPEC_MIN_TICK 兜底（rb/c）+ 缺声明 warn`
  文件：`hexbroker/backtest/cost.py`、`tests/test_cost_multiplier_spec.py`
- `docs(hexbroker): R3-1 min_tick 兜底说明`
  文件：`deliverables/2026-08-24_p0_cost_gate_cooldown.md`（本文档）
