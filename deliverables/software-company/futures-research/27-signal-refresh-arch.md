# B+C 防再发批次 · Architect 增量设计 + 任务分解

> 输入：`26-signal-refresh-prd.md`（B1/B2/C1/C2 + Q1~Q5 全采纳）
> 约束：默认零行为变更 / fail-safe / 红线零顶层重依赖 / 653 全绿

## 0. TL;DR

新增 `hexbroker/diagnostics/signal_refresh.py`（探测 + 横幅 + 可选自动刷新），接线
`paper_trading_main` 启动自检；`scheduler` 增拦截原因累计与「0 开仓」显性汇总。
配置 `configs/paper.yaml::signal_refresh`（**auto_enabled=false**）。

## 1. 架构

```
启动期  paper_trading_main._check_signal_freshness(cfg)
   │  probe(cache_dir, threshold)      ← 复用 health_check.signal_freshness_days 同口径
   ├─ 新鲜      → 打印"检查通过"（一行）
   └─ 陈旧      → 打印 ⛔ 醒目横幅（C1）→ maybe_auto_refresh(enabled=cfg)
                    ├─ enabled=false → "未启用"（默认，仅提示命令）
                    └─ enabled=true  → subprocess p22_tail_ext.py --skip-eval
                                       （超时/失败→告警，绝不阻断）

盘中    scheduler._process_symbol  累计 decision.reason → _block_reasons[day]
        scheduler._emit_daily_stats（无成交分支）→ _warn_zero_open(day)
                                    → "[统计] 今日开仓 0 笔，未开仓原因分布：…"（去重一次）
```

## 2. 任务分解

| # | 任务 | 风险 | 说明 |
|---|------|------|------|
| T01 | `diagnostics/signal_refresh.py`（probe/format_banner/maybe_auto_refresh） | 低 | 纯新增；依赖缺失/解析失败一律返回空（fail-safe） |
| T02 | `configs/paper.yaml::signal_refresh` | 低 | 默认关 |
| T03 | `paper_trading_main._check_signal_freshness` + 自检后调用 | 低 | 异常全隔离，不阻断启动 |
| T04 | `scheduler`：`_block_reasons` 累计 + `_warn_zero_open` 汇总 | 中 | 决策链路内 4 行只读累计；汇总在既有统计钩子内分支 |
| T05 | 测试 + ruff 门禁（新文件入 GATED_FILES）+ 全量回归 | 低 | 11 用例 |

## 3. 关键设计裁决

1. **默认不自动刷新**（Q1）：刷新=外部数据拉取+写缓存，属生产操作，需人工确认；配置开启即启用。
2. **不阻断启动**（Q2）：横幅告警后照常启动——与既有治理自检（WARNING+强制 shadow，不崩溃）同款 fail-safe。
3. **同口径复用**：`signal_freshness_days`（health_check 既有实现）避免两处逻辑漂移。
4. **累计点选在决策已定、撮合之前**：此时 `decision.reason` 已由冷却/熔断/min_hold/风控各层写入，无持仓且目标为 0 即"未开仓"。
5. **汇总去重**：指纹 `(day, ranked_reasons)`，状态不变不重复输出（防每 5 分钟刷屏）。

## 4. 不做（Out of Scope）

- 改门禁阈值 0→1（Q5，状态翻转须 walk-forward QA）。
- 自动刷新失败的自动重试/告警推送（保持简单，人工介入）。
- 修改信号引擎或缓存生成逻辑（本批次仅观测+编排）。
