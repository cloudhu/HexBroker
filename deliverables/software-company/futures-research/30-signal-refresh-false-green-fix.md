# 30 · 信号新鲜度门禁「假绿」缺陷修复报告（P0）

> 阶段：缺陷修复 + 回归锁 · 日期：2026-08-28
> 触发：主理人批复「执行 A 与 push」——执行 A（刷新信号缓存恢复交易）前的接线校验
> 铁律：无证据不翻转 · 全量 pytest 门禁 · ruff 静态门禁 · feat+docs 双提交

---

## TL;DR

执行 A 之前对 B+C 防再发批次做接线校验，**发现防再发功能自身存在两处 P0 缺陷**——
它不但抓不到停摆，还会主动打印「检查通过」：

| 编号 | 缺陷 | 后果 | 状态 |
|------|------|------|------|
| **D1** | `signal_refresh` 配置块位于 `paper.yaml` **顶层**，而传入的 `paper_cfg` 仅为 `paper` 子段 | 整段配置**从未被读到**（死配置），阈值/自动刷新全部回退默认值 | ✅ 已修 |
| **D2** | `probe()` 采用**目录扫描**，默认目录 `data/signal_caches` **不存在** | 返回空列表 → 打印「信号缓存新鲜度检查通过」→ **假绿**，告警恒不触发 | ✅ 已修 |
| **D3** | ruff 门禁测试只认 `python -m ruff`，托管 venv 无 ruff 二进制 | 生产解释器下门禁**假失败**（CI 信号不稳定） | ✅ 已修 |

**判定：P0 —— 防再发设施失效。先修设施，再执行 A。**（已完成修复 + 回归锁）

---

## 一、取证（修复前实测，全部可复现）

### 证据 1：真实信号缓存已过期（停摆直接原因）

```
artifacts/signals_cache18_grouped_v8_tail_ext.parquet: rows=9361 latest=2026-08-27  fd=1   ← 主源
artifacts/signals_cache18_grouped_v8.parquet         : rows=8624 latest=2026-06-29  fd=44  ← 兜底
```

`freshness_threshold_days: 0`（P0-3 隔夜过期口径）→ `fd=1 > 0` 即过期 → 技术兜底
`is_effective=False` → **硬禁开仓**。与 `deliverables/trading_stall_analysis_20260828.md`
的四层根因链一致：运维未刷新 → fd=1 → P0-3 过期 → P1-1/P1-2 禁开。

### 证据 2：D1 死配置

`configs/paper.yaml` 原结构：

```yaml
paper:
  signal_caches: [...]      # ← 在 paper 段内
signal_refresh:             # ← 顶层！
  threshold: 0
  auto_enabled: false
```

而 `_load_paper_config()` 只返回 `full.paper`：

```python
paper_dict = getattr(full, "paper", None)
return OmegaConf.create(paper_dict)   # 顶层 signal_refresh 永远读不到
```

→ `paper_cfg.get("signal_refresh", {})` 恒为 `{}` → 死配置。

### 证据 3：D2 假绿

```python
cache_dir = str(sect.get("cache_dir", "data/signal_caches"))
probes = probe(cache_dir, threshold=threshold)      # 目录不存在 → []
if not banner:
    print(f"[模拟盘] 信号缓存新鲜度检查通过（fd<={threshold}）")   # ← 假绿
```

`probe()` 对缺失目录返回空列表（`fail-safe` 设计），但**调用方把"空"当成"全部通过"**——
`data/signal_caches` 目录本就不存在，故告警 100% 不会触发。

### 证据 4：D3 门禁假失败

```
$ <venv>\python.exe -m ruff --version
RuffNotFound: Could not find the ruff binary ...
$ ruff --version            # PATH
ruff 0.15.20
```

托管 venv 装了 ruff **包**但未随包附带**二进制**；门禁测试仅用 `sys.executable -m ruff`
→ 生产解释器下 `test_ruff_f821_gate` 必然失败。

---

## 二、修复方案

### D1 —— 配置层级归位（铁律注释 + 层级锁测试）

`signal_refresh` 整段移入 `paper:` 之下，并写入铁律注释：

