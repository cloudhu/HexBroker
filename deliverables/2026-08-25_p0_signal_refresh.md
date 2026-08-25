# P0-3 信号缓存新鲜度防护 + 刷新运维手册（2026-08-25）

> 状态：已实现（阈值=0 隔夜过期 + 启动自检 WARN + 运行时告警去重）
> 关联：`deliverables/trade_audit_night_20260824.md`（夜盘审计）、`deliverables/2026-08-24_p0_cost_gate_cooldown.md`（P0-1/P0-2）
> 范围：最小变更、向后兼容；未触碰 `broker.py` / `backtest/cost.py` / `trade_stats.py` 等记账统计代码

---

## 1. 问题与根因

2026-08-24 夜盘审计发现：系统在 **08-24 夜盘用 08-21（周五）的信号缓存开仓**。

根因不是缓存路径配错，而是**新鲜度阈值过宽**：

- `freshness_threshold_days = 5`（`hexbroker/paper/signals.py` 默认 + `configs/paper.yaml`）；
- 新鲜度 `fd` 用**工作日差**计算（`np.busday_count`，`signals.py::_business_days`）；
- 周五信号在周一使用 → `fd = 1 ≤ 5` → 仍判「新鲜」→ `is_effective=True` → 陈旧信号正常驱动开仓。

即：**阈值 5 等价于「一周内的旧信号都能开仓」**，与模型日频重训/日频缓存刷新的设计前提不符。

## 2. 决策：阈值收紧为 0（隔夜过期）

`_build_frame` 的判定为 `effective = is_effective and fd <= threshold`：

| threshold | 语义 | 结论 |
| --- | --- | --- |
| 5（旧） | fd≤5 皆新鲜 | 周五信号周一/周二…周五都可开仓 ❌ |
| **0（新）** | 仅 `fd == 0`（同一交易日）新鲜 | 隔夜（`fd ≥ 1`）即过期 → `is_effective=False` ✅ |

过期后的行为（§8.2 语义，未改动）：

- 有持仓 → 仅风控管理（止损/止盈/S1-S5 照常，不新开仓）；
- 无持仓 → 禁止开新仓；
- 技术兜底（双均线 + ATR 通道）接手：若日线足够，`technical_fallback` 生成 `source=technical` 信号，
  即**仍可开仓但不基于模型**；日线不足则完全禁开。

## 3. 本次改动（3 处防线）

### 3.1 阈值收紧（配置 + 代码默认，双处一致）

| 文件 | 位置 | 变更 |
| --- | --- | --- |
| `hexbroker/paper/signals.py` | `SignalEngine.__init__` 参数（原 63 行） | `freshness_threshold_days: int = 5` → `0`，docstring 注明「0=隔夜过期」 |
| `configs/paper.yaml` | `paper.freshness_threshold_days`（原 22 行） | `5` → `0` + 注释说明 |
| `scripts/paper_trading_main.py` | `build_components_safe` 的 `signals` 构造（约 133 行） | `paper_cfg.get(..., 5)` → `paper_cfg.get(..., 0)`（仍以配置为准，仅缺省值同步） |
| `hexbroker/diagnostics/health_check.py` | `check_lifecycle`（约 263 行） | 展示缺省值 `5` → `0`，阈值 0 时追加「隔夜过期」说明 |

SignalEngine 实例化处确认：**唯一生产入口**是 `scripts/paper_trading_main.py::build_components_safe`，
`cache_paths` 直接取 `paper_cfg.signal_caches`（主源 `signals_cache18_grouped_v8_tail_ext.parquet`
→ 兜底 `signals_cache18_grouped_v8.parquet`），阈值取配置值、无硬编码；`TradingScheduler` 不自建
SignalEngine，仅注入（`cfg/session/quotes/signals/...`）。`artifacts/_qa_verify.py` 中的
`freshness_threshold_days=5` 属历史一次性 QA 脚本，非运行路径，保持不动。

多源级联语义**不变**：主源过期时若兜底源有更新信号仍会回退（`engine_a_fb1`），阈值 0 下只是
「更新」的判定更严格（兜底也必须是当日信号才 `is_effective=True`）。

### 3.2 启动自检 WARN（`hexbroker/diagnostics/health_check.py`）

`check_data_sources` 的信号缓存循环在读到 `latest ts` 后，新增新鲜度计算与判定：

- 新增 `signal_freshness_days(latest_ts, asof=None)`：**复用** `hexbroker.paper.signals._business_days`
  / `_to_date`（同口径 `np.busday_count`，避免两处逻辑漂移），依赖不可用或时间戳无法解析时返回
  `None` → 跳过新鲜度检查（不影响原有存在性检查）；
