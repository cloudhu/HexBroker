# LightGBM 冠军模型多模型集成对比报告

> 报告日期：**2026-08-16** | 品种：['SHFE.au','SHFE.ag','DCE.m'] | horizon=5 n_mc=30 | 全 66 折 walk-forward，调优后 HP，冠军 v4 特征集（25 特征），per-fold Platt 校准

## TL;DR

- 实现 `EnsembleForecast`（LGB+XGB+CatBoost 可配置，均值/方差分别回归 + 路径采样集成）。
- **全 66 折对比：LightGBM 单模型 70.23% 保持冠军；Ensemble（LGB+XGB）68.77%（-1.47pp）不采纳**。
- **工程发现**：CatBoost 在本环境拟合残差方差目标时触发**进程级原生崩溃**（SIGSEGV，无法 try/except），已默认降级为 LGB+XGB 双成员，三成员需显式启用。
- **模型侧探索收官**：特征面封顶 + 校准定稿（Platt）+ 集成负贡献——**冠军 v4（25 特征 + LightGBM + Platt）是实证最优终态**。

## 1. 对比结果（全 66 折）

| 模型 | 方向准确率 | Δpp | RankIC | coverage | 有效准确率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| **LightGBM 单模型（冠军）** | **70.23%** | — | 0.4510 | **85.04%** | **72.99%** |
| Ensemble（LGB+XGB） | 68.77% | **-1.47** | **0.4544** (+0.0034) | 82.75% | 72.30% |

分品种（方向准确率）：
- LightGBM：ag0 68.18% / au0 73.31% / m0 69.21%
- Ensemble：ag0 67.30% / au0 69.94% / m0 69.06%（au0 掉 3.37pp 拖累最大）

## 2. 解读：为什么集成负贡献？

1. **调优后的单模型已接近强局部最优**：冠军 LightGBM 的 HP 是在同一 walk-forward 框架下 Optuna 精调的（200 树/深度 11/leaves 57），单模型本身方差已低；XGB 成员使用映射的近似 HP（min_child_weight 等做了粗略换算）并非各自最优，拖累集成。
2. **RankIC 微升（+0.0034）但方向准确率降（-1.47pp）**：与 Isotonic 同模式——集成压低方差、改善排序连续性，但成员间的分歧把 p_up 推向中间区，削弱了方向判断的锐度（au0 尤其明显）。
3. **coverage 也降 2.3pp**：集成后的概率分布更集中，is_effective 判定（|p_up-0.5|>thr）覆盖减少。
4. **结论一致性**：多模型集成、Isotonic 校准、外盘扩展、特征裁剪、周线特征——**全部负贡献**，共同指向"冠军 v4 配置已是该数据/框架下的性能上界"。

## 3. 工程备注：CatBoost 原生崩溃

- 现象：CatBoostRegressor 拟合残差平方目标（含大量近零值）时进程被硬杀（SIGSEGV 级，无 Python traceback，faulthandler 也不触发）。
- 排查：单成员 mean-fit（MAE）正常（73s）；var-fit 在真实数据 871 样本/750 维下必崩；合成数据正常——疑似 Windows 环境 CatBoost 直方图构建在退化目标上的原生缺陷。
- 处置：`EnsembleForecast` 默认成员 `lgb,xgb`（均稳定）；`ensemble_members="lgb,xgb,cat"` 可显式尝试三成员（不推荐，会崩）。
- 附：方差目标统一改用 `log1p(r²·1e4)` 缩放（predict 逆变换），对数值稳定性更友好（该设计保留）。

## 4. 结论

- **冠军维持 LightGBM 单模型（v4 特征集 + Platt 校准，70.23% / RankIC 0.4510）**。
- **模型侧探索收官**：集成（-1.47pp）、Isotonic（-2.93pp）均负贡献——与特征侧结论一致。
- `EnsembleForecast` 保留为可复用组件（未来若出现更强的单模型或更长的测试窗可重测）。

## 5. 文件清单

| 文件 | 说明 |
| --- | --- |
| `hexbroker/forecast/baselines.py` | 新增 `EnsembleForecast`（LGB+XGB，可配置含 cat；log1p 方差目标；成员级降级） |
| `scripts/refine_lightgbm_champion.py` | `walk_forward_lightgbm` 支持 `model_cls` 参数（默认不变） |
| `scripts/compare_ensemble.py` | 集成对比脚本（`--with-catboost` 可尝试三成员） |
| `deliverables/.../lightgbm-ensemble-compare-2026-08-16.json` | 对比明细 |
| `requirements-optional.txt` | 追加 xgboost/catboost（隔离 venv 已装） |

## 6. 全链路终局（67.89% → 70.23%）

| 阶段 | 方向准确率 | 增量 |
| --- | ---: | --- |
| R7 基准 | 67.89% | — |
| Optuna 调优 HP | 68.33% | +0.44 |
| f_range_pos_20（v2） | 68.67% | +0.34 |
| SPX 外盘组（v3） | 69.06% | +0.39 |
| UUP 外盘组（v4） | **70.23%** | **+1.17** |
| 之后：外盘扩展/裁剪/周线/Isotonic/集成 | 全负贡献 | 实证封顶 |

**冠军 v4 = LightGBM(调优 HP) + 25 特征（含 spx/uup 外盘组）+ Platt 校准 = 70.23% / RankIC 0.4510，为最终交付配置。**
