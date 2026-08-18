# LightGBM 冠军模型精进 — 进度概览（2026-08-16）

> 📄 **最终研究终报**：`deliverables/software-hexfutures-ai/lightgbm-champion-final-report-2026-08-16.md`
> 完整历程（根因修复 → 调优 → 特征工程两轮 → 外盘因子 → 校准/集成探索，20+ 候选全 66 折实证）已整合为一份终报。

## 已完成

### 1. 根因定位 & 修复（关键突破）
- **现象**：自写 `scripts/refine_lightgbm_champion.py::walk_forward_lightgbm` 长期只复现 ~52.20% 方向准确率（RankIC 0.025，近乎随机），而官方 `ForecastTrainer` 复现 R7 的 67.89% / RankIC 0.4092。
- **诊断链**：三品种 raw `p_up` 与官方 Trainer 逐信号 **byte 相同**（2046 信号 0 差异）→ 训练/预测逻辑正确；差异来自官方 Trainer 在**每折测试窗做 per-fold Platt 校准**（各折 (a,b) 不同 → 改写跨折全局排序与方向准确率），自写版本漏掉该步骤。原注释"校准不影响方向准确率"为**错误结论**，已更正。
- **修复与验证**：补 `calibrate_signals` 校准块后，`walk_forward_lightgbm` 与官方 Trainer **完全一致**（dir_acc=0.6789 / rank_ic=0.4092 / cov=0.8460）。

### 2. 特征重要性分析（R7 口径，全 66 折）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-feature-importance-2026-08-16.md`
- Top5：`f_vol_5`(8.7%) > `f_vol_ratio`(7.7%) > `f_bar_dir`(7.5%) > `f_boll_width`(7.2%) > `f_vol_20`(6.3%) —— 量价/波动率类主导。

### 3. Optuna 超参调优（24 核 CPU 并行）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-refinement-2026-08-16.md`
- 基准 67.89% → **68.33%**（+0.44pp），RankIC 0.4092 → **0.4338**；最优 HP 已写回 `configs/forecast/lightgbm_champion.yaml`。
- 工程：`walk_forward_lightgbm` 折级并行（`ProcessPoolExecutor`，`--n-jobs` 默认 24），并行结果与官方逐字节一致；`_write_champion_config` 改原子写。

### 4. 特征工程迭代（新增 8 候选 → 采纳 1）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-feature-engineering-2026-08-16.md`（消融明细 `lightgbm-feature-ablation-2026-08-16.md`）
- 新增 `hexbroker/feature/iterative.py`（8 特征，严格因果 + include 白名单），pipeline 可插拔 `iterative` transformer。
- 单特征消融（全 66 折）：**仅 `f_range_pos_20` 过线**（方向准确率 +0.34pp、RankIC +0.0114）；组合 `+f_kurt_20` 不叠加。
- 冠军特征集 v2（19 特征）：**方向准确率 68.33% → 68.67%**，RankIC 0.4338 → **0.4452**。固化 `configs/feature/champion_v2.yaml` + `configs/experiment/e03_auagm_lightgbm_v2.yaml`。
- 单测 6/6（含严格因果无未来泄漏测试）；feature/config 回归 18/18 全过。

### 5. 跨品种特征迭代（内盘负贡献，冠军 v2 冻结）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-cross-features-2026-08-16.md`（消融明细 `lightgbm-cross-ablation-2026-08-16.md`）
- 新增 `hexbroker/feature/cross.py`：内盘两两比值（金银比/金豆比/银豆比）+ 外盘参照比值（SPX/UDI/WTI，时差安全 shift(1)+asof）。
- 消融（冠军 v2 基线，全 66 折）：**11 个候选全部未过线**；金银比最差（-1.03pp / RankIC -0.021）。**冠军 v2（19 特征）冻结为特征集终态**。
- 外盘数据源勘察：东财 IP 级限流未恢复、腾讯仅 800 根（覆盖不了回测窗）、雅虎/FRED/stooq 不可达。外盘特征代码就绪，数据源恢复后补测。
- 单测 9/9（因果 + 对称 + include + 稳定）。

### 6. 外盘特征迭代（腾讯自选股 MCP 打通，冠军 v3 确立）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-global-features-2026-08-16.md`
- **外盘数据源打通**：腾讯自选股 MCP `data_kline` 可分段拉全历史——usINX（标普500）/usCL（WTI）2017-2024 全覆盖；fxDINIW（美元指数）end 参数不生效（仅 2023-12 起）。落盘 `data/raw/global/{spx,wti}.parquet`（1909 根，关键点位与真实值一致）。
- **SPX 组过线**：方向准确率 68.67% → **69.06%（+0.39pp）**；细粒度拆解（ratio/mom/vol 单独均不达标）确认是整组交互增益。WTI 组与内盘比值不采纳。
- **冠军 v3（22 特征）**：`configs/feature/champion_v3.yaml` + `configs/experiment/e04_auagm_lightgbm_v3.yaml`。
- ⚠️ 事故与教训：残留后台调优进程覆盖了冠军配置（HP 变成 250/0.0535/5/19），已恢复为 200/0.0167/11/57 并重跑确认；多后台任务竞争写同一配置是危险的，Optuna 写回前须校验。