```yaml
  # ⚠️ 层级铁律：本段必须位于 paper: 之下。启动期传入的 paper_cfg 仅为 paper 子段
  # （见 _load_paper_config），顶层同键不会被读到 → 配置静默失效（2026-08-28 缺陷 D1）。
  signal_refresh:
    threshold: 0
    auto_enabled: false
    timeout_sec: 900
    cache_paths: []      # 留空→继承 paper.signal_caches
```

### D2 —— 改用显式文件列表探测（与 SignalEngine 同口径）

| 新增/变更 | 说明 |
|-----------|------|
| `FreshnessProbe.exists` | 新增字段；`unknown` 属性 = 缺失 或 `fd is None` |
| `probe_files(paths, ...)` | **新主入口**：缺失/无 `ts` 列/解析失败 → 产出 `fd=None` 探测项，**不静默丢弃** |
| `resolve_cache_paths(paper_cfg)` | 镜像 `_validate`/`build_components_safe` 解析逻辑（`signal_caches` 列表优先，兼容旧单键） |
| `format_banner()` | `stale` 与 `unknown` 双分区；**空列表也出横幅**（"没查到"≠"通过"） |
| `_check_signal_freshness()` | 改用 `probe_files(resolve_cache_paths(...))`；`cache_dir` 键废弃 |

**核心语义变更：`format_banner([]) != ""`** —— 探测结果为空属"无法判定"，必须显性告警。

### D3 —— 门禁跨解释器稳定

`_ruff_cmd()`：PATH 二进制优先 → 回退 `python -m ruff` → 两者皆无则 `pytest.skip`；
执行报 `RuffNotFound` 也降级为 skip（环境缺失 ≠ 代码缺陷）。新增
`test_ruff_gate_is_interpreter_agnostic` 确保门禁不会静默失效。

---

## 三、修复后验证（证据）

**启动期真实配置输出**（修复前会打印"检查通过"）：

```
================================================================
⛔ 信号缓存陈旧 —— 主源信号已过期，今日将【不会开仓】
================================================================
  · signals_cache18_grouped_v8_tail_ext.parquet  最新=2026-08-27 00:00  fd=1>阈值0
  · signals_cache18_grouped_v8.parquet          最新=2026-06-29 00:00  fd=44>阈值0
  → 修复命令：python scripts/p22_tail_ext.py --skip-eval
  （刷新后重跑 health_check 验证 fd=0 再启动交易）
================================================================
[模拟盘] 信号刷新：自动刷新未启用（signal_refresh.auto_enabled=false）
```

**门禁：**

| 项 | 结果 |
|----|------|
| 全量 pytest（托管 venv = 生产解释器） | ✅ **658 passed**（653 → +5） |
| ruff（PATH 0.15.20） | ✅ All checks passed |

---

## 四、主理人裁决

| 项 | 裁决 |
|----|------|
| D1/D2 定性 | **P0** —— 防再发设施自身失效，必须先于 A 修复 |
| 修复方案 | **采纳**（显式路径探测 + 层级归位 + 空结果告警） |
| 配置层级铁律 | 入库并加测试锁；后续新增启动期配置一律置于 `paper:` 段内 |
| 旧键 `cache_dir` | **废弃**（代码已不读），yaml 改 `cache_paths: []` |
| A（刷新缓存恢复交易） | **修复后执行**（见下节） |

---

## 五、文件清单

| 文件 | 变更 |
|------|------|
| `hexbroker/diagnostics/signal_refresh.py` | 增 `exists`/`unknown`、`probe_files`、`resolve_cache_paths`、`unknown_of`；`format_banner` 空列表告警 |
| `configs/paper.yaml` | `signal_refresh` 移入 `paper:` 段 + 铁律注释；`cache_dir` → `cache_paths: []` |
| `scripts/paper_trading_main.py` | `_check_signal_freshness` 改用 `probe_files(resolve_cache_paths(...))` |
| `tests/test_signal_refresh_gate.py` | +4 用例（D1 层级锁 / D2 缺失不静默 / 空列表横幅 / 生产路径可探测） |
| `tests/test_audit_ruff_gate.py` | `_ruff_cmd()` 跨解释器定位 + 1 用例（门禁不静默失效） |
| `deliverables/…/30-signal-refresh-false-green-fix.md` | 本报告 |

---

## 状态

✅ D1 已修 ✅ D2 已修 ✅ D3 已修 ✅ 658 全绿 ✅ ruff 通过
⏭️ 下一步：执行 A（刷新信号缓存 → 验证 fd=0）→ push 远程
