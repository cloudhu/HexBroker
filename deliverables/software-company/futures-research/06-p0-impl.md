# P0 四项优化实现说明（06-p0-impl.md）

- **工程师**：寇豆码（Alex）· 2026-08-26
- **实现范围**：P0 四项优化（撮合可信度 / 前视自检 / 四层版本化 / Bootstrap 区间）全量代码（T01–T05）
- **权威输入**：`05-p0-arch.md`（架构设计）＋ `04-p0-prd.md`（验收标准）＋ 主理人 Q1–Q5 裁决
- **红线复核**：默认配置数值零变化（<1e-12）✅；不修改 broker.py / cost.py / forecast/base.py / rl/futures_env.py / evaluation/stats.py / evaluation/metrics.py ✅；SignalStore 只落 OOS + 双闸门判定逻辑零改动 ✅；pyproject.toml 依赖零新增 ✅

---

## 1. 任务完成清单与关键实现说明

### T01 项目基础设施（P0 公共底座）

| 文件 | 类型 | 关键实现 |
|---|---|---|
| `hexbroker/config.py` | 修改 | `BacktestConfig` 新增 `next_bar_execution=False` / `volume_cap=None` / `volume_cap_mode="partial"`；新增 `BootstrapConfig`（`block_len=20 / n_boot=1000 / seed=None / by_symbol=False`）挂到 `backtest.bootstrap` |
| `configs/base.yaml` | 修改 | 显式声明 P0-1 三开关默认关闭 + `bootstrap` 默认值（部署以 yaml 为准） |
| `hexbroker/constants.py` | 修改 | `EXECUTION_DEFAULTS` / `VOLUME_CAP_DEFAULT_RATIO=0.05` / `BOOTSTRAP_DEFAULTS` / `FINGERPRINT_LENGTH=12` / `SIGNAL_SIDECAR_SUFFIX` 等常量 |
| `hexbroker/tools/__init__.py` | 新增 | P0-2 包骨架，导出 `AnalysisReport/SuspiciousEdge/Cycle/ExemptionRegistry` 等 |
| `tests/_helpers.py` | 修改 | 新增 `make_leaky_features()`（6 类前视缺陷样本）、`make_recursive_features()`（A→B→A 回环样本）、`fast_execution_cfg()` |

验收：`pytest tests/test_config.py` 全绿（9/9）；`load_config()` 默认含新字段且与旧默认语义一致。

### T02 P0-1 撮合可信度加固（依赖 T01）

| 文件 | 类型 | 关键实现 |
|---|---|---|
| `hexbroker/backtest/execution.py` | 新增 | `ExecutionConfig`（frozen dataclass，`from_cfg` 读取、`resolve_cap` 支持 per-symbol dict）；`next_bar_ref_price()`（下一 bar open，末根 None）；`cap_order_qty()`（partial 按比例截断 / reject 整单拒绝）；`run_dual_caliber()`（同 bar vs next_bar 双口径，含 Δ%） |
| `hexbroker/backtest/engine.py` | 修改 | `__init__` 新增可选 `execution` 参数（None → `ExecutionConfig.from_cfg(cfg)`）；`run()` 新增两个 `if` 分支（`next_bar_execution` / `volume_cap`），**默认值下分支不进入、与原代码逐语句等价**；缺 `open`/`volume` 列时 fail-fast |
| `hexbroker/backtest/walkforward.py` | 修改 | `run(..., execution=None)` 透传至每段 `BacktestEngine` |
| `docs/matching-assumptions.md` | 新增 | 16 条撮合假设（MA-01…MA-16），每条含内容/默认值/影响方向/代码位置 |
| `tests/test_execution_caliber.py` | 新增 | 同 bar vs next_bar 对照、volume_cap partial/reject/per-symbol、缺列 fail-fast、默认 <1e-12 兜底（参考基线复刻） |
| `tests/test_matching_assumptions.py` | 新增 | 文档结构检查：≥14 条、字段齐全、ID 连续、覆盖 14+ 主题 |

关键决策：Q1 裁决成交价 = 下一 bar `open` ± 滑点（`open.shift(-1)` 预计算，末根 NaN 跳过成交）；Q4 裁决 partial 部分成交、默认 `volume_cap=None`、启用默认 0.05、支持 per-symbol dict（缺失品种回退 0.05）。

### T03 P0-2 前视/递归自检工具（依赖 T01）

