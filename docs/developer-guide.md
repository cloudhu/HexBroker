# HexFutures-AI 开发者指南（模型架构 v2.1）

> 项目代号：**HexFutures-AI** ｜ 根目录：`E:/Workspace/HexBroker/` ｜ Python 包：`hexbroker/`
> 文档定位：**当前模型架构的唯一权威描述**（2026-08-18 定稿）。工程师/研究员按本文档理解与复现系统。
> 上游：`docs/system_design.md`（阶段二初始设计）——本文档记录实际落地架构与初始设计的**全部偏离**（见 §7）。
> 性质声明：研究型可复现代码（research-grade），非生产交易系统。

---

## 0. 一页速览（TL;DR）

**当前模型 = 分组建模 LightGBM 截面排序 + 长动量融合 + 双风控门控的日频商品期货单边多头策略。**

```
数据层(18品种+4全局) → 特征层(25特征/组白名单) → 信号层(5组独立LightGBM exp_ret)
→ 融合层(0.8·rank(exp_ret)+0.2·rank(mom90)) → 选择层(top-35% 做多, 名义30%)
→ 风控层(信号监控W20 + 波动率目标17.5% EWMA10) → 评估层(嵌套/完整回测/OOS留出)
```

| 指标 | 全样本（2018~2026-08） | OOS（2024-07-18 后真新数据） |
|---|---:|---:|
| 年化 | +14.40% | +0.64% |
| 最大回撤 | -17.91% | -2.80% |
| Sharpe | **0.96** | **0.31** |

**架构关键事实**：经过 40+ 轮实验验证，项目最终**没有**采用初始设计的 Kronos 主预测、RL 决策主路径——它们被完整回测口径证伪或降级为辅助；真实可重复的 alpha 来自**分组建模的 exp_ret 截面排序 + 时间序列动量融合**。这套架构的全部结论均经过"幻觉拆穿协议"（§6）验证。

---

## 1. 架构总览（分层数据流）