### 7. 外盘参照扩展（冠军 v4 确立，70.23%）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-global-ext-2026-08-16.md`
- **新增 4 个外盘参照**（腾讯自选股 MCP，2017-2024 全覆盖）：纳指（ixic）、道指（dji）、**UUP 做多美元 ETF（美元指数代理）**、**TLT 美债 ETF（10Y 收益率代理）**。
- **美元指数数据源解决**：fxDINIW end 参数不生效（仅 2023-12 起），改用 UUP（us 前缀、可拉全历史、与 DXY 高相关）作代理。
- **UUP 组强过线**：方向准确率 **69.06% → 70.23%（+1.17pp）**、RankIC 0.4390 → **0.4510**、coverage 80.79% → **85.04%**——三指标全升（美元走弱→贵金属走强的直接传导链）。tlt/ixic/dji 负贡献不采纳。
- **冠军 v4（25 特征）**：`configs/feature/champion_v4.yaml` + `configs/experiment/e05_auagm_lightgbm_v4.yaml`。

### 8. 真实 10Y 收益率消融（宏观因子面定稿，冠军 v4 维持）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-t10y-ablation-2026-08-16.md`
- **FRED DGS10 打通**（真实 10Y 收益率，1962 年起，带重试可稳定获取）：`data/raw/global/t10y.parquet`，关键点位验证 ✅（2018=2.46% / 2020 疫情底=0.76% / 2024=4.58%）。
- **消融结论**：t10y -1.08pp ❌、ief（7-10 年 ETF）-1.61pp ❌——**利率维度三轮验证（tlt/ief/t10y）均无增量**。t10y 的 RankIC 微升（+0.0056）但方向准确率降：利率传导（实际利率 vs 通胀预期两维交织）在 5 日 horizon 信噪比不足。
- **宏观因子面定稿**：风险偏好（spx）+ 美元（uup）双正交维度是最优组合；**冠军 v4（70.23%）维持不动**。

### 9. 特征选择裁剪（特征路线收官，冠军 v4 维持）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-feature-selection-2026-08-16.md`
- **重要性排名**（全 66 折）：UUP+SPX 六外盘特征全部 top-10（合计 36.3%）；量价类（vol_5/vol_20/boll_width）次之。
- **裁剪消融**：top-10(-1.86pp)/top-15(-0.49pp)/top-20(-0.93pp) **全部降性能**，不采纳——LightGBM 对弱特征免疫（树分裂加权+已调优正则化），尾部特征提供边际交互信息，外部裁剪是负和博弈。
- **特征工程路线正式收官**：67.89% → **70.23%**（冠军 v4，25 特征）；此后外盘扩展与裁剪全部负贡献，该特征集在 LightGBM 下已达性能上界。
- 新增基础设施：`keep_features` 特征级白名单（pipeline + FeatureConfig）。

### 10. 周线多尺度特征（特征路线封顶，冠军 v4 维持）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-weekly-features-2026-08-16.md`
- 新增 `hexbroker/feature/weekly.py`：4 个严格因果周线特征（周内累计收益/周内位置/周内波动率/上周收益），单测 6/6（含因果验证）。
- **消融**（冠军 v4 基线，全 66 折）：4 单特征 + 组合**全部负贡献**（-0.49~-1.61pp）——与 f_ret_acc_5/f_vol_20 等滚动窗口特征高度冗余，周线聚合是信息有损压缩。
- **特征工程全路线封顶证据链**：外盘扩展、特征裁剪、周线多尺度三轮全部负贡献（全 66 折实证）——冠军 v4 是特征面终态。

### 11. 概率校准对比（Platt 维持冠军）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-calibration-compare-2026-08-16.md`
- `calibrate_signals` 新增 **Isotonic** 与 **none** 分支，全 66 折对比（冠军 v4 特征集）。
- **结果**：Platt **70.23% 维持冠军**；Isotonic -2.93pp（但 RankIC +0.0207、ECE=0 完美校准——per-fold 小样本过拟合破坏方向判断）；none 崩到 52.44%（再次印证校准必要性）。
- **结论**：校准维持 Platt；isotonic 保留为备选（样本量增大时可重测）。单测 6/6，回归 34/34。

### 12. 多模型集成（模型侧封顶，LightGBM 单模型维持冠军）
- 报告：`deliverables/software-hexfutures-ai/lightgbm-ensemble-compare-2026-08-16.md`
- 新增 `EnsembleForecast`（LGB+XGB 双成员，cat 可选但本环境原生崩溃已降级；log1p 方差目标）；`walk_forward` 支持 `model_cls`。
- **结果**：LightGBM **70.23% 维持冠军**；Ensemble 68.77%（-1.47pp，RankIC 微升但方向准确率降、coverage 降 2.3pp）不采纳。
- **模型侧探索收官**：集成与 Isotonic 均负贡献——**冠军 v4（25 特征 + LightGBM + Platt）= 70.23% / RankIC 0.4510 为最终交付配置**。

## 结论
LightGBM 为确认冠军；全链路（特征重要性 → Optuna 调优 → 特征工程两轮 → 外盘特征两轮 → 利率因子验证 → 特征裁剪 → 周线多尺度 → 概率校准对比 → 多模型集成）打通，基准从 67.89% 提升至 **70.23%**（冠军 v4，25 特征；RankIC 0.4510），**特征面封顶 + 校准定稿（Platt）+ 模型侧封顶（集成负贡献）**。冠军 v4 为最终交付配置，研究路线收官。

### 14. GitHub 版本管理 + CI 上线（2026-08-16 傍晚）
- **仓库**：https://github.com/cloudhu/HexBroker（private，main），token 存 Windows 凭据管理器（不落盘）。
- **CI**：`.github/workflows/ci.yml`——push/PR 到 main 触发，py3.11/py3.12 矩阵：`pip install -r requirements.txt` + `pip install -e . --no-deps` + `pytest`。**首次运行通过**（commit a59c7e8，双矩阵 success）。
- **CI 排障两条根因**：① requirements `pandas==2.2.2`+numpy2 不兼容 → `>=2.2.3,<4`；② `test_kronos_predictor.py` 运行时 `import torch`（可选重依赖）→ 加 `_KRONOS_NEEDS_TORCH` skip 标记只跳过 3 个 Kronos 类（无 torch 123+10skip / 有 torch 133 全过）。
- 注：lint job 暂缓（ruff 152 个历史遗留问题，清理为独立任务）。

