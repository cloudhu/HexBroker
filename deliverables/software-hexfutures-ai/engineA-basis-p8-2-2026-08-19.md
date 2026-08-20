# P8-2 引擎 A 加入基差特征并重训重估（2026-08-19）

## 结论摘要

**IS_PASS: NO（不采纳 v5）**

加入基差特征（f_basis_ratio / f_basis_ratio_rank / f_basis_ratio_z / f_basis）后，
引擎 A OOS 全面恶化（非策略选择假象）：

- 引擎 A S2 单引擎 OOS Sharpe：**-0.024（v4）→ -0.335（v5），Δ -0.311**
- 组合 A15/B85 OOS Sharpe：**1.384（v4）→ 1.343（v5），Δ -0.040**
- S1–S4 四种稀疏策略一致恶化（S1 -0.024→-0.335；S3 -0.006→-0.311；S4 -0.006→-0.311）

基差特征确实被模型使用（8 组全部命中，合计重要性 9.0%–18.0%），且部分分组 OOS IC
改善（agri_oil +0.134、ferrous_raw +0.107、agri_protein +0.089），但**无法转化为引擎 A
截面排序质量的提升**。这进一步支持 P5/P6 结论：**引擎 A 瓶颈在模型/标签层面，而非缺失
基本面特征**——即使注入已被引擎 B 验证的 alpha 源（基差），LightGBM 学习到的映射在
OOS 上仍为负贡献。生产维持 v4 缓存。

---

## 1. 实现说明

### 1.1 新增 `hexbroker/feature/fundamental.py`
- `add_fundamental(df, fundamental_close, sym_label, params)`：把品种基差数据对齐到 K 线
  datetime 索引（`reindex + ffill`，只取 t 日及以前最新值，无前视）。
- 特征（f_ 前缀）：
  - `f_basis_ratio`：原始基差率（ffill 对齐值）
  - `f_basis_ratio_rank`：品种内滚动 252 日分位（min_periods=60）——**与引擎 B
    `load_basis_panel` 完全一致**（在原始基本面日期网格上计算后 ffill 对齐，避免日频
    重复值计数扭曲）
  - `f_basis_ratio_z`：品种内滚动 252 日 z-score（min_periods=60）
  - `f_basis`：绝对基差（默认生成，`include_basis=False` 可关；不同品种量纲由树模型按
    特征分裂自适应）
- 缺失处理：早于首个基本面观测日 / 品种无数据 → NaN；`build_windows`（既有行为）训练前
  fill 0.0 → 等价「中性填充 0」。18 品种 K 线覆盖 95.0%–100%（仅 au/ag/sc 起始略晚）。
- 参数：`window=252`、`min_periods=60`、`include_basis=True`（默认=引擎 B 定案值）。

### 1.2 管线接入 `hexbroker/feature/pipeline.py`
- `FeaturePipeline.__init__` 新增 `fundamental_data: dict[str, DataFrame|Series]` 参数
  （键=品种短名如 `au`），`fundamental_params` 从 `cfg.feature.fundamental_params` 读取。
- `_per_symbol` 在 cross 分支后新增 `if "fundamental" in self.transformers` 分支，
  按 `sym_label` 注入并调用 `add_fundamental`。
- **基差特征不参与滚动 z-score 标准化**（`f_basis*` 从 normalize 子集排除）：rank/z 已为
  无量纲形态（保持引擎 B 定案形态不被再标准化扭曲），原始 ratio/basis 量纲由树模型自适应。
- fail-fast：transformers 含 `fundamental` 但未传 `fundamental_data` → 显式报错。
- `build_features` 透传 `fundamental_data`。
- **默认不启用**：`transformers` 默认列表不变（`["technical","microstructure","normalize"]`），
  仅分组建模脚本显式启用，向后兼容。

### 1.3 配置 `hexbroker/config.py`
- `FeatureConfig` 新增 `fundamental_params: dict = Field(default_factory=dict)`。

