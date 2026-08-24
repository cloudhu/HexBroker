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
