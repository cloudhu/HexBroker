# 治理信号数据契约 · scheme_signals.json（P2-D 管线对齐）

> 消费端：`hexbroker/governance/degrade.py::read_signals_file`（RuntimeDegrader 运行时降级）
> 写入端：`scripts/gov_scheme_signals.py`（本仓库 CLI）或任何按本契约写入的外部进程（云侠侧）
> 状态：**契约先行，两端解耦**。云侠侧当前日志（P0O1门禁结果.json）无六方案归因字段，
> 自动供给留待云侠侧补归因；在此之前人工/脚本按契约写入即可启用 P2-D。

## 1. 文件格式（精确 schema）

```json
{
  "generated_at": "2026-08-28T16:50:00",
  "signals": {
    "1C": {"rolling_wr": 0.40, "n": 25}
  }
}
```

- `generated_at`：ISO8601（naive 本地时间）。**新鲜度语义**：距 now > `signal_max_age_sec`
  （configs/scheme_degrade.yaml，默认 900s）→ 视为无信号，Degrader 不评估。
- `signals`：scheme_id（须与 `configs/scheme_governance.yaml` 一致：1C/2A/3A/3B/1B/2B）
  → 指标字典。

## 2. 指标白名单（写入器校验）

| metric | 语义 | 典型降级规则 |
|---|---|---|
| `rolling_wr` | 滚动胜率（0~1） | `< threshold ∧ n≥min_n` → 降级 |
| `rolling_maxdd` | 滚动最大回撤（正数小数） | `> threshold` → 降级（规则侧用 `< -x` 表述时取负） |
| `oos_wr` | 最新 OOS 胜率 | 同 rolling_wr |
| `data_quality` | 数据质量分（0~1，越低越差） | `< threshold` → 降级 |

白名单可扩展（改 `scripts/gov_scheme_signals.py::VALID_METRICS` + 本文档同步）。

## 3. 职责边界

| 端 | 职责 |
|---|---|
| 云侠侧（供给） | 按本契约产出信号数据（CSV/JSON）。**前置缺口**：云侠交易日志（`交易日志/*P0O1门禁结果.json`）记录 strategy/regime/veto，**无六方案归因字段**；补归因后可自动导出，当前可人工按回测/影子结果填写 |
| HexBroker（消费） | `gov_scheme_signals.py` 写入（schema 校验+原子写）；RuntimeDegrader 每 `min_interval_sec`（默认 300s）读文件评估；触发 → 方案 LIVE→SHADOW（仅降不升）+ ledger.history 留痕 |

## 4. 语义与护栏（与 P2-D 一致）

1. **仅降不升**：信号恢复不自动升回；升回走既有联锁（主理人+校准记录）。
2. **样本护栏**：`n ≥ min_n` 才裁决；`n` 取该方案信号字典中最新一次写入值。
3. **降级不热改配置**：不修改 scheme_governance.yaml / ledger status；事件留痕 history。
4. **fail-safe**：文件损坏/过期/字段缺失 → 视为无信号，不评估、不阻断 tick。

## 5. 云侠侧对齐路线（后续批次）

1. 云侠 gate_channels / 日志写入端补 `scheme` 归因字段（哪个决策走了 1C/2A/3B 通道）。
2. 云侠导出脚本：按 fwd5 结算滚动窗口计算各方案 rolling_wr/rolling_maxdd → CSV。
3. HexBroker：`gov_scheme_signals.py --csv <导出>` 接入（或定时任务直接写 signals.json）。
