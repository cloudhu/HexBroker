# P2-D 运行时动态降级 · QA 独立复核（fresh-eyes）

> 对象：`22-p2d-impl.md`（T01~T04）· 日期：2026-08-28 · 基线：612 全绿

## 1. 全量回归

- `pytest -q`：**628 点 = 612 基线 + 16 新增，EXIT=0，0 FAILED / 0 ERROR**。无回归。
- 新增 16 用例逐类抽验（非假绿）：
  - 触发语义：`rolling_wr=0.40<n? 否→0.40<0.45 ∧ n=25≥20` 触发 ✅；`n=19` 护栏不裁决 ✅；`0.55` 红线之上不触发 ✅；缺 sid/metric 不触发 ✅。
  - 仅降不升：恢复信号（0.90/n=99）后 `is_degraded` 仍 True、`effective_mode` 仍 SHADOW ✅。
  - 联锁复用：未降级 PASS（1C）→live、NOT_PASS（3B）→shadow（reason=not_pass）✅——既有联锁语义未被破坏。
  - 默认零变更：仓库真实 `configs/scheme_degrade.yaml` `enabled is False ∧ rules==[]` 断言 ✅ + `load_from_config` 禁用/缺文件→None ✅。
  - 留痕：触发事件落 `ledger.json history["1C"]`（type=runtime_degrade，磁盘回读验证）✅。
  - fail-safe：非数值 metric 异常被隔离（`fired==[]`）✅；节流 `should_evaluate` 二连问 False ✅；信号文件过期/损坏/缺失→None ✅。

## 2. 红线审计

- `degrade.py` 顶层 import：标准库 + `yaml`（与 `scheme.py`/`rule.py` 同款）+ 项目内模块；**零 vnpy_ctp/mlflow/qlib**（grep 实证）✅。
- `read_signals_file` 在 scheduler 侧为**函数内懒加载** import（红线纪律）✅。

## 3. 默认零行为变更（diff 审计）

| 模块 | diff | 审计 |
|---|---|---|
| `paper/scheduler.py` | +25 行 | 仅新增可选参数（默认 None）+ `_maybe_degrade`（未启用首行 return）+ `_tick` 末尾 1 行调用；**交易决策路径（quote→signal→risk→plan→execute）零改动** ✅ |
| `paper_trading_main.py` | +22 行 | 自检后初始化（try/except 全包裹，失败=None）+ 构造传参 2 行 ✅ |
| `governance/*` | +9 行 | `append_history` 纯新增方法 + 导出增补 ✅ |
| `risk/backtest/feature` | **0 diff** ✅ | |

## 4. 验收口径核对（PRD §2）

1. 默认与现状等价 ✅（enabled=false 双重锁定：配置断言 + None 路径）
2. 降级仅内存覆盖+留痕，不热改 yaml/status ✅（`_degraded` 进程内 + `append_history` 事件流）
3. 仅降不升 ✅（专用测试）
4. fail-safe 分层（配置/单规则/文件/tick 外层四层隔离）✅
5. 触发双条件（metric<threshold ∧ n≥min_n）✅（护栏测试）

## 5. 裁决

**PASS**——机制正确、默认零行为变更、红线合规、留痕完整、测试非假绿。

## 6. 遗留（非阻断）

- 信号自动供给管线（云侠→HexBroker 写 `scheme_signals.json`）后续与云侠侧对齐；当前人工/脚本写入即可用。
- 启动自检可增"存在未固化 runtime_degrade 留痕"提示（ledger 有痕已可审计，低优先）。