| 文件 | 类型 | 关键实现 |
|---|---|---|
| `hexbroker/tools/dag.py` | 新增 | `SuspiciousEdge/Cycle/AnalysisReport`（`violations`/`ok` 属性）；`build_dependency_graph()`（AST 调用图，**仅直接函数调用建边**，避免 `eng.run()` 误指本地 `run` 假环）；`find_suspicious_edges()`；`norm_path()/expand_paths()` |
| `hexbroker/tools/lookahead_analysis.py` | 新增 | AST 危险原语扫描（`shift(-n)`/`rolling(center=True)`/`ewm`/`asof` 前缺 `reindex`/`iloc[i+1]`/`np.roll`）；`analyze()` + CLI `main()`（未豁免缺陷 → 非零退出） |
| `hexbroker/tools/recursive_analysis.py` | 新增 | `detect_cycles()`（DFS 三色标记，A→B→A 与自依赖均检出）；CLI `main()` |
| `hexbroker/tools/exemptions.py` | 新增 | `ExemptionRegistry`（`register/is_exempt/load_yaml/default_exemptions`）；内置 9 条默认豁免（z-score、外盘 reindex→asof、span EMA、前向收益仅作评估、obs 环形缓冲、next_bar 显式开关） |
| `.github/workflows/ci.yml` | 修改 | pytest 后新增 lookahead + recursive 静态自检步骤（`hexbroker/feature hexbroker/backtest hexbroker/pipeline.py`） |
| `tests/test_lookahead_analysis.py` | 新增 | 注入样本 6/6 模式 100% 检出；现有 feature 代码 0 未豁免误报；豁免生效；CLI 退出码；只读不污染 |
| `tests/test_recursive_analysis.py` | 新增 | A→B→A 回环、自依赖检出；干净调用链无环；CLI 退出码 |

验收口径折衷（设计 §1.3）：静态分析以「注入样本 100% 检出 + 现有代码任何检出显式豁免」满足 PRD A2.1/A2.2——CI 命令实测 `LOOKAHEAD_EXIT=0`（0 未豁免缺陷 / 6 已豁免）、`RECURSIVE_EXIT=0`（0 回环）。

### T04 P0-3 四层版本化（依赖 T01）

| 文件 | 类型 | 关键实现 |
|---|---|---|
| `hexbroker/data/manifest.py` | 新增 | `DataManifest` dataclass + `content_fingerprint()`（sha1 规范化内容前 12 位，列排序无关、内容必变）+ `write_manifest/read_manifest/build_manifest/backfill_manifests`（sidecar JSON，原子写） |
| `hexbroker/data/store.py` | 修改 | `DataLake.__init__` 新增可选 `constants`；`save_processed` 写 parquet 后自动写 `processed/{symbol}/{freq}/manifest.json`（读路径零改动） |
| `hexbroker/utils/fingerprint.py` | 修改 | `FourLayerFingerprint`（frozen，结构相等）+ `compute_four_layer()`（数据/特征/模型/参数/配置五字段）+ `four_layer_report_block()` |
| `hexbroker/forecast/signal_store.py` | 修改 | `put(signals, fingerprint=None)` 向后兼容：fingerprint 非 None 时写 `{root}/{model_id}/{train_end}.manifest.json` sidecar（**Parquet schema 不变**），同分片指纹不一致 → `warnings.warn`；新增 `verify()` / `fingerprints()` |
| `hexbroker/forecast/trainer.py` | 修改 | `run(..., fingerprint=None)`：None 时由 trainer 用 `compute_four_layer`（utils，不引入模型）计算后随 `store.put` 落盘 |
| `scripts/backfill_manifests.py` | 新增 | 存量 parquet 分区一键回填 manifest（PRD A3.1 迁移脚本） |
| `tests/test_data_manifest.py` | 新增 | 指纹确定性/敏感性、manifest 写读、回填（年分区 + 平铺）、save_processed 自动 manifest |
| `tests/test_signal_fingerprint.py` | 新增 | 四层指纹可复现 + 对数据/特征/模型/参数/全局配置五维敏感性；sidecar 写读/校验；无 fingerprint 向后兼容；bad type raise |

关键决策：Q3 裁决 sidecar JSON、不改 Parquet schema——`get_frame` 消费方零改动，RL env / baseline / pipeline 读取路径不受影响。

### T05 P0-4 Bootstrap + 报告集成 + 全量回归（依赖 T01/T02/T04）

