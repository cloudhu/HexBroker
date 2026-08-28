# B+C 防再发批次 · 工程师实现纪要

> 输入：`26-signal-refresh-prd.md` + `27-signal-refresh-arch.md`（T01~T05）
> 日期：2026-08-28 · 红线：默认零行为变更 / fail-safe / 零顶层重依赖

## 1. 文件清单

| # | 文件 | 变更 | 内容 |
|---|------|------|------|
| T01 | `hexbroker/diagnostics/signal_refresh.py` | 新增（~140 行） | `FreshnessProbe` / `probe()` / `stale_of()` / `format_banner()` / `maybe_auto_refresh()` |
| T02 | `configs/paper.yaml` | 追加段 | `signal_refresh: {threshold: 0, auto_enabled: false, timeout_sec: 900, cache_dir: data/signal_caches}` |
| T03 | `scripts/paper_trading_main.py` | 新增函数 + 1 行调用 | `_check_signal_freshness(paper_cfg)`；自检后调用；异常全隔离 |
| T04 | `hexbroker/paper/scheduler.py` | 低侵入接线 | `__init__` 增 `_block_reasons`/`_last_zero_open_alert`；决策链路 4 行原因累计；`_emit_daily_stats` 无成交分支调 `_warn_zero_open`；新增 `_warn_zero_open`（去重+提示） |
| T05 | `tests/test_signal_refresh_gate.py` | 新增（11 用例） | 探测陈旧/新鲜、目录缺失、阈值容忍、横幅含修复命令、新鲜无横幅、自动刷新禁用/跳过/成功/失败/超时、真实配置默认关断言、0 开仓汇总输出+去重 |
| T05 | `tests/test_audit_ruff_gate.py` | GATED_FILES 增列 | 新模块纳入静态门禁（F821/F811/E9） |

## 2. 关键实现

1. **probe fail-safe**：`pandas` 不可用/目录缺失/列缺失/解析异常 → 返回空列表（"无法判定"不产生误报），绝不抛。
2. **横幅内容**：`⛔ 信号缓存陈旧 —— 主源信号已过期，今日将【不会开仓】` + 每条 `文件名/最新/fd>阈值` + 修复命令 `python scripts/p22_tail_ext.py --skip-eval` + 验证提示。
3. **自动刷新**：subprocess 调 `scripts/p22_tail_ext.py --skip-eval`，`timeout_sec` 保护（默认 900）；超时/非零退出/异常 → 返回 `(False, 原因+手动命令)`；成功后复检缓存，仍陈旧则横幅保留。
4. **原因累计**：仅当 `无持仓(≈0) 且 目标仓位(≈0)` 时累计 `decision.reason or "no_intent"`——不干扰有持仓的风控路径。
5. **0 开仓汇总**：`[统计] 今日开仓 0 笔，未开仓原因分布：no_intent=120、cost_gate_reject=3 …`，若含 `no_intent`/`cost_gate_reject` 追加提示"（主因通常为主源信号过期→技术兜底禁开，请检查信号新鲜度 fd）"；指纹去重。

## 3. 偏差记录

- 无。1 处实现补充：`probe` 在 `signal_freshness_days` 返回 None（依赖不可用）时不判陈旧，与 health_check 同款"跳过检查"语义。

## 4. 验证

- 新增 11 用例全绿；全量 **653 绿**（642+11，0 失败）。
- ruff（含 F821/F811/E9）：**All checks passed**（新文件已纳入 GATED_FILES）。
- 默认行为：`auto_enabled=false` 断言锁定；无 stale 时仅一行"检查通过"。

**IS_PASS: YES**