- 阈值取 `paper_cfg.get("freshness_threshold_days", 0)`，缺失回退 0；
- `fd > threshold` → `CheckItem("WARN", "信号缓存: <name>", "信号陈旧 fd=..>阈值..，最新..，建议开盘前刷新 (p22_tail_ext.py --skip-eval)")`；
- `fd <= threshold` → 原 `OK` 项，detail 追加 `新鲜度 fd=..<=阈值..`；
- **不阻断启动**（仅 WARN；文件缺失仍为 FAIL，语义不变）；
- 新增可选参数 `asof`（默认 `date.today()`），仅供测试注入基准日，向后兼容。

### 3.3 运行时告警 + 去重（`hexbroker/paper/scheduler.py`）

- `SignalEngine` 新增 `@property freshness_threshold` → 暴露阈值给调用方（不再各处重复读配置）；
- `_process_symbol` 取到主源 `sig` 后立即调用 `_warn_stale_signal_once(symbol, sig, day)`：
  `sig.freshness_days > 阈值` → `log.warning("[告警] 信号陈旧 fd={} 品种={}，主源过期，已降级/禁开（技术兜底接手）")`；
- **去重**：`self._stale_warn: dict[(symbol, day), bool]`，`day` 取 `session.day_label(now)`（夜盘归属
  下一交易日，与全局口径一致）→ 每品种每交易日仅 1 条，避免 60s tick 刷屏（对齐 R3-1 `_warn_fallback_once` 风格）；
- **健壮性**：信号引擎未暴露 `freshness_threshold`（旧实现 / 测试 mock）→ `getattr` 返回 `None` → 静默跳过，
  不抛异常、不影响既有管道（保证 `test_signal_cooldown.py` 等既有用例零改动通过）。

## 4. 运维手册：开盘前必须刷新信号缓存

### 4.1 标准动作（每交易日开盘前）

```bash
# 1) 重建信号缓存（仅重建缓存，不重训模型；耗时远低于全量训练）
python scripts/p22_tail_ext.py --skip-eval

# 2) 启动自检确认新鲜度（信号缓存项应为 OK 且 fd=0）
python scripts/paper_trading_main.py --health-check

# 3) 启动模拟盘
python scripts/paper_trading_main.py        # 或 start_paper_trading.bat
```

### 4.2 前置依赖

- **K 线 / 基差数据必须已更新到最新交易日**（`data/` 下的价格与 basis 面板）：
  信号缓存由这些面板派生，数据不新 → 即使执行刷新，缓存 `ts` 也停在旧日期；
- 数据新鲜度可用 `scripts/p23_daily_run.py` 的 `freshness_check` 交叉核对（K 线/基差最新日 + 过期品种清单）。

### 4.3 当前环境限制（重要）

> **本环境行情数据仅到 2026-08-21**：即使执行 `p22_tail_ext.py --skip-eval`，缓存最新 ts 也到不了
> 2026-08-24/25。必须**先由外部数据源把 K 线/基差更新到最新交易日**，再刷新缓存，否则新鲜度自检
> 仍会 WARN。

因此阈值改 0 后的**预期现状**是：

- 启动自检 → 信号缓存项 WARN（`fd>0`，提示刷新命令）；
- 运行期 → 每品种每日 1 条 `[告警] 信号陈旧 fd=.. 品种=..`；
- 主源信号 `is_effective=False` → **不以模型信号开仓**；有日线时由技术兜底（双均线+ATR）接手，
  无日线则完全禁开。这是**安全但不基于模型**的降级状态，属预期行为，不是 bug。

### 4.4 值班检查清单

1. 自检报告 `logs/health_check.log`：信号缓存是否 OK（`fd=0`）；
2. 运行日志是否出现 `[告警] 信号陈旧`：出现即说明当日未刷新成功；
3. 复盘报告信号表 `freshness_days` / `is_effective` 列：确认信号源是 `engine_a`（模型）还是 `technical`（兜底）；
4. 若长期只有 `technical` 成交 → 数据/缓存链路问题，优先修数据，勿放宽阈值。

## 5. 测试与验证

### 5.1 新增用例（26 条，全绿）

