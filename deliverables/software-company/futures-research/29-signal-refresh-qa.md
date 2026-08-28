# B+C 防再发批次 · QA 独立复核（fresh-eyes）

> 对象：`28-signal-refresh-impl.md`（T01~T05）· 日期：2026-08-28 · 基线：642 全绿

## 1. 全量回归

- `pytest -q`：**653 点 = 642 基线 + 11 新增，EXIT=0，0 FAILED / 0 ERROR**。无回归。
- 新用例逐类抽验（非假绿）：
  - 探测：3 天前缓存判 stale / 当天判 fresh；目录缺失→空；阈值 1 容忍隔夜、阈值 0 不 tolerate（口径与 P0-3 一致）。
  - 横幅：stale 含"不会开仓"+文件名+修复命令；fresh/空→空串（不打扰）。
  - 自动刷新：禁用→未启用；新鲜→无需刷新；成功→(True,成功)；非零退出→失败；TimeoutExpired→超时——**全部不抛异常**。
  - 真实配置断言：`auto_enabled is False`、`threshold == 0`（防误开护栏）。
  - C2：构造 `_block_reasons={"2026-08-28":{"no_intent":120,...}}` 调 `_warn_zero_open` → 输出含"今日开仓 0 笔"与"no_intent=120"；同指纹再调**不再输出**（去重生效）。

## 2. 红线与静态门禁

- `signal_refresh.py` 仅为标准库 + 项目内 `health_check`；`pandas` 在函数内懒加载；**零顶层重依赖**。
- ruff（F821/F811/E9 + 默认规则）扫 5 个相关文件：**All checks passed**（新模块已加入 `test_audit_ruff_gate.GATED_FILES`，沿用 08-28 收工审核门禁）。

## 3. 默认零行为变更（diff 审计）

| 文件 | diff | 审计 |
|---|---|---|
| `diagnostics/signal_refresh.py` | 新增 | 纯新增模块，未被其他路径默认调用 |
| `paper_trading_main.py` | +1 调用 + 新函数 | 自检后调用；异常全隔离（except 打印后继续），**不阻断启动** |
| `scheduler.py` | +4 累计 + 1 分支 + 新方法 | 累计为**只读**（不改 decision）；分支仅在"当日无成交"路径；新方法仅日志输出 |
| `configs/paper.yaml` | 追加 `signal_refresh` 段 | `auto_enabled: false`，无既有键改动 |
| `risk/backtest/feature/governance` | **0 diff** ✅ | |

## 4. 验收口径核对（PRD §2）

1. 默认零行为变更 ✅（auto_enabled=false 断言 + 无 stale 不输出横幅）
2. fail-safe ✅（探测/刷新/汇总三层异常隔离；超时与非零退出均降级）
3. 横幅含四要素 ✅
4. 0 开仓汇总仅在无成交时输出且去重 ✅
5. 真实配置锁定 ✅

## 5. 裁决

**PASS**——机制正确、可观测性到位、默认零行为变更、红线合规、测试非假绿。

## 6. 遗留（非阻断）

- 自动刷新仅在 `auto_enabled=true` 时生效；若主理人决定"无人值守自动刷新"，需评估外部数据源可用性与失败重试策略（后续批次）。
- 可进一步把"0 开仓"升级为启动后 N 轮的主动巡检（当前随快照节奏，约 300s）。
