# P2 双治理 + 落地（六方案 P2 项）· 工程师实现纪要

> 输入：`14-p2-prd.md`（PM）+ `15-p2-arch.md`（Architect，T01~T04，Q1~Q5 全采纳）
> 链路：PM→Architect→Engineer→QA→主理人终裁
> 铁律：无证据不翻转 / 双闸门 / 549 测试全绿 / feat+docs 双提交 / `fsck` 空

## 0. TL;DR

P2 为**纯增量治理控制平面**，零改动既有交易/信号/风控逻辑。T01~T04 全部落地，全量回归 612 绿（597+15，0 失败）。IS_PASS = **YES**。

## 1. 任务 ↔ 文件清单

| Task | PRD | 文件 | 内容 |
|------|-----|------|------|
| T01 | P2-3 | `hexbroker/governance/scheme.py` | SchemeStatus/Scheme/SchemeRegistry(load+verify_all) |
| T01 | P2-3 | `configs/scheme_governance.yaml` | 6 方案定案（status+根因+gate_metrics+calib_ref） |
| T02 | P2-2 | `hexbroker/governance/ledger.py` | CalibrationRecord/CalibrationLedger(load/save/append，原子写) |
| T02 | P2-2 | `data/governance/calibration_ledger.json` | 预填 6 方案校准记录（即时可审计） |
| T03 | P2-1 | `hexbroker/governance/interlock.py` | MisOpenBlocked/ResolvedMode/assert_safe_to_activate/resolve_scheme_mode |
| T03 | P2-1 | `hexbroker/governance/__init__.py` | 导出三件套 |
| T04 | P2-4 | `scripts/paper_trading_main.py` | `_run_governance_selfcheck()` + main 中 PID 锁后调用 |
| — | — | `tests/test_scheme_governance.py` | 15 用例（联锁4类reason+fail-safe+账本往返/追加+verify_all+6方案+自检语义） |

## 2. 关键实现要点

### 2.1 双治理
- **①防误开联锁（interlock.py）**
  - `assert_safe_to_activate`：硬断言；`status != PASS` / 无 calibration / 未知方案 → 抛 `MisOpenBlocked(reason)`。
  - `resolve_scheme_mode`：**fail-safe**（Q1）：请求 live 被拦 → 返回 `SHADOW + reason`，**不抛异常**。
  - 默认 fail-safe：未知方案 `get=None` → `unknown_scheme` 拦截 → shadow。
- **②PASS 校准记录账本（ledger.py）**
  - 字段含双闸门证据（gate1_dir_acc / gate2_oos_cost / gate2_pbo / gate2_dsr / n / maxdd / net_pnl_vs_cost）+ 数据根因 + 主理人裁决 + 时间戳 + `pending_qa`。
  - `append` 覆盖 current + 旧版移入 history（append-only 留痕）。
  - 原子写 `.tmp`+`os.replace`（与 `paper/broker.py` snapshot 同款）。

### 2.2 落地
- **③寄存器+Yaml（scheme.py + yaml）**：`SchemeRegistry.load` 解析 6 方案；`verify_all()` 启动期不变量（PASS 须有 calibration_ref）。
- **④接线（paper_trading_main.py）**：`_run_governance_selfcheck()` 在 PID 锁后调用，仅 WARNING + 强制 shadow，**不进入 `_tick` 主循环**（R15 零侵入）。

## 3. 偏差记录（vs 架构设计）

| 项 | 设计 | 实现 | 说明 |
|----|------|------|------|
| YAML 解析 | 设想零依赖极简解析 | 顶层 `import yaml` | 与既有 `market/rule.py`/`factor/registry.py` 同款（pyyaml 轻量已用），红线合规 |
| 1C/2A 精确 WR/n | 待补 | 标 `pending_qa`+gate 布尔 | **不编造数字**（无证据不翻转）；其 status=PASS 已满足联锁前置 |
| 账本预填 | 运行时生成 | 直接预填 JSON | 使治理即时可审计，结构经 dataclass 校验一致 |

无功能性偏差，Q1~Q5 全部按采纳方案落地。

## 4. 默认零变更核验（diff 核对）

- 既有交易/信号/风控代码（`hexbroker/paper/*`、`hexbroker/risk/*`、`hexbroker/backtest/*`）**git diff 为空**（仅新增 `governance/` 子包 + 配置 + `paper_trading_main.py` 追加自检块）。
- `paper_trading_main.py` 改动：**仅新增** `_run_governance_selfcheck` 函数 + 一行调用（PID 锁后），未修改任何既有 `_tick`/组件逻辑。

## 5. 验证证据

- **新测试 15/15 绿**：联锁 4 类 reason（not_pass/deprecated/unknown_scheme/no_calibration）+ fail-safe + 账本 JSON 往返 + 追加 history 留痕 + `.tmp` 无残留 + 寄存器 verify_all + 6 方案加载 + 启动自检语义（live={1C,2A}, shadow={3A,3B,1B,2B}）。
- **全量回归**：612 绿（597+15），**0 失败 / 0 错误**，无回归。
- **端到端冒烟**：registry.load → 1C/2A=live、3A/3B/1B/2B=shadow，verify_all 不抛。
- **红线审计**：`governance/` 仅用标准库 + `..utils.logging`；无顶层 `vnpy_ctp`/`mlflow`/`qlib` import。

## 6. IS_PASS 裁决

**IS_PASS = YES**
- 双治理（联锁 + 账本）落地 ✅
- 落地（寄存器+Yaml + 启动自检接线）落地 ✅
- 默认零行为变更 ✅
- 全量 612 绿、红线合规 ✅
- 六方案定案忠实登记（不编造），1B/2B 精确指标标 `pending_qa` 待 QA 重跑 ✅
