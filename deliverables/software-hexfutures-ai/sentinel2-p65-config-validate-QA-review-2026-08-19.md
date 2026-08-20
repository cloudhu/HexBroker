# P6-5 v4 生产配置固化 — QA 独立复核报告（fresh-eyes）

- **QA**：严过关（Yan）
- **日期**：2026-08-19
- **对象**：HexBroker P6-5（v4 生产配置固化）
- **复核方式**：静态审查（逐文件读码 + git diff + mtime 证据）+ 独立复跑（p6_5_config_validate.py / load_config / 全量 pytest）+ 独立验证脚本（`qa_p65_independent_review.py`，真实数据端到端，非 mock）+ 异常兜底分支 mock 验证
- **复核环境**：Python `C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`，cwd `E:/Workspace/HexBroker`

---

## 1. 静态审查表

### 1.1 hexbroker/config.py — EngineAConfig + BacktestConfig 挂载

| 审查项 | 结论 | 证据 |
|---|---|---|
| EngineAConfig 存在 | ✅ | `hexbroker/config.py:133-149` |
| 位置在 EngineBConfig 之前 | ✅ | EngineAConfig L133 → EngineBConfig L152 |
| `model_config = _MODEL_CFG`（extra=allow, validate_assignment） | ✅ | L144；运行时确认 `model_config.get('extra') == 'allow'` |
| 字段/默认值：`enabled=True` | ✅ | L145 |
| 字段/默认值：`signal_cache="artifacts/signals_cache18_grouped_v4.parquet"` | ✅ | L146 |
| 字段/默认值：`top_k=0.30` | ✅ | L147 |
| 字段/默认值：`min_symbols=3` | ✅ | L148 |
| 字段/默认值：`notional_frac=0.20` | ✅ | L149 |
| BacktestConfig 挂载 `engine_a: Field(default_factory=EngineAConfig)` | ✅ | L199（engine_b L197 / combo L198 保持，注释标明 P5/P6-5 终裁） |
| engine_b/combo 回归未破坏 | ✅ | L152-178 与运行输出一致（win252/thr0.7 / A15/B85 / vol_target=False） |

### 1.2 scripts/p5_engineA_cross_section.py — _resolve_cache_path + engine_a_targets_cs

| 审查项 | 结论 | 证据 |
|---|---|---|
| `_resolve_cache_path` 存在 | ✅ | L83-96 |
| 显式 cache_path 优先（原样返回） | ✅ | L89-90（相对路径原样返回，由 pd.read_parquet 按 cwd 解析——设计如此） |
| None → 读配置 `load_config().backtest.engine_a.signal_cache` | ✅ | L91-92 |
| 配置异常兜底回退 v2（SIGNALS_PATH） | ✅ | L93-94；独立 mock 验证：load_config 抛 RuntimeError → 回退 `...signals_cache18_grouped_v2.parquet` ✅ |
| 相对路径 ROOT 拼接（None 分支） | ✅ | L96 `p if p.is_absolute() else ROOT / p`；验证 None → `E:\Workspace\HexBroker\artifacts\signals_cache18_grouped_v4.parquet` ✅ |
| `SIGNALS_PATH = ART / "signals_cache18_grouped_v2.parquet"` 兜底 | ✅ | L66 |
| `engine_a_targets_cs` 签名含 `cache_path: Path\|str\|None = None` | ✅ | L103 |
| 读取处 `pd.read_parquet(_resolve_cache_path(cache_path))` | ✅ | L127 |
| 显式 v2 路径可用（向后兼容） | ✅ | 独立真实端到端验证（见 §3） |

### 1.3 git diff 改动范围

| 文件 | 状态 | 判定 |
|---|---|---|
| hexbroker/config.py | modified | ✅ P6-5 增量=EngineAConfig + engine_a 挂载。注意：相对 HEAD 的 diff 还含 EngineBConfig/ComboConfig（P5/P6-1 固化当时未提交），属工作区既有未提交内容，非本次新改（先前 P6-3B QA 记录同证） |
| scripts/p5_engineA_cross_section.py | untracked（新增） | ✅ 整文件为工作区新增（P5 起未提交），无法用 git 隔离 P6-5 增量；已用 mtime（18:48:02）+ 逐行代码审查确认 `_resolve_cache_path` 为本次新增点 |
| scripts/p6_5_config_validate.py | untracked（新增） | ✅ 新增验证脚本，mtime 18:48:21 |
| docs/developer-guide.md | modified（18:44:32） | ⚠️ 非代码改动；内容为 Sentinel-2 P0→P6-3b 文档汇总（v3.7），**未包含 P6-5 EngineAConfig 文档**——轻微文档滞后，建议后续补充 |
| scripts/group_modeling.py / group_modeling_v2.py / refine_lightgbm_champion.py / tests/test_refine_lightgbm.py | modified | ✅ mtime 08-18 17:58（信号生成/精调时代），早于 P6-5 窗口，非本次改动 |
| 公众号文章/published_log.json 等 | modified | ✅ 无关内容 |

