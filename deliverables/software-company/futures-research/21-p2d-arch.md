# P2-D 运行时动态降级 · Architect 增量设计 + 任务分解

> 输入：`20-p2d-prd.md`（P2-D1~D4 + Q1~Q5 全采纳）· 约束：默认零行为变更 / 红线零顶层重依赖 / 612 全绿

## 0. TL;DR

`governance/` 新增第 4 件 `degrade.py`（运行时降级引擎），与既有联锁/账本复用；配置 `configs/scheme_degrade.yaml` **默认关**；scheduler `_tick` 末尾 + `paper_trading_main` 初始化两处**低侵入接线**（可选参数默认 None）。

## 1. 架构

```
外部信号写入者（云侠/人工）
   │ 写 data/governance/scheme_signals.json
   ▼
RuntimeDegrader.observe_and_evaluate(signals)      ← scheduler._maybe_degrade（节流 300s，异常吞噬）
   │ 规则: metric < threshold ∧ n ≥ min_n（否则不裁决）
   ├─ 触发 → DegradeEvent + ledger.history[sid] 留痕 + log.warning
   └─ effective_mode("live", sid, registry)          ← 调用方查询生效模式
        ├─ sid 已降级 → (SHADOW, "runtime_degraded")  【仅降不升】
        └─ 未降级   → 委托 interlock.resolve_scheme_mode（既有联锁不变）
```

## 2. 任务分解（风险驱动）

| # | 任务 | 风险 | 说明 |
|---|------|------|------|
| T01 | `governance/degrade.py`：DegradeRule / DegradeEvent / RuntimeDegrader / load_from_config / read_signals_file | 低 | 纯新增；仅标准库+yaml；fail-safe 全包裹 |
| T02 | `configs/scheme_degrade.yaml`：enabled=false + 规则示例 + 信号文件口径 | 低 | 默认关 |
| T03 | 接线：`scheduler.__init__(degrader=None)` + `_maybe_degrade(now)`（_tick 末尾）；`paper_trading_main` 启动期 load_from_config（enabled 才建） | 中 | 可选参数默认 None → 现状等价；_tick 增 1 行 |
| T04 | `tests/test_scheme_degrade.py`（≥10 用例）+ 全量回归 | 低 | 触发/不触发/样本不足/默认关/仅降不升/留痕/fail-safe/节流/文件过期 |

## 3. 关键设计裁决

1. **生效模式查询式**（非推送式）：调用方在决议方案模式时调 `degrader.effective_mode(...)`，未接入的调用方走既有 `resolve_scheme_mode` → 兼容性零破坏。
2. **降级不热改配置**（Q1）：进程内 `_degraded` dict + ledger history；重启清除、ledger 有痕。
3. **样本护栏**（Q5）：`n ≥ min_n` 才触发，杜绝小样本误降级（无证据不翻转）。
4. **信号新鲜度**（Q3）：`read_signals_file` 校验文件内 `generated_at` 或 mtime 距今 ≤ max_age_sec，过期=无信号。
5. **节流**（Q4）：`_maybe_degrade` 以 `min_interval_sec` 节流；异常 `log.exception` 吞噬，tick 不受影响。

## 4. 不做（Out of Scope）

- 云侠→HexBroker 自动信号管线（后续与云侠侧对齐）。
- 自动升回 / 自动固化降级到 yaml（均须主理人拍板）。
- 方案级绩效计算器（HexBroker paper 层无六方案绩效数据，信号由外部供给）。
