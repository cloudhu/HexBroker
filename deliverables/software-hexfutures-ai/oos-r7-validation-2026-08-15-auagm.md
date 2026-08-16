# 真实数据 OOS 信号验证 + R7 终裁报告（au/ag/m，真 Kronos）

> 报告日期：**2026-08-15**  
> 选用数据源：**sina**  （可达性：pytdx 公共服务器在本沙箱 TCP 超时 -> 自动降级 sina 成功）

## 1. 数据来源与可达性

- 供应链优先级（free.yaml）：`['pytdx', 'sina', 'akshare_fundamentals']`
- 最终选用源：**sina**
- 品种：`['SHFE.au', 'SHFE.ag', 'DCE.m']`（freq=1d，horizon=5）
- 区间：2018-01-01 ~ 2024-12-31
- 实际返回品种：['ag0', 'au0', 'm0']；总 bar 数：4772

## 2. 各模型 OOS 运行概览

| 模型 | model_id | 折数(n_folds) | OOS 信号数 |
| --- | --- | ---: | ---: |
| lightgbm | c3fcfadee269 | reused | 2046 |
| ar_transformer | 8342f2e8de40 | reused | 2046 |
| tcn | 6cb4300cafd0 | reused | 2046 |
| gru | b843efeff26f | reused | 2046 |
| kronos | kronos | 66 | 3960 |

## 3. 闸门1指标（信号有效性 / viability floor）

判据：方向准确率 ≥ 54% 且 有效信号准确率 ≥ 58% 且 coverage ≥ 30%

| 模型 | n_oos | n_eff | coverage | 方向准确率 | 有效准确率 | RankIC | IC | 闸门1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| lightgbm | 2046 | 1731 | 84.60% | 67.89% | 70.65% | 0.4092 | 0.0297 | **PASS** |
| ar_transformer | 2046 | 1649 | 80.60% | 66.86% | 70.10% | 0.4022 | -0.0078 | **PASS** |
| tcn | 2046 | 1561 | 76.30% | 67.64% | 71.88% | 0.4161 | 0.0036 | **PASS** |
| gru | 2046 | 1599 | 78.15% | 67.01% | 71.42% | 0.4249 | -0.0096 | **PASS** |
| kronos | 3960 | 3447 | 87.05% | 45.76% | 52.51% | 0.0415 | 0.0558 | **FAIL** |

### 3.1 分品种明细

**lightgbm**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ag0 | 682 | 84.31% | 66.13% | 68.70% | 0.3846 | 0.0327 |
| au0 | 682 | 82.26% | 68.91% | 72.37% | 0.3870 | -0.1226 |
| m0 | 682 | 87.24% | 68.62% | 70.92% | 0.4806 | 0.0720 |

**ar_transformer**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ag0 | 682 | 83.87% | 65.25% | 67.48% | 0.3647 | 0.0672 |
| au0 | 682 | 73.90% | 67.30% | 71.83% | 0.3826 | -0.0034 |
| m0 | 682 | 84.02% | 68.04% | 71.20% | 0.4650 | -0.1020 |

**tcn**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ag0 | 682 | 76.83% | 67.89% | 70.99% | 0.4261 | 0.0122 |
| au0 | 682 | 78.30% | 67.89% | 71.91% | 0.4001 | -0.0123 |
| m0 | 682 | 73.75% | 67.16% | 72.76% | 0.4342 | -0.0048 |

**gru**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ag0 | 682 | 74.93% | 65.69% | 71.23% | 0.4327 | 0.0016 |
| au0 | 682 | 79.77% | 66.86% | 70.59% | 0.3709 | 0.0285 |
| m0 | 682 | 79.77% | 68.48% | 72.43% | 0.4707 | -0.0417 |

**kronos**

| 品种 | n_oos | coverage | 方向准确率 | 有效准确率 | RankIC | IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ag0 | 1320 | 88.48% | 46.14% | 52.14% | 0.0331 | 0.0565 |
| au0 | 1320 | 85.38% | 45.38% | 53.15% | 0.0658 | 0.0846 |
| m0 | 1320 | 87.27% | 45.76% | 52.26% | 0.0329 | 0.0495 |

## 4. R7 终裁（真 Kronos vs LightGBM 主位之争）

- 挑战者：**kronos**（真 Kronos，非代理）
- 基线对照：**lightgbm**
- 裁决逻辑：`kronos_wins = (kronos.方向准确率 ≥ lg.方向准确率) AND (kronos.RankIC ≥ lg.RankIC)` = **False**
- **R7 结论：R7 主位让予 LightGBM(真 Kronos 数据裁决)**
- 真 Kronos 是否过闸门1：否

## 5. Kronos 状态

- **kronos_status：`RUN`**
- 原因：真 Kronos 成功产出 OOS 信号（NeoQuasar/Kronos-small，n_mc=10）
- 模型：NeoQuasar/Kronos-small / NeoQuasar/Kronos-Tokenizer-base（n_mc=10）
- 真 Kronos vs LightGBM 数据裁决（非代理）

## 6. 环境备注

- Python 受管解释器 3.13；venv default。
- pytdx 公共行情服务器在本沙箱 TCP 超时，脚本按 free.yaml 优先级自动降级 sina 并成功跑完。
- sina 返回真实 au0/ag0/m0 主力连续日线，数据截止 2024（区间按 2018-01-01~2024-12-31 过滤）。
- 真 Kronos 部署：third_party/Kronos（shiyu-coder/Kronos master）+ NeoQuasar/Kronos-small 本地 HF 缓存 + Kronos-Tokenizer-base（blob 经 hf-mirror 补齐，sha256 校验通过）。
- Kronos 推理设备：GPU cuda:0（torch 2.11.0+cu128，CUDA 可用）；单次 predict_batch(32)≈0.22s。
- Kronos 路径分布：sample_count=1 循环 n_mc 次收集独立路径（KronosPredictor 内部 sample_count>1 为平均）。
- kronos n_mc=10（KRONOS_N_MC 环境变量可调）；若样本规模过大可在报告注明并按需降 n_mc。
- scipy 可用=是（RankIC/IC 用 scipy.stats 计算）。
- 预测层推理缓存泄漏已修复（GRU/AR/TCN 的 forward 缓存现于 predict 后统一清理）。
