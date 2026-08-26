# 拦截门禁 P1/P2 修复交付报告（2026-08-25）

> 承接 `trade_gate_audit_20260825_am.md` 审计结论。本轮动手修复 P1（技术兜底 exp_ret 语义错位 + 隔夜过期软执行）与 P2（成本门禁日志刷屏）。

## TL;DR

| 项 | 修复前 | 修复后 | 状态 |
|---|---|---|---|
| P1-1 技术兜底 exp_ret 语义 | 把「上一日涨跌幅」当期望收益喂成本门禁 → 随机拦/放 | `exp_ret=0.0` 中性 + `is_effective=False`（降级提示，不构成 edge） | ✅ |
| P1-2 隔夜过期软执行 | 过期主源降级兜底，`intent` 未硬置 0，强多+涨可绕过门禁开仓 | 兜底 `is_effective=False` → `_intent` 返回 0 → 不触发成本门禁、不开仓（硬禁开契约闭合） | ✅ |
| P2 成本门禁日志刷屏 | 每 60s tick 重复打「成本门禁拦截开仓」warning（今上午 ag0 同信号 128 次） | 按 `(symbol, p_up, exp_ret, min_ratio)` 指纹去重，仅拒绝条件变化时打印 | ✅ |

回归测试：新增 `test_noneffective_signal_does_not_open`；相关套件 **102 passed / 0 fail**（risk_gate_cost + freshness + health_check + scheduler_stale_warn + cooldown + paper_pipeline + paper_signals + trade_intent + sessions + broker + signal_metrics_effective）。

---

## 一、P1-1 修复：技术兜底不再伪造期望收益

**文件**：`hexbroker/paper/signals.py` — `SignalEngine.technical_fallback`

**Before**
```python
prev = close.iloc[-2]
exp_ret = float((last / prev - 1.0) * 100.0) if prev > 0 else 0.0
...
return SignalFrame(
    ...,
    exp_ret=exp_ret,        # 实为「上一日已实现涨跌幅」，被冒用为「期望收益」
    is_effective=True,      # 伪装成有效信号，能驱动开仓
    source="technical",
)
```

**After**
```python
# exp_ret 不提供真实期望收益估计：技术指标仅给出方向，无法校准「预期日收益率」。
# 若把「上一日已实现涨跌幅」当 exp_ret 喂给成本门禁，会在「昨日跌+弱多」时误拦、
# 「昨日涨+弱多」时误放，完全取决于历史噪音，与未来期望无关（审计 P1-1）。
# 故 exp_ret 置中性 0.0，并令 is_effective=False（降级 substitute，不构成 edge）：
# 配合 P0-3 隔夜过期「无持仓禁开」硬约束，不会驱动任何新开仓（审计 P1-2）。
exp_ret = 0.0
...
return SignalFrame(
    ...,
    exp_ret=exp_ret,
    is_effective=False,     # 降级方向提示，不构成模型验证过的 edge
    source="technical",
)
```

**机理**：技术指标兜底只能给出方向（双均线/ATR 通道 → `p_up`），无法校准「预期日收益率」。伪造 `exp_ret` 会导致成本门禁的拦截与否**完全取决于历史噪音**（昨日涨/跌），与真实期望无关——这正是今上午「昨日跌+弱多 → 误拦」的根因。修复后，兜底信号被明确标记为「无效」，杜绝了按错误预期收益做决策的可能。

---

## 二、P1-2 修复：隔夜过期硬禁开契约闭合

**文件**：`hexbroker/paper/scheduler.py` — `_process_symbol`（注释硬化）+ 一（P1-1 的 `is_effective=False` 实体化）

**Before**：主源 `fd>阈值` 过期 → 降级 `technical_fallback`（`is_effective=True`，`exp_ret=昨日涨跌幅`）。当「弱多(>0.5) + 昨日涨(exp_ret>0)」时，成本门禁自相矛盾分支不触发 → `expected_pnl>0` → 直接放行开仓。调度器未硬置 `intent=0`，过期信号可绕过门禁。