| 文件 | 类型 | 关键实现 |
|---|---|---|
| `hexbroker/evaluation/bootstrap.py` | 新增 | `block_bootstrap()`（circular block，numpy 向量化 `(n_boot, n)`）；`MetricCI/BootstrapResult`；`bootstrap_metrics_ci()`（点估计 = `compute_metrics` 同口径，CI = 2.5%–97.5% 分位，`by_symbol` 截面）；`bootstrap_report_block()` |
| `hexbroker/evaluation/__init__.py` | 修改 | 导出 bootstrap 5 个符号 |
| `hexbroker/pipeline.py` | 修改 | `_signal_eval` 返回 `fingerprints`；`run_pipeline(..., enable_dual_caliber=True)` 新增 `summary["dual_caliber"]` / `summary["fingerprints"]` / `summary["bootstrap"]` 三块（既有 key 一律不动）；`_render_markdown` 追加章节 7/8/9；CLI 新增 `--no-dual-caliber` |
| `tests/test_bootstrap.py` | 新增 | MC 覆盖率 ∈[0.90,1.00]、固定 seed 可复现、点估计与 compute_metrics 一致、性能 <60s、by_symbol 截面 |
| `tests/test_report_integration.py` | 新增 | 报告含三块且旧 key 不变；`--no-dual-caliber` 关闭；指纹随报告输出 |
| `tests/test_default_behavior_unchanged.py` | 新增 | 红线：默认配置（多品种/含涨跌停/阈值策略/walkforward）与「改动前参考基线」权益曲线 + 五指标 <1e-12 一致 |

关键决策：Q2 裁决双口径默认输出（`--no-dual-caliber` 可关）；Q5 裁决 `block_len=20/n_boot=1000/seed=cfg.seed/by_symbol=False`。DSR/PBO/`compute_metrics` 计算逻辑零改动（bootstrap 并列输出、不参与闸门判定）。

---

## 2. 与设计的偏差记录

| # | 设计（05-p0-arch.md） | 实际实现 | 理由 |
|---|---|---|---|
| D-01 | `SignalStore.put` 的 `fingerprint` 类型标注为 `"FourLayerFingerprint \| dict \| None"` | 实现为 `"Any \| None"`（运行期校验仍限 FourLayerFingerprint/dict，bad type raise） | 避免 signal_store 在模块顶部依赖 utils.fingerprint（保持延迟导入，规避潜在环） |
| D-02 | pipeline 报告指纹块调用 `fingerprint.py::four_layer_report_block()` | pipeline 直接使用 `store.fingerprints()` 返回的 sidecar 记录（已含四层字段 + model_id/train_end） | 指纹由 trainer 内部计算并落盘，pipeline 读取 sidecar 记录即可；`four_layer_report_block` 仍作为独立报告块助手提供并经测试 |
| D-03 | `tools/dag.build_dependency_graph` 对调用目标做 Name+Attribute 双解析 | 仅解析直接函数调用（`ast.Name`）；`obj.method()` 不建边 | 静态方法归属无法确定：`eng.run()` 会被误指到本地同名 `run` 函数形成假环（实测 walkforward 假回环） |
| D-04 | CI 自检范围未指定 | 定为 `hexbroker/feature hexbroker/backtest hexbroker/pipeline.py` | 与「现有代码 0 误报」测试同域，避免把 RL/paper 等含合法未来引用的模块纳入 CI 门槛 |
| D-05 | `compute_four_layer` 的 `data_version` 设计为「manifest data_version + 内容指纹」 | barframe 无 manifest 时退化为 `"cf:" + 内容指纹`；metadata 携带 data_version 时用 `"{data_version}:{cf}"` | barframe 不总携带 manifest，内容指纹保证同配置一致/改数据必变（A3.3/A3.4 验收满足） |
| D-06 | 文档称测试总数 315 | 本仓库实测基线 476（`pytest --collect-only`），改动后 549 全绿 | 以实际代码为准（任务要求「以实际代码为准并记录偏差」）；PRD 要求「>315」满足 |

无接口签名变更（所有新增参数均为可选、默认值向后兼容）；无「不修改」清单内文件被触碰。

---

## 3. 默认零变化验证结果（红线）

验证方式：由于 `engine.py` 为原地修改，测试用**未改动模块**（`SimBroker` / `CostModel` / `Portfolio`）逐语句复刻改动前 `BacktestEngine.run` 作参考基线，断言默认配置下输出与基线完全一致。