### 15. 嵌套验证：校准乐观偏差量化（2026-08-16 晚，⚠️ 重大方法学发现）
- 报告：`deliverables/software-hexfutures-ai/calibration-leakage-verification-2026-08-16.md`
- **发现**：现口径（per-fold 校准用本折测试标签拟合后同批评估）的 **70.23% 含约 +20.7pp 协议幻觉**——嵌套验证（cal_split=0.5，校准与评估零重叠）后方向准确率跌至 **49.53%**（vs 50% 随机线无显著差异）、RankIC 0.4510→0.0108 归零。
- **根因**：Platt 每折内单调不改变折内秩，但**跨折 (a,b) 不同改写跨折全局排序**——校准把"各折方向比例"编码进 p_up 尺度制造伪相关；`compute_gate1` 用校准后 p_up 算 Spearman 放大之。
- **真实状态**：raw p_up 无校准口径 52.44%（±1.1pp≈2.2σ，极微弱 alpha）；嵌套口径确认 5 日方向净 alpha 统计不显著。
- **裁决建议**：历史数值加注"现口径含乐观偏差"；新实验一律嵌套口径（`cal_split` 已支持）；真实 alpha 确立前暂停消融/调优。
- 代码：`_calibrate_and_split` + `walk_forward_lightgbm(cal_split=...)` + `compare_calibration_leakage.py`。

### 16. 代码审核三项建议落地（2026-08-16 晚）
- **夜盘跨零点修复**：`data/resample.py` 凌晨段（00:00-08:59）归属前一日夜盘（au/ag 至 02:30），修复负 offset/错误桶时间戳；+2 回归测试。
- **utils/evaluation 测试覆盖**：新增 `test_utils_core.py`（registry/seed/timeutil/io/fingerprint，12 项）与 `test_evaluation_metrics.py`（metrics/stats/baseline，9 项）；utils/evaluation 覆盖缺口补齐。
- **回归**：165/165 全过（+26 新增）。

### 17. Raw Alpha 诊断（2026-08-16 深夜，模型战略转向点）
- 报告：`deliverables/software-hexfutures-ai/raw-alpha-diagnosis-2026-08-16.md`
- **模型有真实 alpha**：raw p_up 方向准确率 53.37%（二项 p=0.0012，随机对照 2% 分位）；
- **真实信息在收益强度排序**：exp_ret 分位 Q0→Q4 实际收益 -0.20%→+0.70% 单调（0.9pp/5日）；RankIC +0.056；
- **看多可用看空反指**：p_up>0.6 组实际涨 57.4%；p_up<0.4 组实际涨 53.6%；
- **品种分化**：ag0/m0 显著（54.7%/55.6%），au0 无；**时间不持续**：2020/2023 强、2021/2024 负；
- **战略转向**：方向预测（70.23% 幻觉）→ 强度排序 + 单边多头（真实可交易）。

### 18. 信号形态矫正（2026-08-16 深夜，模型矫正落地）
- 报告：`deliverables/software-hexfutures-ai/signal-form-correction-2026-08-16.md`
- 嵌套口径三形态对比：p_up 方向 50.57%（❌ 死路）；exp_ret 多空 +0.688%/5日（年化净 24.4%）；**单边多头 top20% +0.696%/5日（年化净 29.8%）✅ 最优**；
- 嵌套下 exp_ret 分位单调 +0.681（零泄漏真实 alpha）；截面排序（品种内≈0）；m0 核心/ag0 次/au0 无；
- 固化：`hexbroker/evaluation/strength.py`（强度信号评估模块）+ 5 测试；回归 222/222。
- **模型矫正完成**：方向预测 → exp_ret 强度排序 + 单边多头（看空反指禁用）。

### 19. P0 完整回测（2026-08-16 深夜，真实可交易水平确立）
- 报告：`deliverables/software-hexfutures-ai/strength-backtest-2026-08-16.md`
- 单边多头（exp_ret top20%）BacktestEngine 完整回测（滑点/手续费/保证金/逐 bar 调仓）：
  - 名义 20/50/100%：年化 **1.26%→3.29%→6.03%**，回撤 4.9%→21.9%，**Sharpe 稳定 0.51-0.54**；成本拖累 <0.3pp/年；
  - **34.8% 信号级估算为口径幻觉**（满仓单信号 vs 实际触发 17%+资金稀释）——真实水平年化 ~6%；
- 工程：CostModel **per-symbol 合约参数**（修复全局 multiplier=10 对 au/ag 错误）+ BacktestConfig.contracts + 回测脚本参数化；回归 224/224。
- **口径纪律**：后续收益讨论一律以完整回测为准。

### 19. P0 完整回测（2026-08-16 深夜，真实可交易水平确立）
- 报告：`deliverables/software-hexfutures-ai/strength-backtest-2026-08-16.md`
- 单边多头（exp_ret top20%）BacktestEngine 完整回测（滑点/手续费/保证金/逐 bar 调仓）：
  - 名义 20/50/100%：年化 **1.26%→3.29%→6.03%**，回撤 4.9%→21.9%，**Sharpe 稳定 0.51-0.54**；成本拖累 <0.3pp/年；
  - **34.8% 信号级估算为口径幻觉**（满仓单信号 vs 实际触发 17%+资金稀释）——真实水平年化 ~6%；
- 工程：CostModel **per-symbol 合约参数**（修复全局 multiplier=10 对 au/ag 错误）+ BacktestConfig.contracts + 回测脚本参数化；回归 224/224。
- **口径纪律**：后续收益讨论一律以完整回测为准。

