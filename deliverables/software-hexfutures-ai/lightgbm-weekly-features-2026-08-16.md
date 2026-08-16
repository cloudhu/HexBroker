# LightGBM 冠军模型周线多尺度特征报告

> 报告日期：**2026-08-16** | 品种：['SHFE.au','SHFE.ag','DCE.m'] | horizon=5 n_mc=30 | 全 66 折 walk-forward，调优后 HP
> 消融基线 = 冠军 v4（25 特征，spx+uup 外盘组），周线特征严格因果

## TL;DR

- 新增 `hexbroker/feature/weekly.py`：4 个周线多尺度特征（周内累计收益/周内位置/周内波动率/上周收益），**全部严格因果**（只用截至 t 的当日及历史信息，含因果性单测）。
- 消融（冠军 v4 基线，全 66 折）：**4 个单特征 + 组合全部负贡献**（-0.49 ~ -1.61pp），不采纳。
- **结论：冠军 v4（25 特征，70.23%）维持**；周线多尺度特征与现有滚动窗口特征（f_ret_acc_5/f_vol_20 等）高度冗余。
- 特征工程路线**正式封顶**：外盘扩展（ixic/dji/tlt/ief/t10y）、特征裁剪（top-10/15/20）、周线多尺度（4 特征）三轮全负贡献——该特征集在 LightGBM 下已达性能上界。

## 1. 周线特征设计（严格因果）

| 特征 | 含义 | 因果性论证 |
| --- | --- | --- |
| f_week_ret_acc | 周内累计收益 close_t / 本周首日 open - 1 | 本周 open 与 close_t 均为 ≤t 信息 ✅ |
| f_week_pos | 周内位置（本周已过交易日数 / 5） | 已过天数由 t 决定 ✅ |
| f_week_vol | 周内已实现波动率（本周 expanding std） | 窗口只含 ≤t 收益 ✅ |
| f_week_prev_ret | 上一完整周收益（上周五 close / 周一 open - 1） | 上周已结束，shift 一周 ✅ |

单测：`tests/test_weekly_features.py` 6/6（含篡改未来无泄漏验证 + 上周收益手工对拍）。

## 2. 消融结果（冠军 v4 基线，全 66 折）

| 变体 | 方向准确率 | Δpp | RankIC | coverage | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: |
| **冠军 v4（25 特征）** | **70.23%** | — | **0.4510** | 85.04% | 基准 |
| +f_week_vol（周内波动率） | 69.75% | -0.49 | 0.4570 (+0.006) | 81.82% | ❌ |
| +f_week_pos（周内位置） | 69.70% | -0.54 | 0.4563 (+0.005) | 83.77% | ❌ |
| +f_week_ret_acc（周内累计收益） | 69.26% | -0.98 | 0.4507 | 81.13% | ❌ |
| +f_week_prev_ret（上周收益） | 68.62% | -1.61 | 0.4311 | 82.84% | ❌ 最差 |
| +4 特征组合 | 69.11% | -1.12 | 0.4433 | 82.11% | ❌ |

## 3. 为什么周线特征负贡献？

1. **信息冗余**：f_week_ret_acc ≈ f_ret_acc_5（同为 5 日左右动量）、f_week_vol ≈ f_vol_5/f_vol_20（同为波动率）、f_week_prev_ret ≈ f_ret_acc_5 滞后 5 日——现有滚动窗口特征已隐含周级信息，周线重采样不提供正交信息。
2. **周内位置（f_week_pos）RankIC 微升 +0.005 但方向准确率降**：周内节奏对概率排序有微弱正信息，但对方向判断造成干扰（与 t10y 同模式）——5 日 horizon 下"周几效应"信噪比不足。
3. **树模型对重采样冗余敏感**：LightGBM 用原始日线滚动特征（min_periods=2/5）比周线聚合保留更多信息粒度，周线是信息压缩（有损）。

## 4. 结论

- **冠军特征集维持 v4（25 特征，70.23% / RankIC 0.4510）**。
- **特征工程全路线封顶证据链**（全部全 66 折实证）：
  - 外盘扩展：ixic -1.17pp / dji -1.66pp / tlt -1.08pp / ief -1.61pp / t10y -1.08pp ❌
  - 特征裁剪：top-10 -1.86pp / top-15 -0.49pp / top-20 -0.93pp ❌
  - 周线多尺度：-0.49 ~ -1.61pp ❌
- 新增资产：`hexbroker/feature/weekly.py`（可复用的周线特征层，默认不启用）。

## 5. 文件清单

| 文件 | 说明 |
| --- | --- |
| `hexbroker/feature/weekly.py` | 周线多尺度特征（严格因果，include 白名单） |
| `hexbroker/feature/pipeline.py` / `config.py` | `weekly` transformer + `weekly_params` |
| `scripts/ablate_features.py` | 新增 `--weekly-only`、WEEKLY_CANDIDATES |
| `tests/test_weekly_features.py` | 6 单测（因果 + 对拍 + include + 稳定） |
| `deliverables/.../lightgbm-weekly-ablation-2026-08-16.md` | 消融明细 |

## 6. 下一步（模型侧优先）

1. **多模型集成**：LightGBM + XGBoost + CatBoost 概率均值（特征面已封顶，集成是最后一块能提分的板）。
2. **概率校准**：Platt 已用，可试 Isotonic + 分形校准对比。
3. **信号后处理**：基于 is_effective 的置信分层 / p_up 平滑。
