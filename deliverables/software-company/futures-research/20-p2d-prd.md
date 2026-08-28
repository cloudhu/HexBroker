# P2-D 运行时动态降级 · PM 增量 PRD

> 来源：P2 Q5 留项（15-p2-arch §8："运行时动态降级留后续批次，避免热路径侵入"）
> 日期：2026-08-28 · 铁律：默认零行为变更 / fail-safe / 无证据不翻转

## 1. 需求池

- **P2-D1 降级引擎**（`governance/degrade.py`）：运行时按绩效/质量信号把 PASS 方案的**生效模式**从 LIVE 降为 SHADOW（`DegradeRule` + `RuntimeDegrader` + `effective_mode`）。
- **P2-D2 留痕 + 告警**：降级事件追加 `CalibrationLedger.history[sid]`（append-only）+ `log.warning`；**仅降不升**——信号恢复不自动升回，升回走既有联锁（主理人 + 校准记录）。
- **P2-D3 信号源接口**：文件观测 `data/governance/scheme_signals.json`（外部写入：如 `{"1C": {"rolling_wr": 0.40, "n": 25}}`）；文件过期（max_age_sec）视为无信号，不评估。本批次不建云侠→HexBroker 自动管线。
- **P2-D4 低侵入接线**：`configs/scheme_degrade.yaml` **`enabled: false` 默认关**；开启后 scheduler `_tick` 末尾节流评估（min_interval_sec，异常吞噬）。

## 2. 验收口径

1. 默认（enabled: false）行为与现状逐字节等价：不读信号文件、不评估、不告警。
2. 降级仅影响**运行时生效模式**（内存覆盖 + 留痕），不热改 yaml/ledger status；固化须主理人拍板。
3. 仅降不升：`observe` 信号恢复后 `is_degraded(sid)` 仍 True。
4. fail-safe：信号缺失/文件损坏/评估异常 → WARNING/静默跳过，绝不阻断 tick、绝不抛出。
5. 触发须同时满足：metric < threshold ∧ n ≥ min_n（样本不足不裁决）。

## 3. 开放问题（Q1-Q5）

- **Q1** 是否热改 yaml/ledger status？→ **否**（内存覆盖 + history 留痕；重启后降级清除但 ledger 有痕，启动自检提示未固化降级记录待主理人拍板）。
- **Q2** 能否自动升回？→ **否**（仅降不升；升回=联锁路径，须校准记录）。
- **Q3** 信号源？→ 文件观测接口（P2-D3）；数据新鲜度由 max_age_sec 把关。
- **Q4** 默认开关与节流？→ `enabled: false`；`min_interval_sec: 300`。
- **Q5** 规则语义？→ 比较子固定 `<`（跌破红线语义），样本 `n≥min_n` 才裁决（无证据不翻转）。

## 4. 批次

批次一（纯增量）：P2-D1 → P2-D2 → P2-D3；批次二：P2-D4 接线 + 测试 + 全量回归。