**After**：
```python
if sig is None or not sig.is_effective:
    # 主源过期/缺失 → 技术兜底仅作降级方向提示（is_effective=False，不构成 edge）。
    # 配合 P0-3「无持仓禁开」硬约束：RiskGate._intent 对 is_effective=False 返回 0，
    # 不会触发成本门禁、不会新开仓（审计 P1-1/P1-2 闭合）；有持仓则仅风控管理。
    sig = self._signals.technical_fallback(symbol, bars)
```
- 实体化：`technical_fallback.is_effective=False` → `RiskGate._intent` 对该帧返回 0 → 成本门禁 `abs(intent)>1e-12` 条件不成立 → 门禁根本不被触发 → **不可能开仓**。
- 注释硬化：显式锚定「过期 → 无 edge → 硬禁开」契约，防止未来有人把兜底改回 `is_effective=True` 时意外放开。
- 有持仓场景不受影响：`_risk_manage_only` 与风控链仍照常运行（平仓/止损/S1–S5），仅「新开仓」被禁。

**回归锁**：`test_noneffective_signal_does_not_open` 断言 `is_effective=False` 信号经 `RiskGate.evaluate` 后 `target_position==0` 且 `reason != "cost_gate_reject"`（由 `_intent=0` 阻断，而非成本门禁）。

---

## 三、P2 修复：成本门禁警告去重

**文件**：`hexbroker/paper/risk_gate.py` — `RiskGate.__init__` + `evaluate`

**新增去重状态**
```python
# P2：成本门禁拦截警告去重（按品种 + 拒绝条件指纹），避免 60s tick 重复刷屏。
self._last_cost_reject_fp: dict[str, tuple] = {}
```

**evaluate 内去重逻辑**
```python
if not ok:
    cost_rejected = True
    intent = 0.0
    symbol = signal.symbol
    fp = (
        round(float(signal.p_up), 4),
        round(float(signal.exp_ret), 4),
        round(self._cost_gate_min_ratio, 2),
    )
    if self._last_cost_reject_fp.get(symbol) != fp:
        self._last_cost_reject_fp[symbol] = fp
        log.warning("成本门禁拦截开仓 ...", ...)   # 仅在拒绝条件变化时打印
# P2：本 tick 未触发成本门禁拦截 → 清除指纹，下次真实拒绝可重新告警
if not cost_rejected and signal is not None:
    self._last_cost_reject_fp.pop(signal.symbol, None)
```

**机理**：拒绝**决策本身与价格无关**（`expected_pnl` 与 `round_trip_cost` 均 ∝ `notional=price×multiplier`，比值价格无关），故指纹仅取 `(p_up, exp_ret, min_ratio)`（排除每 tick 变化的 `price`/`notional`）。同一品种同一拒绝条件只告警一次；信号变化（新 p_up/exp_ret）即重新告警，保证审计可追溯。

**效果**：今上午 ag0「同信号每 60s 刷 128 次」的噪音将收敛为**每交易日仅 1 次**（配合 P1-1 修复，过期兜底本身已不再触发成本门禁，故实际日志进一步归零）。

---

## 四、影响面与风险

- **不改变**正常交易日（主源 `fd=0` 新鲜）的开仓逻辑：新鲜主源 `is_effective=True` 照常经成本门禁评估。
- **不改变**已有持仓的风控动作（平仓/止损始终走 `evaluate`）。
- **唯一行为变化**：主源过期时不再有任何「基于历史噪音」的开仓——这正是 P0-3「隔夜过期→无持仓禁开」的**预期契约**，非矫枉过正。
- **根因仍待治本**：信号贫血（主源仅到 08-24）需在 08-25 收盘后做 P步-A/B 延伸（纳入 08-25 日线）恢复 `fd=0`，届时模型信号重新驱动开仓。

## 五、文件清单

| 文件 | 改动 |
|---|---|
| `hexbroker/paper/signals.py` | `technical_fallback`：`exp_ret=0.0` + `is_effective=False` + 文档注释（P1-1） |
| `hexbroker/paper/risk_gate.py` | 新增 `_last_cost_reject_fp` 去重状态 + `evaluate` 去重逻辑（P2） |
| `hexbroker/paper/scheduler.py` | `_process_symbol` 过期硬禁开契约注释硬化（P1-2） |
| `tests/test_risk_gate_cost.py` | 新增 `test_noneffective_signal_does_not_open`（P1-1/P1-2 回归锁） |