**缓存与数据文件完整性（mtime 证据）**：

| 文件 | mtime | 判定 |
|---|---|---|
| artifacts/signals_cache18_grouped_v4.parquet | 2026-08-19 18:29:39（8624 行/18 品种/662 天） | ✅ 早于 P6-5 文件（18:47+），未被 P6-5 触碰；行数与 P6-3B QA 记录一致 |
| artifacts/signals_cache18_grouped_v2.parquet | 2026-08-18 15:34:56（7952 行/18 品种/976 天） | ✅ 早于 v4 与 P6-5，未被触碰；与 P6-2/3 记录一致 |
| data/raw、data/interim | 最新 08-17/08-18 | ✅ P6-5 期间无数据文件改动 |
| config.py / p5 / p6_5 mtime | 18:47:54 / 18:48:02 / 18:48:21 | ✅ 全部晚于 v4 缓存创建 |

---

## 2. 复跑对比

### 2.1 `python scripts/p6_5_config_validate.py`

```
========================================================================
P6-5：v4 生产配置固化验证（引擎 A → v4 缓存 + A15/B85 + B win252/thr0.7）
========================================================================
[1] 默认配置 load_config()          21 项 [PASS]
[2] 旧 yaml（无 engine_a 字段）向后兼容   9 项 [PASS]
[3] yaml 覆盖 engine_a 嵌套字段（含显式 v2）7 项 [PASS]
[4] engine_a_targets_cs() 无参调用默认读 v4（mock 轻量验证）3 项 [PASS]
[5] pytest 回归（tests/test_config.py 等） 2 项 [PASS]
========================================================================
[PASS] 全部检查通过：v4 生产配置已固化（引擎A→v4缓存 + A15/B85 + B win252/thr0.7）
      向后兼容：旧 yaml 无 engine_a 字段取默认；显式 cache_path/v2 路径不受影响
========================================================================
EXIT_CODE=0
```

- ✅ 全部 PASS，退出码 0。
- ⚠️ **轻微差异**：工程师声称"34 项断言全 PASS"，实际脚本内 `check()` 调用为 **42 项**（21+9+7+3+2），全部 PASS。数量声称不精确，不影响结论（诚实记录）。

### 2.2 `load_config()` 默认值核对

```
engine_a: {'enabled': True, 'signal_cache': 'artifacts/signals_cache18_grouped_v4.parquet',
           'top_k': 0.3, 'min_symbols': 3, 'notional_frac': 0.2}
engine_b: {'enabled': True, 'win': 252, 'thr': 0.7, 'notional_frac': 0.2}
combo:    {'w_engine_a': 0.15, 'w_engine_b': 0.85, 'vol_target': False,
           'vol_target_ann': 0.175, 'vol_ewma_halflife': 10}
fingerprint: e4f5562ba5a1 | type: EngineAConfig | model_config extra: allow
```

- ✅ 与 P6-5 生产基准完全一致。

### 2.3 pytest 全量回归

```
........................................................................ [ 31%]
........................................................................ [ 63%]
........................................................................ [ 94%]
............                                                             [100%]
EXIT_CODE=0
```

- ✅ **228 passed**（工程师声称 17 passed——数字口径不同；实测全量 228 全绿，覆盖更广）。退出码 0。

---

## 3. 独立验证（fresh-eyes，独立实现）

独立脚本 `qa_p65_independent_review.py`（临时目录，不入库），**真实数据端到端、非 mock**。

### A. 向后兼容：最小旧 yaml（含旧字段、无 engine_a）
自己写最小 yaml（experiment/seed/data.symbols/backtest.initial_capital/engine_b.win=100，无 engine_a）→ load_config：
- ✅ 旧字段全部保留（experiment/seed/data.symbols/initial_capital/engine_b.win=100）
- ✅ 无 engine_a → signal_cache 取默认 v4、top_k=0.30、min_symbols=3、notional_frac=0.20、enabled=True