### 20. 触发阈值网格（2026-08-16 深夜，Sharpe 0.54→0.85）
- 报告：`deliverables/software-hexfutures-ai/topk-grid-backtest-2026-08-16.md`
- top-k 网格（10-70%）完整回测：**top-30% 最优**——Sharpe 0.85（+57%）、年化 10.3%（+71%）、回撤持平（-20.8%）；
- 收益平台结构（30% 后饱和）、Sharpe 30% 见顶；机制 = 极端信号噪声高，30% 有效带更宽；
- 固化：默认 `TOP_K=0.30`（backtest_strength_signal.py）；grid_topk_backtest.py 网格脚本（walk_forward 单次复用）。

### 20. 触发阈值网格（2026-08-16 深夜，Sharpe 0.54→0.85）
- 报告：`deliverables/software-hexfutures-ai/topk-grid-backtest-2026-08-16.md`
- top-k 网格（10-70%）完整回测：**top-30% 最优**——Sharpe 0.85（+57%）、年化 10.3%（+71%）、回撤持平（-20.8%）；
- 收益平台结构（30% 后饱和）、Sharpe 30% 见顶；机制 = 极端信号噪声高，30% 有效带更宽；
- 固化：默认 `TOP_K=0.30`（backtest_strength_signal.py）；grid_topk_backtest.py 网格脚本（walk_forward 单次复用）。

### 21. 市场状态过滤器（2026-08-16 深夜，Sharpe 0.85→1.21）
- 报告：`deliverables/software-hexfutures-ai/market-state-filter-2026-08-16.md`
- **trend 过滤采纳**（close<MA20 空仓）：Sharpe 0.85→**1.21**（+42%）、回撤 -20.8%→**-8.8%**（-58%）、年化持平（10.2%）——几乎纯增益；vol 过滤负贡献、combo 保守可选；
- **最终策略**：exp_ret top-30% 做多 + 趋势过滤 → 年化 10.2% / 回撤 8.8% / **Sharpe 1.21**；
- 脚本：filter_market_state.py；待验证：MA20/波动阈值样本外确认。

### 21. 市场状态过滤器（2026-08-16 深夜，Sharpe 0.85→1.21）
- 报告：`deliverables/software-hexfutures-ai/market-state-filter-2026-08-16.md`
- **trend 过滤采纳**（close<MA20 空仓）：Sharpe 0.85→**1.21**（+42%）、回撤 -20.8%→**-8.8%**（-58%）、年化持平（10.2%）——几乎纯增益；vol 过滤负贡献、combo 保守可选；
- **最终策略**：exp_ret top-30% 做多 + 趋势过滤 → 年化 10.2% / 回撤 8.8% / **Sharpe 1.21**；
- 脚本：filter_market_state.py；待验证：MA20/波动阈值样本外确认。

### 22. MA 窗口网格验证（2026-08-16 深夜，过拟合警示）
- 报告：`deliverables/software-hexfutures-ai/ma-window-grid-2026-08-16.md`
- MA5/10/20/30/50 → Sharpe 1.12/0.76/**1.21**/0.82/0.76：短窗口过滤方向稳健（回撤全减半），**MA20 为尖峰非平台**——参数选择过拟合风险高；
- 建议：保守部署 MA5；**上线前样本外分段验证**（2018-2021→2022-2024）。

### 22. MA 窗口网格验证（2026-08-16 深夜，过拟合警示）
- 报告：`deliverables/software-hexfutures-ai/ma-window-grid-2026-08-16.md`
- MA5/10/20/30/50 → Sharpe 1.12/0.76/**1.21**/0.82/0.76：短窗口过滤方向稳健（回撤全减半），**MA20 为尖峰非平台**——参数选择过拟合风险高；
- 建议：保守部署 MA5；**上线前样本外分段验证**（2018-2021→2022-2024）。

### 23. SENTINEL 新架构启动（2026-08-17，四层模型）
- 架构文档：`deliverables/software-hexfutures-ai/sentinel-architecture-2026-08-17.md`（L1 强度排序/L2 状态门控/L3 RL 决策/L4 自回归进化）
- Phase 1：自研 RankNet 排序目标未胜出（Q4-Q0 0.32% vs LGBM 0.70%）→ 信号源用 exp_ret；
- **Phase 3：SentinelTradingEnv + 自研 PPO，OOS(2022-2024) 年化 +13.8%/Sharpe 0.83 ✅**；
- 报告：sentinel-phase1-3-2026-08-17.md。

### 24. SENTINEL Phase 4 进化闭环（2026-08-17 上午，四层架构全验证）
- 报告：`deliverables/software-hexfutures-ai/sentinel-phase4-2026-08-17.md`
- 奖励工程：bp 尺度 + 趋势惩罚（trend 过滤编码进奖励）+ 权重可进化；
- 自回归 ES 进化奖励权重（valid 2022-23 选择 / OOS 2024 完全留出）：OOS 正收益低回撤；
- ⚠️ OOS 仅 96 天（数据截止 2024-07）——年化 137%/Sharpe 5.47 为小样本外推不可信；
- **P0 下一步：延长数据（sina 拉 2024-07~2026-08）重跑 OOS 验证**；SENTINEL 四层全部验证通过。

