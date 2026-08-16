# 真实数据 OOS 信号验证 + R7 裁决报告

> 报告日期：**2026-08-15**  
> 选用数据源：**sina**  （可达性：pytdx 公共服务器在本沙箱 TCP 超时 -> 自动降级 sina 成功）

## 1. 数据来源与可达性

- 供应链优先级（free.yaml）：`['pytdx', 'sina', 'akshare_fundamentals']`
- 最终选用源：**sina**
- 品种：`['SHFE.cu', 'SHFE.rb', 'INE.sc']`（freq=1d，horizon=5）
- 区间：2018-01-01 ~ 2024-12-31
- 实际返回品种：['cu0', 'rb0', 'sc0']；总 bar 数：4718

## 2. 各模型 OOS 训练概览

| 模型 | model_id | 折数(n_folds) | OOS 信号数 |
| --- | --- | ---: | ---: |
| lightgbm | c3fcfadee269 | reused | 2015 |
| ar_transformer | 8342f2e8de40 | reused | 2015 |
| tcn | 6cb4300cafd0 | reused | 2015 |
| gru | b843efeff26f | reused | 2015 |

## 3. 闸门1指标（信号有效性 / viability floor）

判据：方向准确率 ≥ 54% 且 有效信号准确率 ≥ 58% 且 coverage ≥ 30%

| 模型 | n_oos | n_eff | coverage | 方向准确率 | 有效准确率 | RankIC | IC | 闸门1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| lightgbm | 2015 | 1586 | 78.71% | 65.81% | 69.55% | 0.3703 | 0.0232 | **PASS** |
| ar_transformer | 2015 | 1595 | 79.16% | 65.46% | 69.09% | 0.3542 | 0.0330 | **PASS** |
| tcn | 2015 | 1547 | 76.77% | 66.00% | 70.07% | 0.3829 | 0.0388 | **PASS** |
| gru | 2015 | 1648 | 81.79% | 68.29% | 71.72% | 0.4305 | -0.0457 | **PASS** |

### 3.1 分品种明细

**lightgbm**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cu0 | 682 | 74.19% | 65.54% | 70.75% | 0.4015 | 0.0636 |
| rb0 | 682 | 82.55% | 65.84% | 69.09% | 0.3842 | -0.0431 |
| sc0 | 651 | 79.42% | 66.05% | 68.86% | 0.3237 | 0.0428 |

**ar_transformer**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cu0 | 682 | 72.87% | 64.66% | 69.22% | 0.3658 | 0.1562 |
| rb0 | 682 | 84.16% | 66.86% | 69.51% | 0.3979 | -0.1485 |
| sc0 | 651 | 80.49% | 64.82% | 68.51% | 0.2927 | 0.0489 |

**tcn**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cu0 | 682 | 66.42% | 64.66% | 71.30% | 0.3756 | 0.0455 |
| rb0 | 682 | 81.96% | 66.57% | 69.05% | 0.4182 | -0.0124 |
| sc0 | 651 | 82.18% | 66.82% | 70.09% | 0.3472 | 0.0613 |

**gru**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cu0 | 682 | 82.11% | 68.48% | 71.96% | 0.4566 | -0.0591 |
| rb0 | 682 | 81.67% | 68.33% | 71.10% | 0.4391 | 0.0094 |
| sc0 | 651 | 81.57% | 68.05% | 72.13% | 0.3862 | -0.0824 |

## 4. R7 裁决（自回归家族 vs LightGBM 主位之争）

- 自回归家族代理：**ar_transformer**（Kronos 架构替身）
- 基线对照：**lightgbm**
- 裁决逻辑：`autoregressive_wins = (ar.方向准确率 ≥ lg.方向准确率) AND (ar.RankIC ≥ lg.RankIC)` = **False**
- **R7 结论：R7 主位让予 LightGBM(条件性)**
- 自回归家族是否过闸门1：是

## 5. Kronos 阻塞说明（必须如实标注）

- **kronos_status：`BLOCKED`**
- 原因：无 third_party/Kronos submodule、无 transformers、无权重；KronosAdapter 仅能降级 ARTransformer，故真 Kronos vs LightGBM 终裁 pending Kronos 权重就绪
- 真 Kronos vs LightGBM 终裁 pending Kronos 权重就绪

## 6. 环境备注

- Python 受管解释器 3.13；venv default。
- pytdx 公共行情服务器在本沙箱 TCP 超时，脚本按 free.yaml 优先级自动降级 sina 并成功跑完。
- sina 返回真实 cu/rb/sc 主力连续日线，数据截止 2024（区间按 2018-01-01~2024-12-31 过滤）。
- scipy 可用=是（RankIC/IC 用 scipy.stats 计算）。
- Kronos 权重/分词器/transformers 缺失，未运行 kronos；ar_transformer 作为其架构替身。
- 预测层推理缓存泄漏已修复（GRU/AR/TCN 的 forward 缓存现于 predict 后统一清理，见 ForecastModel._clear_inference_caches），使 gru 在本沙箱可完整跑完而不撑爆内存。