- `tests/test_default_behavior_unchanged.py::test_default_engine_equals_pre_change_reference`：多品种（SHFE.cu+DCE.m）+ 阈值策略，权益曲线逐点差 `max < 1e-12`；sharpe/calmar/max_drawdown/total_return/final_equity/win_rate 六指标 `abs diff < 1e-12` ✅
- `test_default_engine_with_limit_columns_matches_reference`：含 limit_up/down 列时与改动前 P8 行为一致（<1e-12）✅
- `test_explicit_default_execution_equals_implicit`：`execution=None` 与显式 `ExecutionConfig()` 完全等价 ✅
- `test_default_config_fields_are_noop_values`：`next_bar_execution=False / volume_cap=None / volume_cap_mode="partial" / bootstrap 默认` ✅
- `tests/test_execution_caliber.py::test_default_engine_equals_pre_change_reference`：同口径独立验证 ✅
- `test_walkforward_default_unchanged`：WalkForwardBacktester 默认路径逐段 final_equity 与参考基线 <1e-12 ✅
- 既有回归全绿：`test_backtest_env_consistency`（训练-回测一致性 <1e-6）、`test_engine_limit_trade`（P8 涨跌停）、`test_broker_multiplier_pnl`、`test_signal_contract`、`test_validate_oos_r7`（OOS 隔离）等全部通过 ✅

结论：**默认配置下代码路径与改动前数值完全等价（<1e-12），生产基线口径未触碰。**

---

## 4. 全局一致性审查

审查范围：全部 33 个新增/修改文件作为整体读查（跨文件 import、接口契约、数据流、重复实现）。

1. **跨文件 import 一致性**：`engine → execution`、`execution`（延迟）→ `engine/metrics`、`store → manifest`、`signal_store`（延迟）→ `fingerprint`、`trainer`（延迟）→ `fingerprint`、`fingerprint`（延迟）→ `manifest/config`、`pipeline`（延迟）→ `execution/bootstrap`、`evaluation/__init__ → bootstrap`、`tools/__init__ → dag/exemptions`、`lookahead_analysis/recursive_analysis → dag/exemptions`——无缺失、无环。
2. **接口契约合规**：`BacktestEngine.__init__(cfg, cost=None, initial_capital=None, execution=None)`、`WalkForwardBacktester.run(..., execution=None)`、`SignalStore.put(signals, fingerprint=None)`、`ForecastTrainer.run(barframe, feature_frame, fingerprint=None)`、`DataLake(root=None, constants=None)`——全部向后兼容（可选参数默认值）。
3. **数据流正确性**：trainer 计算四层指纹 → `store.put` 写 sidecar → pipeline `store.fingerprints()` 读回入报告；pipeline 候选（RL 或 signal_threshold）targets/equity 正确喂给 `run_dual_caliber` / `bootstrap_metrics_ci`；`--no-dual-caliber` 关闭后 `summary["dual_caliber"]=None` 且旧 key 不变。
4. **无重复实现**：`cap_order_qty` / `content_fingerprint` / `block_bootstrap` 各单点实现；DSR/PBO/`compute_metrics` 只读复用未改。

### IS_PASS: **YES**

（审查中发现并已修复的 2 项问题：① 递归分析对 `eng.run()` 的假环——改为仅直接函数调用建边；② `engine.py` 的 `shift(-1)` 显式开关需默认豁免——已登记 `shift_negative@hexbroker/backtest/engine.py`。修复后全量复测通过。）

---

## 5. 测试运行结果摘要

- 全量 `python -m pytest`（CI 同款）：**549 passed / 0 failed / 0 errors / 11 skipped**（改动前基线 476 collected → 新增 73 项）
- 新增 9 个测试文件全部通过：`test_execution_caliber` / `test_matching_assumptions` / `test_lookahead_analysis` / `test_recursive_analysis` / `test_data_manifest` / `test_signal_fingerprint` / `test_bootstrap` / `test_report_integration` / `test_default_behavior_unchanged`
- 既有回归重点复核：`test_config`（9/9）、`test_backtest_env_consistency`、`test_engine_limit_trade`、`test_walkforward_aggregate`、`test_signal_contract`、`test_validate_oos_r7` 全绿
- CI 静态自检命令：`lookahead_analysis`（0 未豁免缺陷 / 6 已豁免）与 `recursive_analysis`（0 回环）均退出码 0

*文档结束。关联输入：`04-p0-prd.md`（需求）｜ `05-p0-arch.md`（架构设计）｜ `README.md`（红线）。*