### 1.4 信号生产 `scripts/group_modeling.py` / `scripts/group_modeling_v2.py`
- `build_group_signals`：新增 `load_fundamental_data(std_syms)`（读
  `data/raw/fundamental/basis_{SYM}.parquet` → `{短名: DataFrame}`）；transformers 显式加
  `"fundamental"`；`build_features(..., fundamental_data=fund)`；新增
  `collect_models` 参数（True 时返回 `(sig, wf)` 以收集特征重要性）。
- `group_modeling_v2.py`：新增 `--collect-importance` / `--importance-output`，按组聚合
  特征重要性（各组 global_codes 不同 → 特征列不同，不能跨组合并），输出
  `artifacts/p8_basis_importance.json`。
- **仅改分组建模信号生产脚本**；hexbroker 其他默认路径、v4 缓存、数据文件均未改动。

### 1.5 重估脚本 `scripts/p8_engineA_basis.py`
- `engine_a_targets_cs(cache_path=...)` 分别指向 v4/v5 → S2（min=3）单引擎 + A15/B85
  组合（vol=N/Y）完整口径回测；附加分组建模 OOS 时序 IC（Spearman exp_ret vs realized）。
- 产出：`artifacts/p8_engineA_basis_compare.csv`、`artifacts/p8_group_ic.csv`。

### 1.6 测试
- 新增 `tests/test_fundamental_features.py`（11 例：特征生成/Series 输入/None 输入/
  include_basis/缺列报错/ffill 对齐无前视/滚动无前视/z 因果/管线注入/fail-fast/默认不启用）。
- 全量测试套件通过（无失败）；v4 缓存 mtime 未变（Aug 19 18:29）。

---

## 2. 运行输出

### 2.1 重训日志摘要（v5，24m8s，exit 0）
```
[OK] ['au0', 'ag0']:       960 条信号 (global=['spx','uup','t10y'], fundamental=['ag','au'])
[OK] ['rb0', 'hc0']:       960 条信号 (global=['spx','t10y'],       fundamental=['hc','rb'])
[OK] ['i0', 'j0', 'jm0']: 1440 条信号 (global=['spx','t10y'],       fundamental=['i','j','jm'])
[OK] ['cu0','al0','zn0','ni0']: 1920 条信号 (global=['spx','uup'],   fundamental=['al','cu','ni','zn'])
[OK] ['y0', 'p0']:         960 条信号 (global=['spx'],              fundamental=['p','y'])
[OK] ['m0']:               480 条信号 (global=['spx'],              fundamental=['m'])
[OK] ['sr0', 'cf0']:       960 条信号 (global=['spx'],              fundamental=['cf','sr'])
[OK] ['ta0', 'sc0']:       960 条信号 (global=['spx'],              fundamental=['sc','ta'])
[OK] v2 分组信号合并 8624 条 → artifacts/signals_cache18_grouped_v5.parquet
  品种覆盖: 18/18 全齐
```

### 2.2 v5 覆盖率（与 v4 完全一致 —— 纯信号质量变化，非覆盖率变化）
| 缓存 | 行数 | 天数 | 日均品种 | OOS 日均品种 |
| --- | ---: | ---: | ---: | ---: |
| v4 | 8624 | 662 | 13.0 | 13.1 |
| v5 | 8624 | 662 | 13.0 | 13.1 |

信号差异：p_up 57.7% 改变、exp_ret 100% 改变（v4-v5 exp_ret corr 0.809 / p_up corr 0.749）
——模型确实因基差特征改变了输出。

### 2.3 引擎 A / 组合重估表（v5 vs v4）
| 项 | v4（基线） | v5（含基差） | Δ |
| --- | ---: | ---: | ---: |
| 引擎 A S2 全样本 Sharpe | 0.682 | 0.607 | -0.075 |
| 引擎 A S2 **OOS Sharpe** | **-0.024** | **-0.335** | **-0.311** |
| 引擎 A S2 OOS 收益 | -1.99% | -8.59% | -6.60pp |
| 引擎 A S2 OOS MaxDD | -11.7% | -11.3% | +0.4pp |
| 组合 A15/B85 (vol=N) OOS Sharpe | **1.384** | **1.343** | **-0.040** |
| 组合 A15/B85 (vol=Y) OOS Sharpe | 1.393 | 1.347 | -0.046 |

