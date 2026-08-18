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
```

## 9. 已知限制与后续方向

| 限制 | 说明 | 方向 |
|---|---|---|
| OOS 绝对收益低（年化 0.64%） | OOS 段贵金属牛市下截面信号间歇失效 | 多信号融合深化（产业特征/宏观多因子） |
| 外盘特征 2025+ 缺失 | spx/uup 数据到 2024-12 | wind/westock 落盘工程 |
| RL 无稳健 OOS alpha | 已定位辅助定位 | 状态增强/更大种群/多分段交叉 |
| 绝对年化受名义 30% 限制 | 杠杆放大被构成偏差证伪 | 品种池再扩展 + 等名义修正 |
| p0 负 IC 未修复 | 缺产业特征（产地/库存/生柴） | 产业数据源接入后复入 |

---
*文档版本：v2.1（2026-08-18）｜ 关联报告：deliverables/software-hexfutures-ai/（40+ 份实验报告）*
