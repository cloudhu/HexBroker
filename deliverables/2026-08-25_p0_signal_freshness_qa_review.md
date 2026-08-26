# P0-3 信号缓存新鲜度防护 · QA 独立复核报告

- **复核人**：Edward（QA 工程师，fresh-eyes 独立复核）
- **日期**：2026-08-25
- **项目**：HexBroker 中国大宗商品期货模拟交易系统（`E:\Workspace\HexBroker`）
- **git HEAD**：`279ff68`（已 push，复核期间只读，未改动任何生产代码/提交）
- **复核对象**：提交 `5dbf8ce`（feat）+ `279ff68`（docs）
- **治理铁律**：无证据不翻转；独立验证，不采信工程师结论。

---

## 0. 复核方法与范围

不依赖工程师提供的任何测试结论，独立做三件事：

1. **独立驱动生产代码**：构造 `SignalEngine(threshold=0)`，注入内存 DataFrame（不写真实 parquet，规避沙箱缺 pyarrow），直接断言 `latest_signal` 返回值。
2. **逐行代码审查**：signals / health_check / scheduler / paper_trading_main 四处改动。
3. **独立跑工程师新增测试 + 真实方法驱动**：3 个新测试文件 + 直接调用 `TradingScheduler._warn_stale_signal_once` 真实方法。

> 沙箱环境：numpy 2.5.1 / pandas 3.0.5 / omegaconf / pytest 9.1.1 可用；**pyarrow 与 pydantic 未安装**（环境性缺口，见 §4）。

---

## 1. 阈值语义独立核验（核心，P0-3 是否真修好）

**结论：PASS** —— 隔夜信号在阈值 0 下确实判定过期，`is_effective=False`。

独立复算（直接驱动生产 `SignalEngine` + `_business_days`）结果：

| 核验项 | 场景 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| 隔夜过期（核心） | `threshold=0`，rb0 信号 `ts=08-21`，`asof=08-24 21:30`（周一夜盘） | `is_effective=False`、`freshness_days=1`、`direction()=0` | `is_effective=False, fd=1, dir=0` | ✅ |
| 同日新鲜 | `threshold=0`，`ts=08-21`，`asof=08-21 16:00`（同日且晚于 ts） | `is_effective=True`、`fd=0` | `True, fd=0` | ✅ |
| 工作日差口径 | `np.busday_count(08-21 周五, 08-24 周一)` | `=1`（非 0） | `1` | ✅ |
| 工作日差口径 | `_business_days(同日)` / `(_08-17,_08-24)` | `0` / `5` | `0` / `5` | ✅ |
| property 暴露 | `SignalEngine.__init__` 默认 / 显式 `3` | `0` / `3` | `0` / `3` | ✅ |
| 级联语义不变 | 主源 08-21 过期 + 兜底源 08-24 当日 | 回退兜底 `engine_a_fb1`、`effective=True` | `engine_a_fb1, True` | ✅ |
| 反例护栏 | `threshold=5` 下隔夜 | `effective=True`（旧行为，证明缺省必须=0） | `True` | ✅（确认旧根因） |

**关键代码证据**（`hexbroker/paper/signals.py`）：
- L76 `freshness_threshold_days: int = 0`（缺省 0）
- L89 `self._freshness_threshold = int(freshness_threshold_days)`
- L141 `if fd0 <= self._freshness_threshold:` 主源新鲜分支
- L160 `effective = bool(row["is_effective"]) and fd <= self._freshness_threshold` —— **阈值 0 下仅 `fd==0` 才有效**，隔夜 `fd>=1` 强制 `is_effective=False`，即便缓存原值 `is_effective=True`（与本次注入的 rb0 `is_effective=True` 一致，验证“缓存标有效但隔夜仍判过期”）。

> 独立验证脚本共 29 项断言全部 PASS（含 signals 语义 11 项、health_check 口径 3 项、day_label 3 项、scheduler 去重/静默 5 项、反例护栏 1 项、三处一致性 6 项）。脚本为临时内存注入，已清理，未留痕。

---

## 2. 三处一致性