S1–S4 稳健性（引擎 A 单引擎 OOS Sharpe）：
| 策略 | v4 | v5 | Δ |
| --- | ---: | ---: | ---: |
| S1 (min=None) | -0.024 | -0.335 | -0.311 |
| S2 (min=3) | -0.024 | -0.335 | -0.311 |
| S3 (min=5) | -0.006 | -0.311 | -0.305 |
| S4 (min=8) | -0.006 | -0.311 | -0.305 |

### 2.4 特征重要性（模型确实用上基差）
各组 f_basis* 合计重要性：agri_oil 17.97%、ferrous_raw 15.82%、agri_soft 13.36%、
chem_energy 12.87%、ferrous_steel 11.32%、industrial 11.06%、agri_protein 10.65%、
precious 9.00%。f_basis_ratio_rank 在 8 组中 rank 11–19/26–32，f_basis_ratio 在
agri_oil 达 rank 3/26（5.78%）。

### 2.5 分组建模 OOS 时序 IC（v4 vs v5）
| 组 | v4 rank_ic | v5 rank_ic | Δ |
| --- | ---: | ---: | ---: |
| agri_oil | -0.331 | -0.197 | **+0.134** |
| ferrous_raw | -0.049 | +0.058 | **+0.107** |
| agri_protein | +0.025 | +0.113 | **+0.089** |
| industrial | -0.039 | -0.044 | -0.005 |
| precious | -0.132 | -0.147 | -0.015 |
| ferrous_steel | +0.105 | +0.055 | -0.050 |
| agri_soft | +0.004 | -0.055 | -0.059 |
| chem_energy | -0.062 | -0.126 | -0.064 |

分组层面改善（agri_oil/ferrous_raw 恰是基差重要性最高的组）未能传导到引擎 A 截面
top30% 排序质量；A–B 收益相关性 OOS 反而下降（0.618→0.561），排除「与引擎 B 重复导致
分散化下降」的解释路径。

---

## 3. 最终裁决建议

- **v5 不采纳**：生产信号缓存维持 `artifacts/signals_cache18_grouped_v4.parquet`
  （config 默认不变），引擎 A 维持 P6-5 固化配置（S2 min=3）。
- **基差特征模块保留但默认关闭**：`hexbroker/feature/fundamental.py` + 管线分支已就绪、
  测试通过、零泄漏，可供后续（如引擎 A 标签/模型改造后的特征回归）复用；本次不进入
  生产 transformers。
- **引擎 A 瓶颈判断更新**：P8-1「缺基本面特征」嫌疑被证伪——注入已验证基差 alpha 后
  OOS 反而恶化。瓶颈更可能在：(1) 标签/目标构造（horizon-5 已实现收益 + n_mc=30 连续
  p_up 的排序能力），(2) 截面 top30% 与单品种时序信号不匹配，(3) 分组内样本/过拟合。
  建议后续在模型/标签层面实验（而非继续加特征）。
- 附加观察：分组建模 IC 表明基差在油脂/黑色原料组有分组级改善（+0.1 量级），若未来
  引擎 A 重构，可在这些组单独验证基差特征。

---

## 4. 耗时说明

| 环节 | 耗时 |
| --- | ---: |
| 实现（模块/管线/脚本/测试） | ~30 分钟 |
| 全量测试套件 | 1 分 07 秒 |
| v5 重训（8 组，n_jobs=6，collect_importance） | **24 分 08 秒**（后台，exit 0） |
| 引擎 A + 组合重估（v4/v5 各 4 策略 + 组合 + 分组 IC） | ~3 分钟 |
| 合计 | ~1 小时 |

关键产物：
- `artifacts/signals_cache18_grouped_v5.parquet`（新，不覆盖 v4）
- `artifacts/p8_basis_importance.json` / `artifacts/p8_retrain_v5.log`
- `artifacts/p8_engineA_basis_compare.csv` / `artifacts/p8_group_ic.csv`
- `tests/test_fundamental_features.py`（11 例全过）