### 24. SENTINEL Phase 4 进化闭环（2026-08-17 上午，四层架构全验证）
- 报告：`deliverables/software-hexfutures-ai/sentinel-phase4-2026-08-17.md`
- 奖励工程：bp 尺度 + 趋势惩罚（trend 过滤编码进奖励）+ 权重可进化；
- 自回归 ES 进化奖励权重（valid 2022-23 选择 / OOS 2024 完全留出）：OOS 正收益低回撤；
- ⚠️ OOS 仅 96 天（数据截止 2024-07）——年化 137%/Sharpe 5.47 为小样本外推不可信；
- **P0 下一步：延长数据（sina 拉 2024-07~2026-08）重跑 OOS 验证**；SENTINEL 四层全部验证通过。

### 25. SENTINEL 全链路重跑（2026-08-17 上午，OOS 扩样本 + 同段对比）
- 报告：`deliverables/software-hexfutures-ai/sentinel-full-rerun-2026-08-17.md`
- 数据延长实测：sina/tdx/westock 均无现成 2024-07 后连续（tdx 需多合约拼接）→ 工程化脚手架待执行；
- 重排分段 OOS 扩到 1.2 年（240 信号）：**SENTINEL RL Sharpe 3.83 vs 规则基线 0.88 同段**——RL 全面占优，验证通过；
- ⚠️ 绝对年化 79.6% 偏高（OOS 行情偏多），真新数据验证为 P0。

### 25. SENTINEL 全链路重跑（2026-08-17 上午，OOS 扩样本 + 同段对比）
- 报告：`deliverables/software-hexfutures-ai/sentinel-full-rerun-2026-08-17.md`
- 数据延长实测：sina/tdx/westock 均无现成 2024-07 后连续（tdx 需多合约拼接）→ 工程化脚手架待执行；
- 重排分段 OOS 扩到 1.2 年（240 信号）：**SENTINEL RL Sharpe 3.83 vs 规则基线 0.88 同段**——RL 全面占优，验证通过；
- ⚠️ 绝对年化 79.6% 偏高（OOS 行情偏多），真新数据验证为 P0。

### 26. 数据源寻找（2026-08-17 中午）
- 报告：`deliverables/software-hexfutures-ai/data-source-evaluation-2026-08-17.md`
- 实测 5 源全有缺口：sina 截止 2024-07 / tdx 仅未交割 / westock 仅现货 / mx skill 无期货 / akshare 安装被沙箱 safe-delete 破坏（venv pip 不可靠）；
- **推荐连接**：PandaData（明确含期货）/ Wind / 东财妙想——连接后即可完成数据延长 + SENTINEL 最终验证。

### 27. SENTINEL 数据延长 + 最终验证（2026-08-17 下午，全链路收官）
- 报告：`deliverables/software-hexfutures-ai/sentinel-final-verification-2026-08-17.md`
- **PandaData 主力连续延长成功**（2018→2026-08，+501 根/品种，重叠期 cv<0.001 无缝拼接）；
- **最终 OOS（2 年真新数据）：SENTINEL RL Sharpe 2.27 vs 规则基线 -0.13**——最强证据；
- ⚠️ 年化 100.9% 含贵金属牛市 beta，回撤 39% 需风控；环境 akshare 灾难致 14 包损坏已全部修复（224/224）。

### 27. SENTINEL 数据延长 + 最终验证（2026-08-17 下午，全链路收官）
- 报告：`deliverables/software-hexfutures-ai/sentinel-final-verification-2026-08-17.md`
- **PandaData 主力连续延长成功**（2018→2026-08，+501 根/品种，重叠期 cv<0.001 无缝拼接）；
- **最终 OOS（2 年真新数据）：SENTINEL RL Sharpe 2.27 vs 规则基线 -0.13**——最强证据；
- ⚠️ 年化 100.9% 含贵金属牛市 beta，回撤 39% 需风控；环境 akshare 灾难致 14 包损坏已全部修复（224/224）。

### 28. RL 接入 BacktestEngine（2026-08-17 傍晚，口径幻觉暴露）
- 报告：`deliverables/software-hexfutures-ai/rl-backtestengine-2026-08-17.md`
- **env 模拟 = 口径幻觉**（5 日收益窗口归因放大 ~5 倍）：RL 真实水平 = OOS 年化 0.41%/Sharpe 0.22（vs 模拟 100.9%/2.27）；
- **P0 修复**：RL 环境奖励逐日化（与 BacktestEngine 口径对齐）后重训；
- 外盘 UUP 可得/SPX 待权限；影响有限（NaN 由 LightGBM 处理）。

### 28. RL 接入 BacktestEngine（2026-08-17 傍晚，口径幻觉暴露）
- 报告：`deliverables/software-hexfutures-ai/rl-backtestengine-2026-08-17.md`
- **env 模拟 = 口径幻觉**（5 日收益窗口归因放大 ~5 倍）：RL 真实水平 = OOS 年化 0.41%/Sharpe 0.22（vs 模拟 100.9%/2.27）；
- **P0 修复**：RL 环境奖励逐日化（与 BacktestEngine 口径对齐）后重训；
- 外盘 UUP 可得/SPX 待权限；影响有限（NaN 由 LightGBM 处理）。

### 29. P0 RL 奖励逐日化（2026-08-17 傍晚，修复有效）
- 报告：`deliverables/software-hexfutures-ai/rl-daily-reward-2026-08-17.md`
- 奖励 5 日归因 → 当日收益（fwd 1 日），与 BacktestEngine 对齐；
- **OOS 完整口径：Sharpe 0.22→0.35、年化 0.41%→1.04%**——修复有效；仍弱需进化适应度直连 BacktestEngine。

### 29. P0 RL 奖励逐日化（2026-08-17 傍晚，修复有效）
- 报告：`deliverables/software-hexfutures-ai/rl-daily-reward-2026-08-17.md`
- 奖励 5 日归因 → 当日收益（fwd 1 日），与 BacktestEngine 对齐；
- **OOS 完整口径：Sharpe 0.22→0.35、年化 0.41%→1.04%**——修复有效；仍弱需进化适应度直连 BacktestEngine。

