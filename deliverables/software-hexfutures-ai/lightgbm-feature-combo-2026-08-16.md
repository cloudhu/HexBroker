# LightGBM 冠军模型特征工程迭代（单特征消融）

> 报告日期：**2026-08-16** | 数据源：sina | 品种：['SHFE.au', 'SHFE.ag', 'DCE.m'] | horizon=5 n_mc=30

## 1. 消融结果（全 66 折，调优后 HP）

| 变体 | 特征数 | 方向准确率 | Δpp | 有效准确率 | coverage | RankIC | ΔRankIC | 信号数 | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| base（18 特征） | 18 | 68.33% | — | 71.48% | 84.31% | 0.4338 | — | 2046 | 基准 |
| +f_range_pos_20 | — | 68.67% | +0.34 | 72.08% | 84.02% | 0.4452 | +0.0114 | 2046 | ✅ PASS |
| +f_range_pos_20+f_kurt_20 | — | 68.43% | +0.10 | 71.70% | 84.80% | 0.4399 | +0.0061 | 2046 | ❌ 未过线 |

## 2. 通过线

- RankIC 提升 ≥ 0.01 且方向准确率不降；或方向准确率提升 ≥ 0.3pp（coverage 不低于 80%）。

## 3. 调优后 HP

```yaml
lgbm_n_estimators: 200.0
lgbm_lr: 0.016682542111075827
lgbm_max_depth: 11.0
lgbm_num_leaves: 57.0
lgbm_min_child_samples: 22.0
lgbm_subsample: 0.6827653754476659
lgbm_colsample_bytree: 0.9671600243145742
lgbm_reg_lambda: 0.04164800885264854
lgbm_reg_alpha: 0.002421420203310671
```
