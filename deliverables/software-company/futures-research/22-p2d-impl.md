# P2-D 运行时动态降级 · 工程师实现纪要

> 输入：`20-p2d-prd.md` + `21-p2d-arch.md`（T01~T04，Q1~Q5 全采纳）
> 日期：2026-08-28 · 红线：默认零行为变更 / 零顶层重依赖 / fail-safe

## 1. 文件清单

| # | 文件 | 变更 | 内容 |
|---|------|------|------|
| T01 | `hexbroker/governance/degrade.py` | 新增（~230 行） | DegradeRule / DegradeEvent / read_signals_file / RuntimeDegrader（load_from_config 默认关 / should_evaluate 节流 / observe_and_evaluate 评估 / effective_mode 查询） |
| T01 | `hexbroker/governance/ledger.py` | 新增方法 | `append_history(scheme_id, event)`——history 追加留痕（append-only，纯新增零破坏） |
| T01 | `hexbroker/governance/__init__.py` | 增补导出 | DegradeRule / DegradeEvent / RuntimeDegrader / read_signals_file |
| T02 | `configs/scheme_degrade.yaml` | 新增 | **enabled: false 默认关** + signals_file/signal_max_age_sec/min_interval_sec + rules: [] 示例注释 |
| T03 | `hexbroker/paper/scheduler.py` | 低侵入接线 | `__init__` 增可选参数 `degrader=None, degrade_signals=None`；`_tick` 末尾 +1 行 `self._maybe_degrade(now)`；新增 `_maybe_degrade`（未启用直接 return；节流/新鲜度/异常全隔离） |
| T03 | `scripts/paper_trading_main.py` | 接线 | 自检后 `RuntimeDegrader.load_from_config`（enabled=false → None）+ degrade_signals 元组 + 传参 TradingScheduler；try/except 全包裹，失败按未启用处理 |
| T04 | `tests/test_scheme_degrade.py` | 新增（16 用例） | 触发/样本护栏/红线之上不触发/缺信号不触发/降级后 live 强制 shadow/仅降不升/健康方案走既有联锁/默认关双测/启用构建/真实配置默认关断言/ledger 留痕/异常隔离/节流/文件新鲜+过期/文件缺失损坏 |

## 2. 关键实现

1. **仅降不升**：`_degraded` dict 一旦写入，后续评估对同 sid 直接 `continue`；信号恢复不移除；`effective_mode` 恒返 `(SHADOW, "runtime_degraded")`。
2. **联锁复用**：未降级方案 `effective_mode` 委托既有 `resolve_scheme_mode`（`21-p2d-arch` 裁决 1），兼容性零破坏。
3. **样本护栏**：`n < min_n` 直接跳过（无证据不裁决）；比较子固定 `<`。
4. **留痕**：触发 → `ledger.append_history(sid, event.to_dict())`（type=runtime_degrade，含 rule_id/metric/value/n/reason/ts）；留痕失败仅 `log.exception`，不影响降级生效。
5. **fail-safe 层级**：配置解析失败→None；单规则异常→跳过该规则；读文件失败→None=无信号；scheduler `_maybe_degrade` 外层 try/except——tick 永不受影响。
6. **默认零行为变更**：`enabled: false` → `load_from_config` 返回 None → scheduler `degrader=None` → `_maybe_degrade` 首行 return。`test_real_default_config_is_disabled` 断言仓库内真实配置必须默认关。

## 3. 偏差记录

- 无重大偏差。1 处微调：`read_signals_file` 新鲜度在文件内 `generated_at` 缺失时回退 mtime（架构文档口径的补充实现，更稳健）。

## 4. 验证

- 新增 16 用例 + 治理 15 用例 = 31 点全绿。
- 全量回归：612 → 628 预期（+16），0 失败（见 QA 报告）。
- 默认行为：真实配置 `enabled: false` 断言 + degrader=None 路径测试双重锁定。

**IS_PASS: YES**