| 文件 | 条数 | 覆盖 |
| --- | --- | --- |
| `tests/test_signal_freshness.py` | 9 | 阈值 0 下 fd=1（08-21→08-24）→ `is_effective=False`；fd=0 → `True`；缓存自身 `is_effective=False` 时 fd=0 也不放行；跨交易日主源过期且兜底无更新 → 主源 + `False`（fd=2）；主源隔夜过期 + 兜底当日新鲜 → 回退 `engine_a_fb1`（级联语义不变）；`freshness_threshold` property 默认 0 / 显式值；`configs/paper.yaml` 阈值==0 回归护栏；`_business_days` 口径（周五→周一=1、同日=0、跨周=5） |
| `tests/test_health_check_signal_freshness.py` | 11 | 陈旧缓存（08-21 @ 08-24）→ WARN 且 detail 含 `fd=1>阈值0` + 刷新命令；极陈旧（06-29）→ fd 为工作日差；新鲜（当日）→ OK 且含 `fd=0<=阈值0`；阈值取配置（5 时 fd=1 判 OK）；配置缺失回退 0；文件缺失仍 FAIL；`signal_freshness_days` 与 `SignalEngine` 口径逐点一致（参数化 4 组）；无 ts → None；生命周期展示「0 交易日 … 隔夜过期」 |
| `tests/test_scheduler_stale_warn.py` | 6 | 同品种同交易日 3 次 tick → 仅 1 条 `[告警] 信号陈旧`（含 `fd=1`/`品种=rb0`/`技术兜底接手`）且无持仓禁开（0 成交）；跨交易日（08-24→08-25）→ 再告警 1 条（共 2）；新鲜信号（fd=0）→ 不告警且正常开仓；引擎未暴露阈值 → 静默；`sig=None` → 不告警不抛错 |

沙箱缺 `pyarrow`，故新增用例**不写真实 parquet**：`SignalEngine` 用例注入 `_load_cache` 返回内存
DataFrame（列/类型与生产缓存一致），健康检查用例注入 `pandas.read_parquet` + 占位文件，
保证与环境无关地覆盖真实代码路径。

### 5.2 全量 pytest

```
361 collected | 315 passed | 20 failed | 15 errors(collection) | 11 skipped
```

- 20 failed + 15 errors **全部为环境性**：缺 `pyarrow`（parquet 读写：`test_paper_signals.py` 4 条、
  `test_utils_core.py::test_io_parquet_roundtrip` 等）、`pydantic`（`hexbroker/config.py` 导入链，
  15 个收集错误的主因）、`scipy`、`torch`/RL 依赖（`test_rl_smoke.py` 等）；
- 与本次改动**无关**：改动前同一环境即为同一组失败（失败清单与 `.workbuddy` 历史审计一致，
  且失败用例均在 `import` 阶段即中断，未进入 freshness 逻辑）；
- 与信号/调度/自检相关的用例全绿：`test_signal_freshness.py`（9）、
  `test_health_check_signal_freshness.py`（11）、`test_scheduler_stale_warn.py`（6）、
  `test_signal_cooldown.py`（6）、`test_paper_pipeline.py`、`test_paper_sessions.py`、
  `test_paper_broker.py`、`test_risk_gate_cost.py`、`test_trade_stats.py` 等。

### 5.3 依赖 threshold=5 的旧测试

`tests/test_paper_signals.py` 的 `_engine(..., threshold: int = 5)` **显式传入**阈值，验证的是
「多源级联」而非阈值语义 → 行为不受默认值收紧影响，**无需改断言**；仅在 helper 上补 docstring
说明「显式宽松阈值与生产默认解耦，阈值语义用例见 `test_signal_freshness.py`」。
其余测试中出现的 `freshness_days=1`（`test_signal_cooldown.py`、`test_risk_gate_cost.py`）是
**手工构造的 SignalFrame**（`is_effective` 直接给定，不经 `_build_frame` 判定），语义不受影响；
调度器新告警对未暴露 `freshness_threshold` 的 mock 静默，故这些用例零改动通过。

## 6. 设计偏离与备注

- **无功能性偏离**。仅两处「非必需但必要的一致性补充」：
  ① `scripts/paper_trading_main.py` 与 `health_check.check_lifecycle` 的 `get(..., 5)` 缺省值同步为 0
  （否则配置缺失时仍退回 5，与决策矛盾）；
  ② `check_data_sources` 增加可选 `asof` 参数（默认今日）以便确定性测试，向后兼容。
- 新鲜度计算未引入交易日历（仍为 `np.busday_count` 工作日近似）：阈值 0 下节假日影响消失
  （只要求「同一交易日」），无需额外精度。
- `_stale_warn` 字典按 (symbol, day) 累积，每交易日每品种 1 条，长期运行内存开销可忽略。
- 未触碰 `hexbroker/paper/broker.py`、`hexbroker/backtest/cost.py`、`hexbroker/paper/trade_stats.py`
  等记账统计代码。