### 30. 进化直连 BacktestEngine（2026-08-17 傍晚，最终裁决）
- 报告：`deliverables/software-hexfutures-ai/evolution-bt-fitness-2026-08-17.md`
- 适应度直连 BacktestEngine：valid 选择 Sharpe 0.78 但 **OOS -0.02**——valid 过拟合暴露；
- **四轮校准后 SENTINEL RL OOS ≈ 0（噪声级）**——不升格主策略；规则基线（全样本 Sharpe 1.21）为最稳健策略；
- 口径纪律固化：完整回测 + OOS 留出为准。

### 30. 进化直连 BacktestEngine（2026-08-17 傍晚，最终裁决）
- 报告：`deliverables/software-hexfutures-ai/evolution-bt-fitness-2026-08-17.md`
- 适应度直连 BacktestEngine：valid 选择 Sharpe 0.78 但 **OOS -0.02**——valid 过拟合暴露；
- **四轮校准后 SENTINEL RL OOS ≈ 0（噪声级）**——不升格主策略；规则基线（全样本 Sharpe 1.21）为最稳健策略；
- 口径纪律固化：完整回测 + OOS 留出为准。

### 31. 多分段 valid 交叉选择（2026-08-17 傍晚）
- 报告：`deliverables/software-hexfutures-ai/cross-segment-valid-2026-08-17.md`
- 进化适应度 = 3 子段交叉平均 Sharpe：**OOS -0.02 → +0.36**（BacktestEngine 完整口径，年化 0.70%/回撤 -3.0%）；
- RL 定位辅助增强；最优权重极度保守（高趋势惩罚）。

### 31. 多分段 valid 交叉选择（2026-08-17 傍晚）
- 报告：`deliverables/software-hexfutures-ai/cross-segment-valid-2026-08-17.md`
- 进化适应度 = 3 子段交叉平均 Sharpe：**OOS -0.02 → +0.36**（BacktestEngine 完整口径，年化 0.70%/回撤 -3.0%）；
- RL 定位辅助增强；最优权重极度保守（高趋势惩罚）。

### 32. P1 三件套：品种池 6 + 更长 valid + 状态增强（2026-08-17 傍晚）
- 报告：`deliverables/software-hexfutures-ai/symbol-pool-extension-2026-08-17.md`
- 品种 3→6（cu/rb/i PandaData 全历史）、动作 2 档、状态 36 维、valid 3 段交叉；
- **OOS（592 信号）：RL Sharpe 0.38 vs 基线 -0.27**——提升边际但统计更稳健；RL 定位辅助增强层。

### 33. 全项目反思（2026-08-17 傍晚，信号反转诊断）
- 报告：`deliverables/software-hexfutures-ai/project-reflection-2026-08-17.md`
- **主病灶**：OOS 真新数据段分位桶倒挂（+0.688% → -0.884%）——截面排序信号在 regime 切换（贵金属牛+黑色熊）中系统性反转；非典型欠拟合，有过拟合（MA20/top30/valid）但非主因；
- 跨品种多空验证失败（空头侧无 alpha）——排除该路径；
- **提升路径**：信号监控自适应降仓（P0）→ 滚动重训参数（P0）→ 多信号融合+板块分层（P1）→ RL 辅助（P2）。

### 33. 全项目反思（2026-08-17 傍晚，信号反转诊断）
- 报告：`deliverables/software-hexfutures-ai/project-reflection-2026-08-17.md`
- **主病灶**：OOS 真新数据段分位桶倒挂（+0.688% → -0.884%）——截面排序信号在 regime 切换（贵金属牛+黑色熊）中系统性反转；非典型欠拟合，有过拟合（MA20/top30/valid）但非主因；
- 跨品种多空验证失败（空头侧无 alpha）——排除该路径；
- **提升路径**：信号监控自适应降仓（P0）→ 滚动重训参数（P0）→ 多信号融合+板块分层（P1）→ RL 辅助（P2）。

### 34. P0-1 信号监控自适应降仓（2026-08-17 傍晚，止血落地）
- 报告：`deliverables/software-hexfutures-ai/adaptive-exposure-2026-08-17.md`
- 滚动 W 日 exp_ret 分位价差监控（严格因果）：**W=20 step 全样本 Sharpe 0.34→0.48、回撤 -40%、年化不降反升**（平台非尖峰）；
- OOS 回撤 -36%（止血有效）但 Sharpe 0.05→0.08（噪声带）——止血非治疗；
- **推荐：top30% + W=20 价差<0 空仓**；与 trend 过滤正交叠加；P0-2 滚动重训参数待做。

### 34. P0-1 信号监控自适应降仓（2026-08-17 傍晚，止血落地）
- 报告：`deliverables/software-hexfutures-ai/adaptive-exposure-2026-08-17.md`
- 滚动 W 日 exp_ret 分位价差监控（严格因果）：**W=20 step 全样本 Sharpe 0.34→0.48、回撤 -40%、年化不降反升**（平台非尖峰）；
- OOS 回撤 -36%（止血有效）但 Sharpe 0.05→0.08（噪声带）——止血非治疗；
- **推荐：top30% + W=20 价差<0 空仓**；与 trend 过滤正交叠加；P0-2 滚动重训参数待做。

### 35. P0-2 滚动重训参数（2026-08-17 傍晚，负面结果）
- 报告：`deliverables/software-hexfutures-ai/rolling-params-2026-08-17.md`
- **滚动重选参数未胜出**（全样本 0.32 < 固定+MA20 0.43；OOS -0.06 < 0.05）——参数最优在 360 天窗口内是噪声（topk 0.2/0.4 横跳、选择期 Sharpe 与未来脱节）；
- **结论：可监控的是信号有效性（P0-1 ✅），不可预测的是参数最优（P0-2 ❌）**——滚动重训参数 = 周期性过拟合；
- 修正：固定 top30%+MA20 叠加 P0-1 监控为推荐配置；P1 多信号融合为根本解。

