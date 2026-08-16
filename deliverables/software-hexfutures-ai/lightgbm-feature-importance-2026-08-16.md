# LightGBM 冠军模型特征重要性

> 报告日期：**2026-08-16** | 数据源：sina | 品种：['SHFE.au', 'SHFE.ag', 'DCE.m'] | horizon=5 n_mc=30

## 基准 gate1（全 66 折 walk-forward）

- 方向准确率：**67.89%**
- 有效准确率：70.65%
- coverage：84.60%
- RankIC：0.4092

## 特征重要性（按特征聚合，降序）

- 收集折数：66 | 特征数：18 | lookback：30

| 排名 | 特征 | 重要性占比 |
| ---: | --- | ---: |
| 1 | `f_vol_5` |  8.73% |
| 2 | `f_vol_ratio` |  7.67% |
| 3 | `f_bar_dir` |  7.50% |
| 4 | `f_boll_width` |  7.20% |
| 5 | `f_vol_20` |  6.31% |
| 6 | `f_gap` |  6.08% |
| 7 | `f_up_vol_share` |  5.73% |
| 8 | `f_lower_shadow` |  5.61% |
| 9 | `f_upper_shadow` |  5.56% |
| 10 | `f_ret_acc_5` |  5.49% |
| 11 | `f_body_ratio` |  5.17% |
| 12 | `f_rsi` |  4.73% |
| 13 | `f_intraday_range` |  4.68% |
| 14 | `f_ret_1` |  4.51% |
| 15 | `f_macd_hist` |  4.41% |
| 16 | `f_macd` |  4.06% |
| 17 | `f_ret_acc_20` |  3.51% |
| 18 | `f_ma_spread` |  3.06% |

### Top-10 (特征, 滞后) 组合

| 特征 | 滞后(k=0 最近) | 重要性占比 |
| --- | ---: | ---: |
| `f_ret_1` | 0 |  0.97% |
| `f_ret_acc_5` | 0 |  0.66% |
| `f_boll_width` | 0 |  0.57% |
| `f_bar_dir` | 28 |  0.52% |
| `f_vol_5` | 0 |  0.51% |
| `f_bar_dir` | 29 |  0.50% |
| `f_boll_width` | 29 |  0.48% |
| `f_macd` | 29 |  0.44% |
| `f_vol_5` | 20 |  0.43% |
| `f_vol_ratio` | 24 |  0.43% |

> 注：重要性来自每折 ``_mean_model.feature_importances_`` 的均值模型；扁平化维度按 build_windows 行优先还原为 (特征, 滞后)。