### B. yaml 覆盖 engine_a 嵌套字段
- ✅ `engine_a.top_k: 0.25` → 生效（实际 0.25）
- ✅ `engine_a.signal_cache: v2 路径` → 生效
- ✅ 未覆盖字段 min_symbols 保持默认 3

### C. 默认缓存解析（真实数据，内容级证据）
```
prices: 37559 行 / 18 品种
默认调用目标: 8624 行 / 662 天 / 做多行 1969
[PASS] 默认调用天数 == v4 缓存天数(662) → 实读 v4
```
- ✅ `engine_a_targets_cs(prices)` 无 cache_path → 输出天数 662 == v4 缓存天数（v2=976），**内容级证明默认实读 v4 文件**（非仅路径断言）。

### D. 显式 cache_path=v2 向后兼容（真实读 v2）
```
显式 v2 目标: 7952 行 / 976 天 / 做多行 1846
[PASS] 显式 v2 天数 == v2 缓存天数(976) → 实读 v2（向后兼容）
[PASS] 默认(v4)与显式 v2 结果不同（证明缓存选择生效）
```

### E. _resolve_cache_path 路径解析
- ✅ None → `E:\Workspace\HexBroker\artifacts\signals_cache18_grouped_v4.parquet`（ROOT 拼接绝对路径）
- ✅ 显式相对 v2 → 原样返回相对路径
- ✅ 显式绝对路径 → 原样返回

### F. 异常兜底分支（mock 补充验证）
- ✅ load_config 抛 RuntimeError → `_resolve_cache_path(None)` 回退 `SIGNALS_PATH`（v2 绝对路径）
- ✅ 配置显式返回 v2 → 生效

### G. 真实生产 yaml 兼容
- ✅ `configs/experiment/` 下 5 个 yaml 全部可加载，engine_a 均取默认 v4/top_k=0.30/min_symbols=3，engine_b/combo 保持固化值。

---

## 4. 最终裁决

### 裁决：**VERIFIED ✅**

| 维度 | 结论 |
|---|---|
| 配置固化正确性（引擎A→v4 + A15/B85 + B win252/thr0.7） | ✅ 字段/默认值/挂载位置/model_config 全部正确 |
| 向后兼容（旧 yaml 不破坏） | ✅ 最小旧 yaml + 真实生产 yaml 5 份均验证通过 |
| 默认缓存解析（无参 → v4） | ✅ 真实端到端内容级证明（662 天==v4） |
| 显式覆盖优先 + 显式 v2 可用 | ✅ top_k=0.25/signal_cache 覆盖生效；显式 v2 976 天==v2 实读 |
| 不改 v2/v4 缓存与数据文件 | ✅ mtime 证据：v2=08-18 15:34、v4=08-19 18:29，均早于 P6-5 文件（18:47+） |

**生产配置就绪状态：READY ✅** — `load_config()` 默认即生产口径：引擎A→v4 缓存（top_k=0.30/min_symbols=3/notional_frac=0.20）+ 组合 A15/B85（vol_target=False）+ 引擎B win252/thr0.7；configs/ 下生产 yaml 无 engine_a 引用，依赖代码默认值，即默认即生效。

### 非阻塞观察（不影响裁决，建议跟进）
1. **断言数量声称不精确**：工程师报告"34 项断言"，实测 p6_5_config_validate.py 为 42 项 `check()`，全部 PASS。建议后续交付物以实测输出为准。
2. **pytest 数量口径**：工程师报告"17 passed"，实测全量 228 passed（覆盖更广），非矛盾，仅口径不同。
3. **docs 滞后**：docs/developer-guide.md（v3.7）未记录 P6-5 EngineAConfig 固化，建议补充（非代码问题）。
4. **git 卫生**：config.py 相对 HEAD 的 diff 混有 P5/P6-1 的 EngineB/Combo 固化（未提交），建议 P5/P6-1/P6-5 固化一次性提交，便于日后追溯 P6-5 精确增量；p5_engineA_cross_section.py 整文件未跟踪，无法 git 隔离 P6-5 增量（已用 mtime+代码审查补证）。
5. **显式相对 cache_path 语义**：`_resolve_cache_path` 对显式相对路径原样返回（由 pd.read_parquet 按 cwd 解析）；p5 脚本内已 os.chdir(ROOT) 无碍，但外部调用方若在其它 cwd 传相对路径需自担解析。设计如此，低风险。

**Routing Decision: NoOne** — 全部测试通过，无源码缺陷、无测试缺陷；以上仅为记录性观察，不阻塞生产采纳。