### 35. P0-2 滚动重训参数（2026-08-17 傍晚，负面结果）
- 报告：`deliverables/software-hexfutures-ai/rolling-params-2026-08-17.md`
- **滚动重选参数未胜出**（全样本 0.32 < 固定+MA20 0.43；OOS -0.06 < 0.05）——参数最优在 360 天窗口内是噪声（topk 0.2/0.4 横跳、选择期 Sharpe 与未来脱节）；
- **结论：可监控的是信号有效性（P0-1 ✅），不可预测的是参数最优（P0-2 ❌）**——滚动重训参数 = 周期性过拟合；
- 修正：固定 top30%+MA20 叠加 P0-1 监控为推荐配置；P1 多信号融合为根本解。

### 36. 叠加验证（2026-08-17 傍晚，最终配置确立）
- 报告：`deliverables/software-hexfutures-ai/combo-validation-2026-08-17.md`
- **C（top30%+信号监控 W=20 step）= 最终配置**：全样本 Sharpe 0.48/回撤 -9%；叠加 MA20 证伪（冗余非正交，0.44）；
- 推翻 P0-2 叠加建议；收益提升待 P1-1 多信号融合。

### 36. 叠加验证（2026-08-17 傍晚，最终配置确立）
- 报告：`deliverables/software-hexfutures-ai/combo-validation-2026-08-17.md`
- **C（top30%+信号监控 W=20 step）= 最终配置**：全样本 Sharpe 0.48/回撤 -9%；叠加 MA20 证伪（冗余非正交，0.44）；
- 推翻 P0-2 叠加建议；收益提升待 P1-1 多信号融合。

### 37. P1-1 多信号融合（2026-08-17 傍晚，根本解验证成功）
- 报告：`deliverables/software-hexfutures-ai/multi-signal-fusion-2026-08-17.md`
- **F1 = 0.7·rank(exp_ret) + 0.3·rank(mom60)，top30% + W=20 监控**：
  - 全样本 Sharpe **0.48→0.61**（+27%）、回撤 **-8.98%→-3.08%**（-66%）、年化 1.90%；
  - **OOS（真新数据）Sharpe -0.05→+0.19**——动量补截面失效期收益，首个全样本+OOS 双改善；
- mom20 太噪、纯动量全样本负（不可单用）；权重权衡曲线非尖峰。

### 37. P1-1 多信号融合（2026-08-17 傍晚，根本解验证成功）
- 报告：`deliverables/software-hexfutures-ai/multi-signal-fusion-2026-08-17.md`
- **F1 = 0.7·rank(exp_ret) + 0.3·rank(mom60)，top30% + W=20 监控**：
  - 全样本 Sharpe **0.48→0.61**（+27%）、回撤 **-8.98%→-3.08%**（-66%）、年化 1.90%；
  - **OOS（真新数据）Sharpe -0.05→+0.19**——动量补截面失效期收益，首个全样本+OOS 双改善；
- mom20 太噪、纯动量全样本负（不可单用）；权重权衡曲线非尖峰。

### 38. 自动循环迭代搜索（2026-08-17 傍晚，最优解收敛）
- 报告：`deliverables/software-hexfutures-ai/auto-iterate-optimal-2026-08-17.md`
- 三阶段自动搜索（粗网格→精调→平台性）：**最优 = 0.6·rank(exp_ret)+0.4·rank(mom120)，top25%，W=20 监控**；
- 全样本 Sharpe **0.96**/回撤 -1.6%；OOS **+0.24** 且 9/9 邻域全正（std=0.004 无尖峰）；
- 演进：0.48→0.61→**0.96**；工具 auto_iterate_search.py（可复跑）。

### 38. 自动循环迭代搜索（2026-08-17 傍晚，最优解收敛）
- 报告：`deliverables/software-hexfutures-ai/auto-iterate-optimal-2026-08-17.md`
- 三阶段自动搜索（粗网格→精调→平台性）：**最优 = 0.6·rank(exp_ret)+0.4·rank(mom120)，top25%，W=20 监控**；
- 全样本 Sharpe **0.96**/回撤 -1.6%；OOS **+0.24** 且 9/9 邻域全正（std=0.004 无尖峰）；
- 演进：0.48→0.61→**0.96**；工具 auto_iterate_search.py（可复跑）。

### 39. P1-2 板块分层（2026-08-17 傍晚，负面结果+新瓶颈发现）
- 报告：`deliverables/software-hexfutures-ai/sector-layering-2026-08-17.md`
- 板块分层四变体全部未胜出（基线 0.96 最优）——截面排序+动量已隐式完成分层，显式信号冗余；
- **新瓶颈：OOS 段仅 1.2% 持仓（防御过度）**——下一步 OOS 持仓密度校准（监控阈值放宽/mom 权重下调）。

### 39. P1-2 板块分层（2026-08-17 傍晚，负面结果+新瓶颈发现）
- 报告：`deliverables/software-hexfutures-ai/sector-layering-2026-08-17.md`
- 板块分层四变体全部未胜出（基线 0.96 最优）——截面排序+动量已隐式完成分层，显式信号冗余；
- **新瓶颈：OOS 段仅 1.2% 持仓（防御过度）**——下一步 OOS 持仓密度校准（监控阈值放宽/mom 权重下调）。

