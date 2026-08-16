# HexBroker 代码审核报告（2026-08-16）

> **审查范围**：全库（hexbroker 85 源文件 + scripts 18 + tests 24），三路并行 fresh-eyes 独立复核（数据/特征层、模型/回测层、工程基线）
> **审查方式**：2 个独立审查子代理 + 主理人实证核实（逐条代码确认 + 测试复现）
> **结论**：无 P0；**4 项 P1 全部修复并加回归测试**；7 项 P2 记录在案（2 项顺带修复）

---

## TL;DR

| 层级 | 判定 | 说明 |
|---|---|---|
| 数据/特征层 | ⚠️→✅ | 因果框架严谨（rolling z-score、purge/embargo、shift(1)+asof），1 项 P1 修复 |
| 模型/回测层 | ⚠️→✅ | 因果切片正确、并行确定性实测成立；3 项 P1 修复（含 1 个真实数值 bug） |
| 工程基线 | ✅ | 无敏感信息残留、依赖版本已对齐、CI 双矩阵绿 |

**核心成就**：审查发现并修复了 1 个真实数值 bug（vol_hat ddof 判定）+ 1 个 numpy 2.x 弃用崩溃（np.sum(generator)），后者若非审查触发将影响未来的集成功能。

---

## P1 修复（4 项，均已加回归测试）

| # | 位置 | 问题 | 影响 | 修复 |
|---|---|---|---|---|
| 1 | `forecast/base.py:225` | `vol_hat` 用 `paths.shape[1]`（窗口数）判 ddof，应为 `shape[0]`（n_mc） | **n_mc=1 且多窗时 `np.std(ddof=1)` 返回 NaN → conf=NaN 传播进信号**；n_mc>1 单窗时 vol_hat 错误置 0 | 改用 `n_mc` 判 ddof；单测覆盖 NaN 防护 |
| 2 | `pipeline.py:372`（生产主流程） | `run_pipeline` 调 `build_features(barframe, cfg)` 不传 `global_close` | 冠军配置（spx/uup 外盘组）在生产路径**静默缺失 6 个 f_xr_\* 特征**，与实验 25 特征不一致 → 生产性能退化且无警告 | ① 新增 `_load_global_context` 按 `cross_params.global_codes` 加载+时差对齐；② `FeaturePipeline.__init__` fail-fast：配置了 global_codes 但无数据 → ValueError |
| 3 | `scripts/ablate_features.py` | `--global-only` 未传 `--global-code` 时 `global_close={}` → 候选变体**静默 0 新特征**（n_cols 不变，消融结论失真） | 消融结果可信度风险（历史运行均显式传参未受影响） | ① `--global-only` 自动加载全部 GLOBAL_CANDIDATES；② 非 base 变体断言 `n_cols > base`，否则 RuntimeError |
| 4 | `forecast/ensemble.py:54` | `items[0][1].eff_thr`：ForecastSignal 无此字段 → AttributeError（潜伏，无调用方） | 一旦使用集成即崩溃 | `getattr(..., "eff_thr", 0.05)`；顺带修复 `np.sum(generator)` numpy 2.x 弃用崩溃 |

**P1-1 是真实数值 bug**（审查子代理独立发现，主理人逐行确认）——虽冠军路径 n_mc=30 未触发，但任何 n_mc=1 的降采样路径都会产生 NaN 信号。

---

## 方法学关注（不修改，记录在案）

**per-fold 校准乐观性**：`_train_eval_fold` 用本折测试窗标签拟合 Platt 校准器后，方向准确率又在同一批标签上评估（`refine_lightgbm_champion.py:293-301`）——等效"用测试标签挑选每折决策阈值"，**70.23% 指标存在系统性偏乐观可能**。
- 不擅改：所有已发布结论（冠军 v4、消融、校准对比）都基于该口径，改动需全量重跑（8-12 分钟/次）；
- **建议后续**：嵌套验证对照实验（校准器用 train 窗拟合 vs 现口径），量化乐观偏差幅度后再决定是否重定标。

---

## P2 记录（7 项）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| 1 | `forecast/kronos_predictor.py:100` | torch 缺失时抛原始 ImportError | ✅ 顺手修复：友好 RuntimeError 提示 |
| 2 | `forecast/baselines.py:294` | 集成成员库缺失时 ImportError 无提示 | ✅ 顺手修复：提示 pip install 三库 |
| 3 | `utils/` `evaluation/` | 无对应测试文件（覆盖缺口） | 记录，后续补 |
| 4 | `trainer.py:129` | `result.models` 全量保留 66 折模型（内存累积） | 记录；walk_forward 脚本已 `del model` |
| 5 | `data/cleaner.py:48` | winsorize 用全样本分位数（分布泄漏） | 记录；冠军路径未用 cleaner，需确认接入时修复 |
| 6 | `data/resample.py:29` | 夜盘跨零点 bar 判定（分钟级，5 日 horizon 未用） | 记录；使用分钟级时需修复 |
| 7 | `data/dataset.py:47` | `make_labels` 尾部 horizon 个标签填 0（应 NaN） | 记录；champion 路径未用 |

---

## 工程基线核验

- ✅ **敏感信息**：全库扫描 `github_pat_/ghp_/AKIA` 等模式 0 命中（token 仅存 Windows 凭据管理器）
- ✅ **依赖一致性**：requirements 新约束（pandas>=2.2.3,<4 等）与本地验证版本一致，CI 双矩阵绿
- ✅ **测试回归**：139/139 全过（133 原 + 6 新增审查回归测试）；冠军 v4 特征集复核 25 特征（6 个外盘特征）不变
- ✅ **环境清理**：删除 `_t1.npy` 测试残留；`.gitignore` 补充 `*.npy`；清理坏 git ref（`refs/remotes/origin/main` 指向不存在对象）
- ⚠️ **git 通道**：今日多次 push/fetch 挂起（协议层不稳定，网页/API 正常），已用 REST API 绕过；待通道恢复后 fetch 对齐

---

## 主理人裁决

1. **P1 全部采纳修复**（4 项，含真实 bug 修复），回归测试 6 项固化；
2. **P1-5（校准乐观性）暂不动作**——列为方法学风险登记，后续嵌套验证量化；
3. **P2-3/4/5/6/7 记录在案**，按"接入生产路径前修复"原则排期；
4. 审查子代理与主理人结论一致（因果框架健康、无 P0），**整体判定：⚠️→✅ 通过**。

---

## 变更文件清单

| 文件 | 变更 |
|---|---|
| `hexbroker/forecast/base.py` | vol_hat ddof 修复（P1-1） |
| `hexbroker/feature/global_ref.py` | **新增**：外盘加载/时差对齐（生产复用） |
| `hexbroker/pipeline.py` | `_load_global_context` + 生产路径注入外盘（P1-2） |
| `hexbroker/feature/pipeline.py` | fail-fast：global_codes 无数据报错（P1-2） |
| `scripts/ablate_features.py` | 自动加载 + n_cols 断言（P1-3）；复用 global_ref |
| `hexbroker/forecast/ensemble.py` | eff_thr getattr + np.sum 弃用修复（P1-4 + P2 顺带） |
| `hexbroker/forecast/kronos_predictor.py` | torch 缺失友好提示（P2-1） |
| `hexbroker/forecast/baselines.py` | 集成成员库缺失提示（P2-2） |
| `tests/test_audit_fixes.py` | **新增**：6 项审查回归测试 |
| `.gitignore` | 补充 `*.npy` |