| 位置 | 值 | 证据 | 一致 |
|---|---|---|---|
| `signals.py` L76 缺省 | `0` | `freshness_threshold_days: int = 0` | ✅ |
| `configs/paper.yaml:22` | `0` | `freshness_threshold_days: 0` | ✅ |
| `scripts/paper_trading_main.py` L134 缺省 | 配置读取，缺失回退 `0` | `int(paper_cfg.get("freshness_threshold_days", 0))` | ✅ |
| 实例化路径 | 从配置读，无硬编码 `5` | `build_components_safe` L131-136 仅 `freshness_threshold_days=...get(...,0)` | ✅ |

**无硬编码 `5` 的运行路径**：全仓 grep `freshness_threshold` 与 `=5`，运行路径（signals / health_check / scheduler / paper_trading_main / paper.yaml）全部为 `0` 或读取配置。
唯一残留的 `=5` 在 **`artifacts/_qa_verify.py:97`**（`SignalEngine(..., freshness_threshold_days=5)`）—— 属历史一次性 QA 脚本，非运行路径，交付文档已注明“保持不动”。**不影响运行时行为**，但建议后续清理或加注释以免误读。

---

## 3. 自检 / 告警代码审查

### 3.1 启动自检（health_check.py）
- **L131-148 `signal_freshness_days`**：直接 `from ..paper.signals import _business_days, _to_date` 复用 signals 同口径（`np.busday_count`），无两处逻辑漂移。✅
- **L195 阈值取配置、缺失回退 0**：`threshold = int(paper_cfg.get("freshness_threshold_days", 0) or 0)`。✅
- **L216-223 陈旧→WARN 不阻断**：`if fd is not None and fd > threshold:` 输出 `WARN` 含 `fd={fd}>阈值{threshold}` + 刷新命令（`p22_tail_ext.py --skip-eval`）；`check_data_sources` 返回 `WARN` 项，**不抛异常、不 exit**，启动继续。✅
- **L203 文件缺失→FAIL**：`not path.exists()` → `FAIL` 项（原语义不变，仍阻断该缓存加载）。✅
- **L304 生命周期展示**：阈值 0 时附「0=隔夜过期（仅当天信号有效，P0-3）」语义说明。✅

独立参数化核验（周五→周一=1、同日=0、跨周=5、06-29→08-24=工作日差）`signal_freshness_days` 与 `_business_days` 逐点相等。✅

### 3.2 运行时告警去重（scheduler.py）
- **L119-120**：`self._stale_warn: dict[tuple[str, Optional[date]], bool] = {}`，按 `(symbol, day)` 去重。
- **L213-214**：`_process_symbol` 在取信号后立即调用 `_warn_stale_signal_once(symbol, sig, day)`。
- **L408-440 `_warn_stale_signal_once`**：
  - `sig is None` → `False`（不告警，缺信号走独立禁开逻辑）。
  - `threshold = getattr(self._signals, "freshness_threshold", None)`；**`None` → 静默 `return False`**（旧实现/mock 不抛错）。✅
  - `fd <= limit` → `False`（新鲜不告警）。✅
  - 同 `(symbol, day)` 已告警 → `False`（去重，防 60s tick 刷屏）。✅
  - 否则置位并 `log.warning("[告警] 信号陈旧 fd=…")` 返回 `True`。✅
- **day 取 `session.day_label(now)`**（L160 / L201）：夜盘（≥21:00）归属**下一交易日**（L135-145）。即 08-24 夜盘 tick 的 `day=08-25`，与 08-25 白天同 key → 按交易日记去重，符合 P0-3 边界预期。✅

**直接驱动真实方法验证**（5 项全 PASS）：同 symbol/day 多次 tick→仅 1 条；跨 day（08-24→08-25）→再 1 条；新鲜 `fd=0`→不告警且正常开仓；引擎未暴露 `freshness_threshold`（`threshold=None`）→静默；`sig=None`→`False`。

---

## 4. 测试结果