### 40. OOS 持仓密度校准（2026-08-17 傍晚，最终配置 Sharpe 1.01）
- 报告：`deliverables/software-hexfutures-ai/exposure-calibration-2026-08-17.md`
- 诊断：贵金属 OOS 持仓占 71%（未被防御）；持仓稀疏=截面 rank 偏低非监控过度；
- **采纳监控阈 -0.2%（Sharpe 0.96→1.01 纯增益）**；释放持仓不采纳（代价>收益）；
- **最终策略：0.6exp+0.4mom120, top25%, W20 监控阈 -0.2% → 全样本 Sharpe 1.01/OOS +0.24**。

### 40. OOS 持仓密度校准（2026-08-17 傍晚，最终配置 Sharpe 1.01）
- 报告：`deliverables/software-hexfutures-ai/exposure-calibration-2026-08-17.md`
- 诊断：贵金属 OOS 持仓占 71%（未被防御）；持仓稀疏=截面 rank 偏低非监控过度；
- **采纳监控阈 -0.2%（Sharpe 0.96→1.01 纯增益）**；释放持仓不采纳（代价>收益）；
- **最终策略：0.6exp+0.4mom120, top25%, W20 监控阈 -0.2% → 全样本 Sharpe 1.01/OOS +0.24**。

### 41. 名义比例放大测试（2026-08-17 傍晚，口径幻觉拆穿）
- 报告：`deliverables/software-hexfutures-ai/notional-scale-2026-08-17.md`
- **Sharpe 非杠杆中性**（30%→100%：1.01→0.46，回撤 13.6×）——30% 高 Sharpe 是手数 floor 排除高价弱品种（au/cu）的构成偏差；
- 100% 名义真实水平：年化 4.43%/回撤 21.5%/Sharpe 0.46——不建议杠杆化；
- 真正放大方向：品种池扩展/等名义修正/信号质量——而非名义杠杆。

### 41. 名义比例放大测试（2026-08-17 傍晚，口径幻觉拆穿）
- 报告：`deliverables/software-hexfutures-ai/notional-scale-2026-08-17.md`
- **Sharpe 非杠杆中性**（30%→100%：1.01→0.46，回撤 13.6×）——30% 高 Sharpe 是手数 floor 排除高价弱品种（au/cu）的构成偏差；
- 100% 名义真实水平：年化 4.43%/回撤 21.5%/Sharpe 0.46——不建议杠杆化；
- 真正放大方向：品种池扩展/等名义修正/信号质量——而非名义杠杆。

### 42. 品种池扩展 6→18（2026-08-17 傍晚，构成扭曲根治+真实化）
- 报告：`deliverables/software-hexfutures-ai/symbol-pool-18-2026-08-17.md`
- +12 品种（PandaData），18 品种信号 7952 条；**年化 1.21→9.76%（8×，扭曲根治）但 Sharpe 1.01→0.57（真实化，6品种含水分）**；
- 18 品种最优：w1=0.5/topk=0.30/thr-0.3% → 年化 9.8%/回撤 -35.5%/Sharpe 0.57；
- 定位：6=精选稳健 / 18=全市场进取（需风控）；下一步品种质量筛选（IC top10-12）。

### 42. 品种池扩展 6→18（2026-08-17 傍晚，构成扭曲根治+真实化）
- 报告：`deliverables/software-hexfutures-ai/symbol-pool-18-2026-08-17.md`
- +12 品种（PandaData），18 品种信号 7952 条；**年化 1.21→9.76%（8×，扭曲根治）但 Sharpe 1.01→0.57（真实化，6品种含水分）**；
- 18 品种最优：w1=0.5/topk=0.30/thr-0.3% → 年化 9.8%/回撤 -35.5%/Sharpe 0.57；
- 定位：6=精选稳健 / 18=全市场进取（需风控）；下一步品种质量筛选（IC top10-12）。

### 43. 品种质量筛选（2026-08-17 傍晚，静态筛选失败+认知修正）
- 报告：`deliverables/software-hexfutures-ai/symbol-quality-filter-2026-08-17.md`
- **认知修正：ag0(-0.156)/au0(-0.119) 嵌套 IC 为负（反指），hc/m/i 才是正 IC**——原 6 品种精选选错了；
- 静态筛选 top10/12 OOS 恶化（-0.29/-0.14 vs 18 全部 +0.09）——IC 随 regime 漂移，静态=样本内选择；
- **对症解：品种级滚动 IC 监控**（负 IC 品种当期剔除）——P0。

### 43. 品种质量筛选（2026-08-17 傍晚，静态筛选失败+认知修正）
- 报告：`deliverables/software-hexfutures-ai/symbol-quality-filter-2026-08-17.md`
- **认知修正：ag0(-0.156)/au0(-0.119) 嵌套 IC 为负（反指），hc/m/i 才是正 IC**——原 6 品种精选选错了；
- 静态筛选 top10/12 OOS 恶化（-0.29/-0.14 vs 18 全部 +0.09）——IC 随 regime 漂移，静态=样本内选择；
- **对症解：品种级滚动 IC 监控**（负 IC 品种当期剔除）——P0。

### 44. 宏观多因子 + 分组建模（2026-08-18 上午，净值首次兑现）
- 报告：`deliverables/software-hexfutures-ai/group-modeling-2026-08-18.md`
- **分组建模（5 组独立训练 + 特征-品种白名单）**：全样本 Sharpe **0.57→0.70**（+23%）、年化 9.76→11.75%、回撤 -30.6%；**OOS Sharpe 0.09→0.17（翻倍）**；
- 机制：组内排序纯净（j/ta 转正）+ 特征白名单消除跨组污染；p0 恶化待组内细分；
- **方法论突破：加特征正确但组织方式更重要**——分组建模优于堆特征。