```
┌─────────────────────────────────────────────────────────────────────┐
│ ① 数据层                                                            │
│   - 18 品种主力连续（17 用于信号，p0 已剔除）                        │
│   - 来源：PandaData close_pcr 后复权主力连续 + sina 基准拼接          │
│   - 全局参照：spx / uup / t10y（vix 已落盘但未用）                   │
│   - 合约参数 per-symbol（乘数/最小变动价位）                          │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ② 特征层（hexbroker/feature/）                                      │
│   - 25 特征 / 5 类：技术动量(6) 波动风险(4) 日内微观(8)              │
│     SPX跨市场(3) UUP跨市场(3) + t10y 跨市场(3，贵金属组)             │
│   - 特征-品种白名单：不同品种组挂不同宏观特征（分组建模核心）          │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ③ 信号层 L1：分组建模（scripts/group_modeling.py）                  │
│   - 5 组独立训练 LightGBM（组内截面排序纯净）                        │
│   - 输出：exp_ret（嵌套 walk_forward 样本外预测）                    │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ④ 融合层：score = 0.8·rank(exp_ret) + 0.2·rank(mom_90)             │
│   - exp_ret：截面排序（分组信号，质量经分组建模提升）                 │
│   - mom90：90 日时间序列动量（趋势跟踪，补截面失效期收益）            │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ⑤ 选择层：score ≥ top-35% 做多（单边，无做空）                      │
│   - 名义 30%/标的，手数 = floor(名义/(价格×乘数))                    │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ⑥ 风控层（双门控，严格因果 ≤t）                                    │
│   - 信号监控：过去 20 日跨品种 exp_ret 分位价差 < -0.3% → 降仓        │
│   - 波动率目标：组合 20 日 EWMA(halflife=10) 年化波动 → 17.5% 缩放    │
└──────────────┬──────────────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ⑦ 评估层（验证协议，全项目铁律）                                    │
│   - 嵌套 walk_forward（cal_split=0.5，零泄漏）                       │
│   - BacktestEngine 完整口径（滑点/手续费/保证金/per-symbol 合约）     │
│   - OOS 完全留出（2024-07-18 后，PandaData 独立源）                  │
│   - 平台性检查（参数邻域 OOS 稳定性，防尖峰过拟合）                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 各层详述

### 2.1 数据层

| 项 | 内容 |
|---|---|
| 品种池 | 18 个主力连续：au/ag（贵金属）、rb/i/hc/j/jm（黑色）、cu/al/zn/ni（有色）、m/y/p/sr/cf（农产品）、ta/sc（化工能源）；**p0 已从信号剔除（17 个）** |
| 主数据 | **PandaData `get_future_daily_post`（close_pcr 后复权主力连续）**，2018-01~2026-08，与 sina 基准重叠期校准拼接（cv<0.001） |
| 全局数据 | `data/raw/global/`：spx / uup / t10y（美债 10Y，wind 补齐到 2026-08-14）/ vix（已落盘未用）；外盘特征 2025-01 后 spx/uup 为 NaN（LightGBM 原生处理） |
| 合约参数 | `CONTRACTS18`：au×1000/tick0.02、ag×15/0.01、m×10/1、cu×5/10、rb×10/1、i×100/0.5、al×5/5、zn×5/5、**ni×1/10**、hc×10/1、y×10/2、p×10/2、j×100/0.5、jm×60/0.5、sr×10/1、cf×5/5、**ta×10/1**、sc×1000/0.1 |
| 落盘 | `data/raw/processed/{sym}/1d/*.parquet`（按年分片） |

### 2.2 特征层（25 特征 / 5 类）

| 类别 | 特征 | 说明 |
|---|---|---|
| 技术/动量（6） | f_ret_1, f_ret_acc_5, f_ret_acc_20, f_ma_spread, f_macd, f_rsi | 收益与动量 |
| 波动/风险（4） | f_vol_5, f_vol_20, f_boll_width, f_vol_ratio | 风险状态 |
| 日内微观（8） | f_intraday_range, f_body_ratio, f_upper_shadow, f_lower_shadow, f_bar_dir, f_gap, f_up_vol_share, f_range_pos_20 | 日线形态 |
| 跨市场 SPX（3） | f_xr_spx_ratio/mom/vol | 风险偏好 |
| 跨市场 UUP（3） | f_xr_uup_ratio/mom/vol | 美元 |
| 跨市场 t10y（3，贵金属组） | f_xr_t10y_ratio/mom/vol | 实际利率代理 |

**特征-品种白名单**（分组建模的关键设计，防止跨组污染）：

| 组 | 品种 | 宏观特征 | 依据 |
|---|---|---|---|
| precious 贵金属 | au/ag | spx+uup+t10y（全宏观） | 全球定价（实际利率/美元/风险） |
| ferrous 黑色 | rb/i/hc/j/jm | spx+t10y | 国内需求+宏观 |
| industrial 有色 | cu/al/zn/ni | spx+uup | 全球定价 |
| agri 农产品 | m/y/p/sr/cf | spx（少） | 避免 t10y 污染（实证教训） |
| chem_energy 化工能源 | ta/sc | spx（少） | 同上 |

### 2.3 信号层 L1：分组建模（核心创新）

- **机制**：5 组各自独立训练 LightGBM（组内只含组内品种的 bars/features），输出组内品种的嵌套样本外 exp_ret；
- **为什么有效**（实验证据）：①组内截面排序纯净——合并训练时负 IC 品种被强品种压制，分组后 j0/ta0 IC 转正（-0.06→+0.12、-0.02→+0.09）；②特征白名单消除跨组污染（ta/cf 不再被宏观噪声干扰）；
- **关键事实**：walk_forward 本就逐品种独立训练，"分组"的实际作用 = 特征构造白名单 + 排序空间纯净化；
- 全样本 Sharpe：合并训练 0.57 → 分组建模 0.70（同口径首次数值兑现）。

### 2.4 融合层

```
score = 0.8 × rank(exp_ret) + 0.2 × rank(mom_90)
```

- **exp_ret**：分组 LightGBM 的截面强度信号（排序目标，非方向概率）；
- **mom90**：90 日时间序列动量（close/close.shift(90)-1），趋势跟踪因子；
- **权重来源**：三阶段自动搜索（粗网格→精调→平台性）收敛，分组后 exp_ret 质量提升 → 动量权重可降（旧平衡 0.5 → 0.8/0.2）；
- 融合价值：截面信号失效期（OOS 贵金属单边牛市）动量接棒——OOS 从负转正。

### 2.5 选择层

- **单边多头**：score ≥ 1 - top_k（top_k=0.35）做多，无做空（空头侧实证无 alpha：跨品种多空 OOS -1.48%）；
- **名义等权**：每标的 30% 权益名义，手数 floor 取整（au/cu 高价品种在低名义下自然排除——6 品种时代构成偏差的来源，18 品种后已消除）。

### 2.6 风控层（双门控，均严格因果）

| 门控 | 机制 | 参数 | 作用 |
|---|---|---|---|
| **信号监控**（P0-1） | 过去 W=20 日跨品种 (exp_ret, realized) Q4-Q0 滚动价差 < -0.3% → 降仓（step/linear） | W=20，阈值 -0.3%，linear | 信号有效性门控——regime 切换期止血 |
| **波动率目标**（P2） | 组合 20 日 EWMA(halflife=10) 年化波动，scale=clip(17.5%/vol, 0, 1.5) | 目标 17.5%，EWMA hl=10 | 风险目标化——高波动期自动降仓 |

- 被证伪的风控：逐仓位止损（持仓周期短几乎不触发）、组合回撤熔断（粗粒度误伤反弹，Sharpe 0.98→0.61）；
- 波动率目标演进：等权 20 日（回撤 -26%→-18%）→ **EWMA10（OOS Sharpe 0.24→0.31 双改善）**。

### 2.7 评估层（验证协议——全项目铁律）

| 协议 | 内容 | 防什么 |
|---|---|---|
| 嵌套 walk_forward | cal_split=0.5（嵌套内再分校准/评估），零泄漏 | 校准幻觉（70.23% 案例） |
| 完整回测口径 | BacktestEngine：滑点 1tick + 手续费 0.005% + 保证金 12% + per-symbol 合约 + 逐 bar 调仓 | 信号级估算幻觉（34.8% 案例） |
| OOS 完全留出 | 2024-07-18 后（PandaData 独立采集路径，训练期后 2 年） | 样本内选择偏差 |
| 平台性检查 | 最优参数 ± 邻域 OOS 稳定性（std/全正率） | 尖峰过拟合（MA20 尖峰案例） |

---

## 3. 关键参数表（v2.1 生产配置）

| 层 | 参数 | 值 | 来源 |
|---|---|---|---|
| 数据 | 品种池 | 18（信号 17，剔 p0） | p0-removal |
| 特征 | global_codes（贵金属组） | spx, uup, t10y | group-modeling |
| 信号 | 分组 | 5 组独立 LightGBM | group-modeling |
| 信号 | walk_forward cal_split | 0.5（嵌套） | 全项目 |
| 信号 | LightGBM params | load_best_params()（冠军参数） | refine-champion |
| 融合 | w1（exp_ret 权重） | 0.8 | group-param-search |
| 融合 | w2（mom 权重） | 0.2 | 同上 |
| 融合 | mom 窗口 | 90 日 | 同上 |
| 选择 | top_k | 0.35 | group-param-search |
| 选择 | 名义/标的 | 30% 权益 | notional-scale |
| 风控 | 监控窗口 W | 20 日 | adaptive-exposure |
| 风控 | 监控阈值 | -0.3%（linear） | exposure-calibration |
| 风控 | 波动率目标 | 17.5% 年化 | drawdown-control |
| 风控 | EWMA halflife | 10 日 | vol-ewma-tune |
| 评估 | OOS 起点 | 2024-07-18 | data-extension |

---

## 4. 关键代码文件索引

| 文件 | 职责 |
|---|---|
| `hexbroker/config.py` | 配置（数据/特征/回测/风控/RL） |
| `hexbroker/feature/` | 特征管道（technical/microstructure/iterative/cross/normalize/pipeline） |
| `hexbroker/backtest/engine.py` | 回测引擎（targets 驱动，支持负 target） |
| `hexbroker/backtest/cost.py` | 成本模型（per-symbol 合约参数增强） |
| `hexbroker/backtest/broker.py` | 模拟券商（逐 bar 调仓） |
| `hexbroker/backtest/portfolio.py` | 组合/权益/回撤指标 |
| `hexbroker/evaluation/metrics.py` | 年化/回撤/Sharpe |
| `hexbroker/evaluation/strength.py` | 强度信号评估模块（quintile/long_only/long_short） |
| `hexbroker/forecast/intensity.py` | RankNet 排序学习器（L4 进化候选，未入主路径） |
| `hexbroker/rl/sentinel_env.py` | RL 决策环境（研究框架保留） |
| `hexbroker/rl/agent.py` | 自研 PPO（研究框架保留） |
| `hexbroker/evolution/examm_engine.py` | 自回归进化引擎（研究框架保留） |
| `scripts/group_modeling.py` | **分组建模（主信号生产）**：5 组独立训练 + 白名单 |
| `scripts/build_signals18.py` | 18 品种信号缓存构建（walk_forward） |
| `scripts/eval_signals18.py` | 18 品种评估（滚动监控/回测工具） |
| `scripts/group_param_search.py` | 三阶段参数搜索（--phase 1/2/3） |
| `scripts/drawdown_control.py` | 回撤控制实验（止损/熔断/波目标） |
| `scripts/vol_ewma_tune.py` | 波动率 EWMA 微调 |
| `scripts/monitor_adaptive_exposure.py` | P0-1 信号监控（信号缓存复用） |
| `scripts/p0_removal.py` | p0 剔除实验 |
| `scripts/build_extended_data.py` | PandaData 数据延长拼接 |
| `scripts/parse_pandadata.py` | PandaData 持久化解析 |
| `scripts/update_t10y.py` / `update_vix.py` | 全局数据落盘 |
| `artifacts/signals_cache18_grouped.parquet` | 分组信号缓存（v1 等价，7952 条） |

---

## 5. 架构演进史（从初始设计到 v2.1）

| # | 阶段 | 变更 | 结论 |
|---|---|---|---|
| 1 | 信号矫正（08-16） | 方向预测 p_up → exp_ret 强度排序 + 单边多头 | 方向 50.6%≈随机死路；强度排序真实 alpha |
| 2 | 完整回测（08-16） | BacktestEngine 口径 + CostModel per-symbol | 信号级 34.8% 是口径幻觉，真实 ~6% |
| 3 | 阈值网格（08-16） | top 20%→30% | Sharpe 0.54→0.85 |
| 4 | 趋势过滤（08-16） | close<MA20 空仓 | Sharpe 0.85→1.21（后被监控取代） |
| 5 | SENTINEL 四层（08-17） | L1 排序/L2 门控/L3 RL/L4 进化 | 架构设计交付 |
| 6 | RL 校准链（08-17） | env 模拟→完整回测→逐日奖励→BT 适应度→交叉 valid | RL OOS ≈0（噪声级）——**不升格主策略** |
| 7 | 全项目反思（08-17） | OOS 分位桶分解 | **信号 regime 反转是主病灶**（+0.688%→-0.884%） |
| 8 | P0-1 信号监控（08-17） | 滚动价差监控 | 止血有效（Sharpe 0.34→0.48） |
| 9 | P0-2 滚动参数（08-17） | 定期重选参数 | **负面**：参数最优是噪声——不采纳 |
| 10 | P1-1 多信号融合（08-17） | +mom60 | 首个全样本+OOS 双改善（0.48→0.61） |
| 11 | 自动迭代（08-17） | mom120 + top25% | Sharpe 0.96（OOS 平台性 std=0.004） |
| 12 | 数据延长（08-17） | PandaData 2018→2026-08 | OOS 扩到真新数据 |
| 13 | 品种池 6→18（08-17） | +12 品种 | 年化 8×（构成扭曲根治）；Sharpe 真实化 0.57 |
| 14 | 分组建模（08-18） | 5 组独立训练 + 白名单 | **净值首次兑现**（0.57→0.70） |
| 15 | 分组×参数搜索（08-18） | 0.8/0.2 + mom90 + top35% | **Sharpe 破 1**（1.001，OOS +0.334） |
| 16 | 组内细分（08-18） | 8 组 vs 5 组 | **负面**：完全等价——分组仅影响特征 |
| 17 | p0 剔除（08-18） | 17 品种 | OOS 0.33→0.40（净正） |
| 18 | VIX 落盘（08-18） | 贵金属组 +vix | **不采纳**：单因子相关性≈0 |
| 19 | 回撤控制（08-18） | 波目标 17.5% | 回撤 -26%→-18%（止损/熔断否决） |
| 20 | **EWMA 微调（08-18）** | **波目标 EWMA10** | **v2.1 定稿**（OOS Sharpe 0.24→0.31） |

## 6. 幻觉拆穿清单（方法论资产）

| # | 幻觉 | 数字 | 拆穿手段 |
|---|---|---|---|
| 1 | 校准幻觉 | 方向 70.23% | 嵌套口径（cal_split=0.5） |
| 2 | 信号级估算 | 年化 34.8% | BacktestEngine 完整回测 |
| 3 | RL env 模拟 | OOS 100.9%/Sharpe 2.27 | BT 口径（真实 0.41%/0.22） |
| 4 | 5 日归因 | 奖励口径虚高 | 奖励逐日化（fwd 1 日） |
| 5 | valid 选择 | 0.78→OOS -0.02 | 适应度直连 BT |
| 6 | 杠杆中性 | 30% Sharpe 1.01 | 名义放大测试（构成偏差） |
| 7 | 静态品种选择 | top10 OOS -0.29 | OOS 确认 |
| 8 | 滚动参数 | 0.32<固定 0.43 | OOS 确认 |
| 9 | MA20 尖峰 | std≈0.2 | 平台性检查 |

## 7. 与 system_design.md 初始设计的偏离记录

| 初始设计 | 实际落地 | 偏离原因 |
|---|---|---|
| Kronos-small 主预测 | **未采用**（LightGBM exp_ret 主） | Kronos 研究方向独立推进（temp/kronos/），本链路未集成 |
| AR-Transformer fallback | 未采用 | 同上 |
| Stable-Baselines3 PPO 主决策 | 自研 PPO 研究框架保留，**不升格主策略** | 四轮校准后 RL OOS≈0（噪声级） |
| Optuna 超参优化 | 自研三阶段网格（粗→精→平台） | 更透明、平台性检查内置 |
| EXAMM 进化主外循环 | 自回归 ES 实验验证，未入主路径 | valid 过拟合暴露 |
| TQSDK 主数据源 | **PandaData 主力连续 + sina 拼接** | 数据可得性实测（TQSDK 不可用） |
| 风控 ATR 止损 v4.0 | **信号监控 + 波动率目标** | ATR 止损在日频短持仓下几乎不触发（实证） |
| 预测层+RL 耦合（SignalStore） | 保留（signals_cache18_*.parquet 即 SignalStore 实现） | 一致 |
| 三阶段回测（vn.py/RQAlpha 交叉） | 自研 BacktestEngine 唯一权威 | 自研引擎已含成本/保证金/per-symbol |

## 8. 复现路径（数据 → 结果）

```bash
# 1. 数据（已有本地 parquet，重拉用 PandaData/wind）
python scripts/build_extended_data.py        # au/ag/m 基准+延长拼接
python scripts/update_t10y.py                # 全局 t10y 补齐（wind）
# 2. 信号生产（分组建模，5 组独立 walk_forward）
python scripts/group_modeling.py --n-jobs 6  # 产出 signals_cache18_grouped.parquet
# 3. 参数（已固化，可复跑验证）
python scripts/group_param_search.py --phase 3   # 平台性检查
# 4. 风控验证
python scripts/vol_ewma_tune.py               # 波动率 EWMA 网格
# 5. 完整回测（v2.1 配置）
python scripts/drawdown_control.py            # 含波目标对比
# 6. 基本面引擎 B（P0→P3 全链路）
python scripts/parse_deepview_result.py <持久化文件> --metric basis --seg 2018_2022   # P0 解析（4 段）
python scripts/parse_deepview_result.py --merge                                          # 合并 → basis_{SYM}.parquet
python scripts/check_fundamental_quality.py    # 质量校验（对齐率/异常值）
python scripts/p1_factor_ic.py                 # P1 单因子 IC（品种内时序口径）
python scripts/p2_basis_backtest.py            # P2 引擎 B 基线回测
python scripts/p2_basis_grid.py                # P2 参数网格（win×thr）
python scripts/p3_combo_backtest.py            # P3 双引擎组合
```

## 9. Sentinel-2 双引擎（v3.0 方向，P0-P3 已落地）

### 9.1 动机与架构

v2.1 单引擎（趋势）OOS 年化仅 0.64%、信号 regime 反转（分位桶倒挂 +0.688%→-0.884%），
信息维度单一。Sentinel-2 引入 **基本面/均值回归引擎（引擎 B）** 与趋势引擎（引擎 A）低相关组合：

| 引擎 | 信号源 | 逻辑 | 目标 |
|---|---|---|---|
| A 趋势（v2.1） | exp_ret top30% | 动量+分组建模 | 趋势捕获 |
| **B 基差** | basis_ratio 品种内滚动分位 | 现货升水（backwardation）→ 期货收敛上涨 | **胜率 55-65%** |

### 9.2 P0 数据落地（PandaData DeepView）

- 方法：`get_future_basis`（basis/basis_ratio/spot_price）+ `get_future_warehouse_receipt`（仓单）
- 网关参数：**`symbol`**（列表），响应为 `result[]` 数组格式（非 dataframe）
- 分段拉取：2018-2022 / 2022-2026 × 18 品种 × 2 指标 → 合并去重
- 落盘：`data/raw/fundamental/{basis|warehouse}_{SYM}.parquet`
- 质量：**平均对齐率 98.0%**、仓单无负值；SC 极端基差集中在 2020-03 原油危机（真实事件）

### 9.3 P1 因子验证（关键方法论教训）

| 因子 | 截面 IC（错误口径） | **品种内时序 IC（正确口径）** | 判定 |
|---|---|---|---|
| basis_ratio | IS +0.09 → OOS ~0（假崩） | IS +0.077 → OOS +0.047（t=3.58） | **PASS** |
| basis | IS +0.08 → OOS -0.03（翻转） | IS +0.079 → OOS +0.041（t=3.01） | **PASS** |
| wr_change | 翻转 | OOS +0.003~+0.018 | NOT_PASS（否决） |
| wr_lot_change | 翻转 | 同 wr_change | NOT_PASS（否决） |

**教训**：基差收敛是【品种内时序】信号（同一品种基差偏离自身水平→收敛），
**截面 IC 是错误口径**——不同品种基差率水平不可比（RB 6~10% vs AU -0.6% vs SC -50%），
截面排序被品种水平主导，产生"IS 强 OOS 崩"的假象。
2024-07-18 后真新数据严格 OOS 复核：basis_ratio IC +0.056（h=10）仍 PASS。

### 9.4 P2 引擎 B 回测（完整 BacktestEngine 口径）

- 策略：basis_ratio 品种内滚动 252 日分位 ≥ 0.70 → 做多（名义 20% 权益/标的）
- 全样本：Sharpe **0.927**、年化 +16.5%、MaxDD -38.2%
- **OOS（2024-07-18 后）：Sharpe 1.260**、年化 +10.5%、MaxDD -6.7%
- **交易级胜率 64.4%**（命中 55-65% 目标）
- 参数网格 15/15 组合 OOS Sharpe 全正（0.40~1.34）——稳健，非过拟合

### 9.5 P3 双引擎组合

| 配置 | 全样本 Sharpe | OOS Sharpe | OOS MaxDD |
|---|---|---|---|
| 引擎 A（趋势） | 0.484 | **-0.19** | -10.7% |
| 引擎 B（基差） | 0.927 | **1.26** | -6.7% |
| **组合 A30/B70** | **0.947** | **0.859** | **-5.0%** |

- 日收益相关性仅 **0.306**（低相关目标达成）→ 组合回撤压缩、OOS 显著改善
- 结论：引擎 B 补上引擎 A 的 OOS 短板，**Sentinel-2 v1.0 组合可行**

### 9.6 P4 精调与验证（QA VERIFIED 3/3）

| 子任务 | 结论 | 证据 |
|---|---|---|
| P4-1 组合权重精调 | **A25/B75（不叠加波目标）**，OOS Sharpe **0.940**（优于基线 0.859，Δ+0.082） | 网格 8 配置全样本/OOS 全表 |
| P4-2 引擎B双因子升级 | **NOT_PASS 否决**：基差动量 f2（5d/10d）max\|OOS IC\|=0.0159<0.03，无增量信息 | 不构造合成、不改引擎B |
| P4-3 仓单周度复验 | **NOT_PASS 确认否决**：4 因子 IS/OOS 反号（3 反号 + 1 同号未过门槛），与 P1 一致 | 5d/20d 及 pct 变体全表 |

- **推荐生产配置 v3.1**：组合 **A=0.25 / B=0.75**、引擎B win=252/thr=0.7、**不叠加**组合层波动率目标（波目标全样本↑但 OOS 无增益，0.940→0.931）
- QA 建议项（非阻塞）：① P3 引擎 A 跨时间 rank 遗留（`exp_ret.rank(pct=True)` 全表排序非按日截面，信号日均 ~8 品种）→ 后续 P3 层面修复重估；② 同 bar 成交约定为已知限制（文档明示）；③ 若将来 f2 有 PASS 候选，改「IS 定候选、OOS 终裁」严格对齐铁律

### 9.7 P5 引擎 A 跨时间 rank 修复（QA VERIFIED）

**缺陷**：`engine_a_targets` 用 `exp_ret.rank(pct=True)` 全表跨时间排序，而信号缓存覆盖率不均（2019-21 日均13品种 / 2026 仅 2.2 品种/天，52.5% 天数<5 品种）→ top30% 阈值被扭曲。

**修复**（`scripts/p5_engineA_cross_section.py`）：按日横截面 rank（`groupby(ts).rank(pct=True)`）+ 稀疏日过滤（S2: min_symbols=3，当日不足整日空仓）。

**效果**（QA 独立复跑逐字节一致）：
| 口径 | 全样本 Sharpe | OOS Sharpe | 结论 |
|---|---|---|---|
| 旧版（全表 rank） | 0.484 | -0.192 | 伪影 |
| **S2 修复版** | **0.594** | **-0.337** | 结构性正确，OOS 如实暴露 |
| 纯引擎 B（基准） | 0.927 | **1.260** | — |

- 修复后全样本/IS 改善（IS 1.162→1.309），但 OOS 如实恶化——**引擎 A 瓶颈在信号端（覆盖率+校准），非排序口径**
- **组合重估**：S2 A25/B75 OOS 0.812 < 旧版 0.940（旧版是伪影）；**纯 B OOS 1.260 > 任何含 A 组合**——当前信号形态下引擎 A 是 OOS 负贡献者
- **主理人终裁**：① 采纳 S2（拒绝回退旧版）② 引擎 A 降权 ≤15% 或暂以纯 B 运行 ③ 权重决策一律用 S2 口径

### 9.8 P6 生产配置固化 + 覆盖率诊断（QA VERIFIED ×2）

**P6-1 生产配置固化**（`hexbroker/config.py` +32 行，向后兼容）：
- `EngineBConfig`：enabled/win=252/thr=0.70/notional_frac=0.20
- `ComboConfig`：w_engine_a=0.15 / w_engine_b=0.85 / vol_target=False（P5/P4-1 终裁）
- 验证：28 断言 PASS + 回归 20 passed（QA 独立复跑一致）

**P6-2 引擎A覆盖率诊断**（三重根因，QA 独立复现）：
1. 结构性截断：walk_forward 每折测试窗 60 天 → cal_split=0.5 只输出后半 ~16 天/折 → 覆盖率仅 ~25%
2. 折叠相位漂移（主因）：折叠按品种整数索引 + 数据缺口不同 → 低谷月恰是缺口品种（2022/23 cu0/i0/rb0、2024/25 i0）
3. 2026 尾部真空：15/18 品种数据止于 2026-02-24，仅 au0/ag0/m0 到 08-14

**修复方案**：A（推荐）`_calibrate_and_split` 返回全部信号 → 覆盖率翻倍（~1900 天），交易信号零泄漏（QA 审查通过）；B 补数据治 2026 真空；C 缓存重建。
**主理人终裁**：P6-3 = 信号缓存重建（A+C）；P6-4 = 数据补齐（B）

### 9.9 P6-3 信号缓存重建（QA VERIFIED 数字 + 归因修正）

**实现**：`cal_return_all=True` 模式（前 k 拟合校准器、全部应用）→ v3 缓存 15,407 条（+94%）、唯一日 1,464（+50%）、日均品种 10.52。

**重估（QA 独立复现）**：引擎 A S2 OOS -0.337→**-0.437**；组合 A15/B85 0.999→**0.962** → **v3 不采纳**。

**QA 归因修正（重要）**：
- ❌ 工程师"新增日信号质量差"不成立（新增日命中率 48.4% 更优）
- ✅ 真因 = **既有日截面稀释 + 暴露翻倍**（v3∩v2日期 OOS -0.454）；引擎 A 根子在模型信号端（exp_ret OOS IC -0.26），非覆盖率

**附带发现**：生产环境校准器**从未执行**（31 条信号 → k=15 < 20 阈值 → scaler=None）→ 建议测试窗 60→120+ 或调低校准最小样本阈值。

**生产基线维持**：v2 缓存 + w_a=0.15/w_b=0.85/vol_target=False（组合 OOS 0.999）；纯 B OOS 1.260 最强单引擎；v3 保留作候选。

### 9.10 P7 引擎A信号端修复（QA VERIFIED + 统计检验）

**根因（P7-1）**：引擎 A OOS 失效 = 分组信号 OOS 翻转——agri（IS +0.133→OOS -0.190）、precious（+0.025→-0.186）严重翻转；ferrous/industrial 稳定；chem_energy 改善。全样本分年 IC 本就不稳定（仅 2020/21 显著）。

**修复对比（P7-2，7 方案）**：S4（更频繁重训 test_len=30，重训节奏翻倍 9→19 折）唯一单引擎 OOS 转正（-0.337→+0.053）、组合 OOS 0.999→1.138；但 **QA 块自助检验 p≈0.49 不显著**、改善主因降波动（0.744→0.527 %/d）、单引擎滚动胜率仅 43.7%、rt30 覆盖率更低（-24% 行数）。

**主理人终裁**：
1. **S4 不直接投产**，仅影子验证候选（fresh-OOS 复核 + 漂移监测后再决）
2. **生产维持 v2 缓存 + A15/B85（组合 OOS 0.999）**；纯 B OOS 1.260 最强
3. S1b 不采纳（IS 噪声冠军，IS Δ=+0.012 < 决策阈值）
4. 引擎 A 信号端结论：**参数级修复（加权/剔除/翻转/重训频率）均无法稳健逆转 OOS 负 alpha**，需深层改造（特征体系/模型结构/多模型融合），列远期

## 10. 已知限制与后续方向

| 限制 | 说明 | 方向 |
|---|---|---|
| OOS 绝对收益低（年化 0.64%） | OOS 段贵金属牛市下截面信号间歇失效 | 引擎 B 已补位（OOS 年化 10.5%） |
| 外盘特征 2025+ 缺失 | spx/uup 数据到 2024-12 | wind/westock 落盘工程 |
| RL 无稳健 OOS alpha | 已定位辅助定位 | 状态增强/更大种群/多分段交叉 |
| 绝对年化受名义 30% 限制 | 杠杆放大被构成偏差证伪 | 品种池再扩展 + 等名义修正 |
| p0 负 IC 未修复 | 缺产业特征（产地/库存/生柴） | 产业数据源接入后复入 |
| 引擎 B 全样本 MaxDD -38% | 高胜率低盈亏比（盈亏比 0.83） | 组合权重/止损精细化 |
| 仓单因子无效（日度+周度） | 日度/周度 IS/OOS 反号、方向不稳定 | 确认否决，不再尝试 |
| 基差动量无增量 | f2 5d/10d 嵌套 OOS IC≈0 | 维持引擎B 单因子基线 |
| 引擎 A 跨时间 rank 遗留 | exp_ret.rank 全表排序非按日截面 | P3 层面修复 + 重估组合 |
| 基差数据 2018 年中才开始 | AG/AU 基差 2018-06 起、SC 尾部 2026-04 | 补充早期数据源 |
| ~~K 线覆盖率缺口~~ | ~~15/18 品种止于 2026-02-24~~ | ✅ P6-4 已补齐（2026-08-17 全覆盖） |

### 9.11 P6-4 数据补齐（QA VERIFIED 可验收）

**完成**：PandaData `get_future_daily_post`（close_pcr 后复权）26 段 / 2420 交易日全部拉取并合并，**闭合率 100%**，18 品种全部延伸至 **2026-08-17**（2022 CU/RB/I、2024 CU/RB + AG/AU/M 历史缺口全部闭合）。

**关键口径修复**：au0/ag0/m0 既有序列是**未复权原始口径**，新拉是 close_pcr 后复权 → `RAW_SCALE_FIX` 换算（m0×0.3168、au0×1.2596、ag0×1.4505，QA 独立核算 514 重叠日偏差 0.000000%）。**该三品种口径为未复权**，后续拉取/拼接需沿用此常量。

**QA 验证**：2420 行逐行核对 0 mismatch、seam 全部 <5%、非缩放品种 0 污染、备份完整（artifacts/backup_p64/ 32 个）。

**意义**：P6-3 方案 A 的 B 条件（补数据）已满足 → 可重跑信号缓存（消除 2026 尾部真空），重跑后按 P6-3 结论复核引擎 A/组合。

### 9.12 P6-3b 信号缓存 v4 重建（QA VERIFIED，采纳）

**重建**：数据补齐后重跑 `group_modeling_v2.py`（与 v2 完全同口径，只换数据）→ **v4 缓存 `signals_cache18_grouped_v4.parquet`（8,624 行/18 品种）为生产信号缓存**，v2 保留回归基线。

**覆盖率全面改善**（QA 独立复现）：2026 日均 2.24→13.09、2022-2025 全部翻倍（5.7-8.0→~13.0）、2026 尾部信号 0→181、<5 品种天数 52.5%→27.5%。

**引擎 A/组合重估**（target 逐行一致）：
| 配置 | v2 OOS | v4 OOS | Δ |
|---|---:|---:|---:|
| 引擎 A S2 (min=3) | -0.155 | **-0.024** | +0.131 |
| 组合 A15/B85 | 1.360 | **1.384** | +0.024 |
| 纯 B 参考 | — | **1.622** | — |

**关键定论**：归因验证（OOS 截断 ≤06-11：v2 -0.166 vs v4 -0.023）→ 改善主要来自覆盖率密度；**引擎 A 单引擎 OOS 仍负（-0.024）→ 瓶颈确认在信号质量（特征/模型），非覆盖率**。生产组合维持 A15/B85；引擎 A 信号质量迭代为最高优先级。

**记录在案**：fold 结构性截断（末折止于 index 2057/2093 → 2026-06-30~08-17 约 32 交易日无信号），v2/v4 同约束公平对比，组合由引擎 B 兜底不阻塞。

### 9.13 P6-5 v4 生产配置固化（QA VERIFIED，生产 READY）

**固化**：`EngineAConfig` 挂载 BacktestConfig（signal_cache=v4 / top_k=0.30 / min_symbols=3 / notional_frac=0.20）；`engine_a_targets_cs` 默认 cache_path=None 时读配置 v4（显式覆盖优先、异常兜底 v2）。

**QA 验证**：42 项断言全 PASS、pytest 全量 228 passed、旧 yaml 5 份全部兼容、yaml 覆盖生效；**内容级证明**：无 cache_path 输出 662 天 == v4 缓存天数（实读 v4），显式 v2 → 976 天（向后兼容）。v2/v4 缓存与数据文件未被触碰。

**Sentinel-2 v1.0 生产基线（最终确认）**：
```
引擎 A：v4 缓存 + top_k 0.30 + S2 min=3（按日截面 rank）
引擎 B：win252 / thr0.70 / 名义 20%
组合：A15/B85 / vol_target=False → OOS Sharpe 1.384
```

### 9.14 P8 引擎A信号质量迭代（QA VERIFIED + 根因深化）

**P8-1 诊断**：引擎 A 特征纯量价（technical+microstructure+iterative+cross+normalize），无基本面特征 → 嫌疑"缺基差特征"。

**P8-2 实验**（加入基差特征重训 v5，QA 零泄漏检查 PASS、精确复现）：
- 引擎 A S2 OOS **-0.024 → -0.335 恶化**（f_basis* 合计重要性 9-18% 模型确实用上，但无法转化截面排序质量）
- **QA 根因深化（核心）**：v5 exp_ret 截面 IC（-0.040）优于 v4（-0.063）但 P&L 恶化 → 问题在"信号→P&L"层；**f_basis_ratio_rank 截面 IC=-0.059**——基差是品种内时序信号（引擎 B 时序 IC +0.025），天生无截面排序力
- **定论**：**训练任务（LightGBM 时序预测 exp_ret）与推理用途（每日截面选 top30% 品种）错配**——引擎 A 瓶颈不是缺特征，是任务/用途错配

**主理人终裁**：v5 不采纳（生产维持 v4 + A15/B85 OOS 1.384）；基差特征模块保留默认关闭（hexbroker/feature/fundamental.py，11 测试通过）；**重构方向=标签/训练目标截面化**（直接训练截面排序目标而非绝对未来收益）；方法论：后续实验先做目标用法维度单特征 IC 预检、引擎 B 时序信号不直接作为引擎 A 截面特征。

### 9.15 P8-3 标签截面化重构（QA VERIFIED + 度量假象修正）

**实现**：`label_mode` 三变体（cross_rank 当日截面分位 / cross_z 当日 z-score / cross_demean 当日去均值），默认 absolute 逐字节不变；零泄漏 QA 验证（只用当日品种值、与折边界无关、退化处理正确）。

**结果**（QA 独立复现）：
| 缓存 | A-S2 OOS Sharpe | OOS 收益 | 组合 OOS | exp_ret 截面 IC(OOS) |
|---|---:|---:|---:|---:|
| v4 absolute 基线 | -0.024 | -1.99% | 1.384 | -0.063 |
| v6 cross_rank | -0.389 | -8.15% | 1.381 | **+0.035** |
| v7 cross_z | +0.004* | -1.63%* | 1.399 | -0.041 |

*⚠️ **QA 修正**：v7 "OOS 转正"是算术日均假象，**复利口径仍亏损 -1.63%**（"亏得少一点"）；全样本 Sharpe 0.682→0.418、MaxDD -19.7%→-37.7%。

**关键发现**：① v6 恶化根因=跨组尺度错配（单品种组 m0 绝对标签 vs 多品种组 rank → m0 入选 0%、2 品种组占做多 79.9%）；② 标签截面化方向**验证有效**（同基差基线上 cross_z 拉到 +0.004，标签是活性成分）但跨组可比性未解决；③ **IC 改善≠盈利**。

**主理人终裁**：v4 维持生产；v7 作候选不进生产；**后续最高优先级=全 18 品种统一截面**（根治组规模偏置）；方法论：引擎 A 评估用复利口径（非算术日均 Sharpe）。

### 9.16 P8-4 全品种统一截面（QA VERIFIED，v8 采纳为生产缓存）

**实现**：`label_pool="all"`（全 18 品种统一截面，根治 P8-3 跨组尺度错配；默认 group 逐字节兼容）；修复 union 空洞 bug（2208→8624 条，加回归单测）。

**结果**（QA 独立复现，复利口径）：
| 指标 | v4 (absolute) | v7 (组内 z) | **v8 (全品种 z)** |
|---|---:|---:|---:|
| 引擎 A OOS 复利 | -1.99% | -1.63% | **+8.68%** ✅ |
| 引擎 A OOS Sharpe | -0.024 | +0.004 | **+0.626** |
| 全样本 Sharpe/MaxDD | 0.682/-19.7% | 0.418/-37.7% | **0.867/-26.1%** |
| 组合 A15/B85 OOS | 1.384 | 1.399 | **1.602** |
| OOS 分段 | 一负一正 | 一负一正 | **双段均正** |

**组规模偏置根治**：m0 入选天数 27.3%（接近 top30% 公平份额）、2 品种组 79.9% 病理消失（最大组 22.7%）。

**QA 风控条件（采纳前提）**：① OOS 截面 IC 仍负（-0.064），盈利来自做多正漂移**主要靠黑色系敞口**（jm0/zn0/rb0/j0/hc0/i0）→ 黑色系转弱需预警；② m0 恢复是公平性修复非 alpha（m0 OOS realized 负）；③ 双变体选择偏差已用全样本/OOS 双指标 + 分段稳定性缓解。

**主理人终裁**：**v8 采纳为生产信号缓存**（EngineAConfig.signal_cache=v8）；风控监测：黑色系敞口占比 + OOS 截面 IC 持续跟踪；复利口径为唯一评估口径。

### 9.17 P9 组合级验证 + 黑色系敞口风控（QA VERIFIED 附条件）

**组合网格**（12 配置，复利口径，QA 逐位复现）：vol=N 最优 **A10/B90**（OOS 1.612 vs 基线 A15/B85 1.602，零漂移复现）；全网格最优 A10/B90+vol=Y（1.662/+43.79%）但 OOS MaxDD -8.1% 更深。⚠️ QA 修正：A10/B90 是网格**下边界**最优（OOS Sharpe 随 w_a 单调降、全样本单调升）→ 真正最优可能 <0.10。

**黑色系敞口 + group_cap**：黑色系每日选中占比 OOS mean 26.5%（>50% 16 天）；group_cap=0.5 消除极端集中（>50%→0 天）、OOS 性能中性。⚠️ QA 修正：cap 性能增量对 **refill 规则敏感**（两变体 0.646 vs 0.656），只能当**纯风控**（风险降低稳健、性能中性），不能当性能增强。

**配置固化**：ComboConfig **A10/B90** / vol_target=False；EngineAConfig + `group_cap=None`（默认不启用）。

**主理人终裁**：① A10/B90 固化（先验一致 + B 主导低风险，标注"与 A15/B85 等价因先验选 A10"）；② vol=Y 维持候选不翻转 P4-1（需专项验证）；③ **group_cap 保留为杠杆**——缺 group_map 配置（默认 GROUPS_V2 分两组无法实现"合并黑色系≤50%"），下轮加 group_map 后 yaml 显式启用；④ 遗留：git 提交、group_map 落地、A10 边界补测（A5/A0）。

### 9.18 P10 group_map 落地 + A10 边界补测（2026-08-20）

**group_map 配置落地（QA 阻塞项闭环）**：
- `EngineAConfig.group_map: dict[str,str] | None = None`（yaml 可覆盖）；`_resolve_group_map/_resolve_group_cap` 解析优先级：显式传入 → 读 config → 兜底 GROUPS_V2/不启用
- `configs/base.yaml` 启用：`group_cap: 0.5` + `group_map` 18 键（**i0/j0/jm0/rb0/hc0 → ferrous_all 合并黑色系**）
- pytest 208 passed；QA 静态审查 + 复跑全 VERIFIED

**A10 边界补测（QA 要求）**：A0/B100 OOS 1.622（纯 B，下界最优）> A5/B95 1.618 > A10/B90 1.612（生产）> A15/B85 1.602——OOS 随 w_a 单调改善但**全样本反向**（A0 0.972 < A10 1.023）、OOS MaxDD 略深 → **QA 裁决 ComboConfig 维持 A10/B90 不更新**（增量小 + 全样本代价 + A0 边界 overfit 嫌疑）。

**⚠️ 部署接线标准（DISCREPANCY-1 处置）**：无参 `load_config()` 不加载 base.yaml（向后兼容）→ **生产部署必须显式加载 + 显式传参**：
```python
cfg = load_config("configs/base.yaml")                     # 显式加载部署配置
engine_a_targets_cs(prices,
    group_cap=cfg.backtest.engine_a.group_cap,  # 0.5
    group_map=cfg.backtest.engine_a.group_map)  # 18 键 ferrous_all
```

---
*文档版本：v3.13（2026-08-20，Sentinel-2 P10 group_map+边界补测）｜ 关联报告：deliverables/software-hexfutures-ai/（40+ 份实验报告）*
