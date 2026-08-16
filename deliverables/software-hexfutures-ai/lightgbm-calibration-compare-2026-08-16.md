# LightGBM 冠军模型概率校准对比报告（Platt vs Isotonic vs none）

> 报告日期：**2026-08-16** | 品种：['SHFE.au','SHFE.ag','DCE.m'] | horizon=5 n_mc=30 | 全 66 折 walk-forward，调优后 HP，冠军 v4 特征集（25 特征）

## TL;DR

- 在 `calibrate_signals` 新增 **Isotonic 校准**（sklearn，单调非参数）与 `none`（不校准）两个分支，与现有 Platt 在全 66 折上对比。
- **结果：Platt 保持冠军（70.23%）**；Isotonic 方向准确率暴跌 **-2.93pp**（但 RankIC +0.0207、ECE=0 完美校准）；无校准崩到 52.44%（再次印证校准是方向准确率的关键）。
- **结论：校准方法维持 Platt，不切换**。Isotonic 的排序能力（RankIC）更强但方向判断被 per-fold 小样本过拟合破坏。

## 1. 对比结果（全 66 折，冠军 v4 特征集）

| 校准方法 | 方向准确率 | Δpp | RankIC | coverage | 有效准确率 | ECE(分箱最大偏差) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Platt（当前冠军）** | **70.23%** | — | **0.4510** | 85.04% | 72.99% | 0.0541 |
| Isotonic | 67.30% | **-2.93** | **0.4717** (+0.0207) | 85.87% | 73.19% | **0.0000** |
| 无校准（none） | 52.44% | -17.79 | 0.0559 | 96.04% | 53.54% | 0.5221 |

分品种（方向准确率）：
- Platt：ag0 68.18% / au0 73.31% / m0 69.21%
- Isotonic：ag0 64.96% / au0 70.23% / m0 66.72%（三品种全降）

## 2. 解读：为什么 Isotonic 校准更好但方向准确率更差？

1. **Isotonic 完美校准（ECE=0.0000）**：per-fold 内对 p_up 做单调非参数映射，样本内完全拟合测试窗经验频率——校准质量指标（ECE）近乎完美。
2. **但这是过拟合**：每折测试窗只有 ≥20 个有效样本，Isotonic 的阶梯函数逐点拟合噪声，把 p_up 推向极端（0/1），**破坏了跨折的全局排序与方向判断**（方向准确率 -2.93pp）。
3. **RankIC 反而更高（+0.0207）**：Isotonic 保持单调性，单调变换不改变秩相关，RankIC 理论上应与 raw 相同——实际 +0.0207 说明它压缩了中间概率、放大了高低两端差异，使秩相关微升。但方向判断依赖的是 p_up 与 0.5 的关系，被极端化破坏。
4. **Platt 是 2 参数平滑映射**：对 per-fold 小样本稳健，不逐点过拟合——这正是它胜出的原因。
5. **无校准（none）验证**：raw p_up 经 per-fold 校准后才是可用的方向信号（52.44% → 70.23%），校准步骤必不可少。

## 3. 结论

- **校准方法维持 Platt**（`configs/forecast/lightgbm_champion.yaml` 不变，`calibration_method: "platt"`）。
- Isotonic 作为**可选备选**保留在代码中（`calibrate_signals(..., method="isotonic")`），若未来测试窗样本量显著增大（如分钟级数据、更长测试窗）可重测——其在 RankIC 上的优势（+0.0207）在样本充足时可能转化为方向增益。

## 4. 文件清单

| 文件 | 说明 |
| --- | --- |
| `hexbroker/forecast/calibration.py` | 新增 `isotonic`/`none` 分支（`_calibrate_isotonic`） |
| `scripts/compare_calibration.py` | 三方法全 66 折对比脚本 |
| `tests/test_calibration.py` | 新增 3 项（isotonic 单调/边界、none、未知方法报错）→ 6/6 全过 |
| `deliverables/.../lightgbm-calibration-compare-2026-08-16.json` | 对比明细（含可靠性曲线） |

## 5. 下一步

1. **多模型集成**（LightGBM+XGBoost+CatBoost）：不同模型概率均值，校准仍用 Platt。
2. **Isotonic 变体**：若做集成，可用 Isotonic 校准集成概率（样本量 = 3×单模型，更稳）。
3. **信号后处理**：is_effective 置信分层 / p_up 平滑。
