# LightGBM 冠军模型特征工程迭代报告

> 报告日期：**2026-08-16** | 数据源：sina | 品种：['SHFE.au', 'SHFE.ag', 'DCE.m'] | horizon=5 n_mc=30
> 全 66 折 walk-forward，调优后 HP（`configs/forecast/lightgbm_champion.yaml`）

## TL;DR

- 新增 `hexbroker/feature/iterative.py`（8 个候选特征，全部严格因果），pipeline 支持 `iterative` transformer 与 `iterative_params.include` 白名单（可插拔消融）。
- 单特征消融（全 66 折，每特征独立加在 base 18 特征上）：**仅 `f_range_pos_20` 通过线**（方向准确率 +0.34pp，RankIC +0.0114，双指标达标）。
- 组合验证：`f_range_pos_20 + f_kurt_20` 不叠加（68.43% < 单特征 68.67%），**最终采纳单特征 `f_range_pos_20`**。
- 冠军特征集 v2 = base 18 特征 + `f_range_pos_20`（19 特征）：**方向准确率 68.33% → 68.67%**，RankIC **0.4338 → 0.4452**。
- 固化：`configs/feature/champion_v2.yaml` + `configs/experiment/e03_auagm_lightgbm_v2.yaml`。

## 1. 单特征消融结果（全 66 折）

| 变体 | 方向准确率 | Δpp | 有效准确率 | coverage | RankIC | ΔRankIC | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| base（18 特征） | 68.33% | — | 71.48% | 84.31% | 0.4338 | — | 基准 |
| +f_range_pos_20（20日区间位置） | **68.67%** | **+0.34** | 72.08% | 84.02% | **0.4452** | **+0.0114** | ✅ PASS |
| +f_kurt_20（收益峰度） | 68.67% | +0.34 | 71.82% | 84.80% | 0.4309 | -0.0029 | ✅ PASS |
| +f_skew_20（收益偏度） | 68.57% | +0.24 | 71.85% | 84.21% | 0.4354 | +0.0016 | ❌ 差 0.06pp |
| +f_ret_vol_corr_20（量价相关） | 68.52% | +0.20 | 72.52% | 80.94% | 0.4352 | +0.0014 | ❌ coverage 掉 3.4pp |
| +f_intraday_ret（日内收益） | 68.23% | -0.10 | 71.63% | 83.72% | 0.4287 | -0.0051 | ❌ |
| +f_streak_dir（连续同向天数） | 68.08% | -0.24 | 71.27% | 85.39% | 0.4285 | -0.0053 | ❌ |
| +f_autocorr_20（收益自相关） | 67.99% | -0.34 | 72.16% | 81.28% | 0.4253 | -0.0085 | ❌ |
| +f_vol_ratio_5_20（波动率结构比） | 67.69% | -0.64 | 70.94% | 84.26% | 0.4171 | -0.0167 | ❌ 最差 |

> 通过线：RankIC ≥ +0.01（且方向准确率不降）**或** 方向准确率 ≥ +0.3pp（且 coverage ≥ 80%）。
> 注：初版脚本通过线单位 bug（0.003 误为比率而非 pp）导致 f_skew_20/f_ret_vol_corr_20 误判 PASS，已修正重判。

## 2. 组合验证

| 变体 | 方向准确率 | Δpp | RankIC | ΔRankIC | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| base | 68.33% | — | 0.4338 | — | — |
| +f_range_pos_20 | 68.67% | +0.34 | 0.4452 | +0.0114 | 最优 |
| +f_range_pos_20+f_kurt_20 | 68.43% | +0.10 | 0.4399 | +0.0061 | 不叠加，放弃 |

**结论**：`f_kurt_20` 与 `f_range_pos_20` 信息重叠（都编码波动/区间结构），组合反而稀释增益。最终只采纳 `f_range_pos_20`。

## 3. 分品种（base + f_range_pos_20）

| 品种 | 方向准确率 | 有效准确率 | coverage | RankIC |
| --- | ---: | ---: | ---: | ---: |
| ag0 | 66.57% | 71.04%→72.08%(整体) | 80.50% | 0.4278 |
| au0 | 71.26% | — | 84.75% | 0.4444 |
| m0 | 68.18% | — | 87.68% | 0.4580 |

（au0/m0 方向准确率分别 66.57→71.26、67.45→68.18；增益主要在 au0/m0，ag0 持平）

## 4. 冠军特征集 v2（19 特征）

base 18 + `f_range_pos_20`（20 日高低区间内收盘位置，0~1）：

```
f_range_pos_20 = (close - low_20) / (high_20 - low_20)
```

实现：`hexbroker/feature/iterative.py`，严格因果（rolling min/max 窗口含当前 bar），除零保护，NaN 填充 0.5（中性）。

## 5. 文件清单

| 文件 | 说明 |
| --- | --- |
| `hexbroker/feature/iterative.py` | 新增：8 个候选特征 + include 白名单 |
| `hexbroker/feature/pipeline.py` | 新增 `iterative` transformer 支持（默认不启用，行为不变） |
| `hexbroker/config.py` | `FeatureConfig` 新增 `iterative_params` 字段 |
| `configs/feature/champion_v2.yaml` | 冠军特征集 v2（启用 iterative + f_range_pos_20） |
| `configs/experiment/e03_auagm_lightgbm_v2.yaml` | v2 实验入口（平铺 feature 段） |
| `scripts/ablate_features.py` | 消融脚本（`--n-jobs N` / `--limit-feature` / `--combine`） |
| `tests/test_iterative_features.py` | 6 单测（含严格因果无未来泄漏测试） |

## 6. 结论与下一步

- **本迭代净收益**：方向准确率 +0.34pp（68.33→68.67%），RankIC +0.0114（0.4338→0.4452），coverage 基本持平。
- **经验**：① 波动率/区间类特征已近饱和——f_vol_5/f_vol_ratio/f_boll_width 占特征重要性前 5，再加分布形态类（skew/kurt）或波动率比（vol_ratio_5_20）边际递减甚至负贡献；② 新增特征与 top 特征重叠时组合不叠加；③ 量价相关类（ret_vol_corr）提升 coverage 能力但代价是覆盖率下降，需权衡。
- **下一步候选**（按预期信息增量排序）：
  1. **跨品种套利价差**（金银比 au/ag、贵金属-豆粕相对强弱）——期货特有、与现有单品种特征正交，需 pipeline 支持跨品种上下文；
  2. **基差/展期结构**（若数据可得）——期限结构斜率是商品期货独有 alpha；
  3. **波动率锥 z-score**（vol_20 相对其自身 60 日分布分位）——与已饱和的绝对波动率区分；
  4. 特征选择（对 540 维做按重要性裁剪，可能小幅提升泛化）。