### 4.1 三个新增测试文件（独立运行，全绿）
```
tests/test_signal_freshness.py                9 passed
tests/test_health_check_signal_freshness.py  11 passed
tests/test_scheduler_stale_warn.py            6 passed  → 合计 26 passed（exit 0）
```
- **无 pyarrow 依赖**：三个文件均用内存注入（`monkeypatch SignalEngine._load_cache` / `pd.read_parquet`），未触碰真实 parquet，规避沙箱缺 pyarrow 的环境性失败。✅
- 工程师用例覆盖了隔夜过期、同日新鲜、缓存原 `is_effective=False` 仍拦截、跨日主源过期、级联回退、property、paper.yaml 回归护栏、启动 WARN/OK/FAIL、阈值取配置/缺失回退、口径一致性、scheduler 去重/跨日/新鲜/静默/None。

### 4.2 既有 `tests/test_paper_signals.py`（FFFF）—— 环境性，非回归
- 失败根因：`ModuleNotFoundError: No module named 'pydantic'`（连带 pyarrow 缺失）。该测试依赖真实 parquet 缓存 + 经 `pydantic` 的配置链路，沙箱未安装二者。
- **判定：环境性失败，不是 P0-3 回归**。该文件显式传 `threshold=5`（级联语义不变），而 `signals.py` 级联逻辑（L142-156）本次未改动；级联正确性已由新增 `test_cascade_still_prefers_fresher_backup_under_zero_threshold`（PASS）+ 本复核 §1 独立复算（PASS）双重覆盖。
- **CI 建议**：在含 pyarrow + pydantic 的环境跑 `test_paper_signals.py` 以闭环既有回归护栏。

---

## 5. 边界推演（4 项）

| # | 边界场景 | 验证结论 |
|---|---|---|
| ① | 当天开盘前刷新信号（`fd=0`） | `_process_symbol` 取 `latest_signal` → `is_effective=True` → 风控开仓（见 `test_fresh_signal_does_not_warn` 断言 `position > 0`）。**P0-3 不误伤正常流程**。✅ |
| ② | 夜盘归属下一交易日 | `day_label(08-24 21:30) = 08-25`，与 08-25 白天同 `(symbol, day)` 去重 key；跨自然日（08-24→08-25）再触发告警。✅ |
| ③ | 技术兜底接手路径未被破坏 | `signals.py` L216-217：`if sig is None or not sig.is_effective: sig = technical_fallback(...)`。`is_effective=False` 后必然走兜底；无持仓→禁开（`_all_trades==[]`）。✅ |
| ④ | 阈值=0 长期未刷新 → 仅技术兜底成交 | 全部信号 `fd>=1` → `effective=False` → 恒走 `technical_fallback`；有 K 线则技术兜底成交，无 K 线则禁开。**安全降级，非 bug**。✅ |

---

## 6. QA 裁决与遗留风险

### 裁决：**PASS** ✅

P0-3 根因（周五→周一 `np.busday_count=1 ≤ 5` 误判新鲜）已**真修好**：阈值改为 0 使隔夜信号（`fd>=1`）一律 `is_effective=False`，仅当日（`fd=0`）信号驱动开仓。三处配置一致、启动自检 WARN 不阻断、运行时告警按交易日记去重且对缺 property/mock 静默、级联与技术兜底路径未被破坏，独立复算与 26 个新增测试全部绿色。

### 遗留风险（均非 P0-3 阻断项）

1. **环境性测试缺口（中）**：`test_paper_signals.py`（级联回归护栏）在本沙箱因缺 `pydantic`/`pyarrow` 无法运行。需在完整 CI 环境补跑以闭环既有回归。级联正确性已被新增测试 + 本复核独立覆盖，故不影响本次判定。
2. **历史脚本残留 `=5`（低）**：`artifacts/_qa_verify.py:97` 仍硬编码 `freshness_threshold_days=5`，非运行路径，但易误读。建议加注释或清理。
3. **新鲜度日历不含配置节假日（低/安全方向）**：`_business_days` 用 `np.busday_count` 默认节假日表（空），未纳入 `paper.yaml` 的 `holidays_2026`（春节等）。长假期间 `fd` 会比“实际交易日差”偏大——但阈值=0 下任何 `fd>=1` 都判过期，该偏差仅使 `fd` 数值更大、**偏向更严格（更可能判过期）**，不改变布尔结论，属安全方向，不影响 P0-3 正确性。如未来阈值放宽需修正此口径。

---

*复核期间未修改任何生产代码、未新增提交；临时验证脚本已清理。*
