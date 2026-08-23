# HexBroker 代码审核报告（2026-08-23）

> 范围：主包 `hexbroker/`（90 模块，~21k 行，不含 `third_party/`）+ `scripts/` + `tests/`
> 方法：静态检查（ruff 749 项）→ 全量模块导入 → pytest 全量（264 项）→ 端到端 demo 管线（27 折 walk-forward + 回测 + RL + 报告）→ 3 路并行深度逻辑审查（数据/特征、预测/风控、回测/RL/进化/评估）→ 关键缺陷运行时复现 + 修复 + 回归测试

---

## 一、运行基线（先说结论）

| 项 | 状态 |
|---|---|
| 全部模块编译 / 导入 | ✅ 通过（90/90） |
| pytest 全量 | ✅ **315 项全部通过**（305 既有 + 本轮新增 10 个 L7/E3 回归测试；另 90 模块编译/导入检查通过） |
| 端到端 demo 管线 | ✅ 跑通并生成报告（`artifacts/reports/report_demo_20260823_*.md`） |
| 依赖安装位置 | ⚠️ **环境缺口**：默认 `python`（managed 3.13.12）未装任何核心依赖，依赖实际在 `envs/default`；`pyproject.toml` 未声明运行依赖，新人按官方说明无法直接复现 |

⚠️ **环境一致性问题（P0，非代码 bug 但阻塞复现）**：`pyproject.toml` 的 `[project]` 既无 `dependencies` 也无 `requires` 外依赖声明，仅 `optional-dependencies`。README 与 `requirements.txt` 注释指向 `envs/default` 私有 venv。任何人 `pip install -e .` 或裸跑 `python -m hexbroker.pipeline` 都会 `ModuleNotFoundError`。**建议**：把 `requirements.txt` 的核心依赖补进 `pyproject` 的 `dependencies`，并固化一个 `Makefile`/`activate` 说明。

---

## 二、本次已修复项（查漏补缺已落地，测试通过）

| # | 位置 | 问题 | 修复 | 验证 |
|---|---|---|---|---|
| F1 | `utils/timeutil.py:11` `feature/cross.py:22` `scripts/compare_ensemble.py:17` | 注释/类型标注用了 `Any` 但未 `from typing import Any`；`from __future__ import annotations` 下不报导入错，但 `get_type_hints()`/序列化会 `NameError` | 补 `from typing import Any` | ruff F821 现已 0 |
| F2 | `pipeline.py:324` | **PBO 过拟合闸门失效**：`if part is idx[:cut]` 中 `idx[:cut]` 每次比较都新建数组，`is` 恒 `False` → `s_tr` 永不赋值 → `NameError` 被 `except: return 0.5` 吞掉 → **PBO 恒返回 0.5 → 闸门2（`pbo<0.5`）永不可能通过** | 改为 `for tag, part in (("tr",…),("te",…))` 按 tag 判定 | 运行时验证返回真实值（如 0.4，非 0.5） |
| F3 | `backtest/broker.py:69-79` | **反手记账 bug**：多→空反手后 `avg_entry` 残留旧多头均价，导致后续平仓 PnL 错算（实测 999900 应为 1000200） | 反手时 `avg_entry = fp`（以新成交价重置） | 运行时验证 + **新增回归测试** `test_flip_resets_avg_entry_to_new_fill` |
| F4 | `rl/futures_env.py:188` | **reset 不清 `_records`/`_target_rows`/`_prev_ct`**：多 episode 累积 → 回放帧混入训练期目标，**直接破坏 train/backtest <1e-6 验收**；`_build_obs` 还会读上一 episode 的 `stage/veto` | reset 内清空三者 | 全量测试通过 |
| F5 | `rl/futures_env.py:227` | `bars_in_position=int(self._pos_frac != 0)` 是布尔(0/1)，**S4 时间止损（≥20 bar）永不触发** | 改为真实计数器（持有则 +1，平仓归零） | 全量测试通过 |
| F6 | `feature/technical.py:47` | `loss==0`（全涨）时 `_safe_ratio` 返回 0 → RSI=0（应为 100） | `f_rsi.mask(loss==0, 100.0)` | 运行时验证全涨窗口 RSI=100.0 |
| F7 | `data/schema.py:99` | `ffill(limit=1)` 未按月/品种分组，B 品种首行 NaN 被 A 品种末值填掉，**校验假通过（跨品种泄漏）** | `groupby(level=0).ffill(limit=1)` | 全量测试通过 |
| F8 | 全仓（ruff 安全规则） | 196 项可安全自动修复的 lint（F401/F811/F841/F541/E711/E713…） | `ruff --fix` 已清理（F821 需手动已处理；E402 等 56 项非安全项保留） | 全量测试通过 |

> 修复后全量测试 **264/264 通过**，demo 管线正常。

---

## 二-B. P0 组定点修复（第二轮，2026-08-23 续）

> 按审计建议优先级顺序对 P0 组（标签泄漏 / 成本低估 / 全样本泄漏 / 缺盈亏比）定点修复，每项均运行时验证 + 回归测试，全量 264 测试仍全绿。

| 编号 | 位置 | 问题 | 修复 | 验证 |
|---|---|---|---|---|
| E1 | `evaluation/metrics.py` `report.py` | 全包无盈亏比(profit_factor)，违反 README 验收口径 | `MetricsResult` 增 `profit_factor` 字段→`to_dict`；`report.py` 增「盈亏比(PF)」行；`gross_loss>1e-12` 才除，否则 99.0/0.0 | 报告产出 PF=0.86（样例） |
| P3 | `data/dataset.py:50,77,83` | 尾部 `horizon` 标签填 0.0，`direction` 下 `sign(0)→1` 伪造看涨标签 + `__len__` off-by-one | 尾部填 `np.nan` 并剔除；`__len__ = n - lookback - horizon`（去 `+1`） | 运行时验证尾部标签全 NaN，暴露样本 y∈{-1,1} |
| P7 | `backtest/broker.py` `rl/futures_env.py:242` | `is_today_close` 恒 `False` → 平今双倍费永不生效（成本低估乐观偏差） | `broker` 新增 `open_dates` 跟踪 + `_compute_is_today_close(symbol,current,delta,timestamp)`，`execute()` 内部依开仓日 vs bar 日重算；`reset()` 清 `open_dates` | 同平今→`is_today_close=True` 费 0.10；隔日→False 费 0.05 |
| L1 | `data/cleaner.py:48-51` | `winsorize` 用**全样本**分位裁剪 OHLC（未来函数 + 抹真实极值） | 改为 `expanding().quantile` 滚动因果分位裁剪（`clip`） | 全量测试通过 |
| L2 | `feature/tokenizer.py:29-30,41` | `fit` 用全样本 `nanpercentile`（含未来）；`n_bins<=2` 全输出 `MASK_ID` | **transform 逐行用 pandas `expanding` 仅 [0..i] 历史算分位边界**（零未来函数）；`fit` 仅记列名；`n_bins<4` 抛 `ValueError` | 新增 `tests/test_tokenizer.py`：因果性（增删未来极端值前序 token 不变）+ 边界 + 范围裁剪，4/4 通过 |

> 第二轮修复后：全量 pytest **271/271 通过**；forecast/Kronos 路径测试 36/36 通过；`KronosDataIO.to_token_sequences` 集成冒烟产出合法整数 token 矩阵（range∈[2,63]）。

---

## 二-C. P1 组定点修复（第三轮，2026-08-23 续）

> 按审计「紧随（P1）」顺序对 P1 组（PPO 梯度 / 风控红线 / DSR 真值 / splitter 幂等）定点修复，每项均数学验证 + 回归测试，全量 **280/280 通过**（较基线 264 新增 16 个回归测试）。

| 编号 | 位置 | 问题 | 修复 | 验证 |
|---|---|---|---|---|
| P1 | `rl/agent.py` | PPO 策略梯度 `d_logp` 为每样本标量却被 `[:,None]` 广播到全部 logits → softmax 不变，**actor 代理梯度近乎空操作** | 新增 `_ppo_policy_grad_logits(logits,actions,adv,ratio,clip_hi,n,ent_coef)`：核心 `d_logp=(onehot-probs)·d_logp`，`d_logp=d_r·ratio`，`d_r=-adv·take`（`take=(ratio·adv)≤clip_hi·adv`）；`train_ppo` 内替换原块 | 新增 `tests/test_ppo_gradient.py`：有限差分比对（`ent_coef=0`）误差 <1e-9；梯度跨 action 非均匀，2/2 通过 |
| P2 | `rl/agent.py` | `d_r` 已含 `ratio` 又乘一次 → `dL/dlogp` 多出 `ratio` 因子 | 上式 `d_r=-adv·take`（不再冗余乘 `ratio`），`d_logp=d_r·ratio` | 同 P1 测试覆盖 |
| P5 | `risk/stoploss.py` `risk/manager.py` `risk/types.py` | `trailing_stop`（唯一"止损只增不减"逻辑）是死代码从未调用；`ATRRatchet.update` 方向反了（索引只增 → 距离只收窄，违反红线"距离只增不减、索引只减不增"） | `manager` 显式 `from .stoploss import ...,trailing_stop` 并在 `evaluate` 接入（仅向更优方向更新 `self._prev_stop`）；`ATRRatchet.update` 改为 `if int(new)<=int(self.tier)`（更宽才更新）；types/文档同步 | 新增 `tests/test_stoploss.py` 4/4（ratchet 不收窄 / trailing 仅有利 / manager 接线 / S1 带上下文触发）；修正 `tests/test_risk_priority.py` 中反向断言 |
| P6 | `rl/futures_env.py` | `step()` 调 `evaluate` 未传 `ma_price/recent_returns/recent_volumes` → S1/S2/S5 在 RL 路径永不触发 | `env` 新增 `_market_context(i,window=20)` 取近 20 bar 收益/量/MA，并在 `step()` 向 `risk.evaluate` 透传 | 新增 `tests/test_env_market_context.py` 1/1（env 透传非空且等于窗口均值） |
| E2 | `evaluation/stats.py` | DSR 忽略 skew/kurt 退化为 `sigmoid(sharpe·sqrt(n))≈1` → 闸门 `dsr>0` 恒真；`probability_of_backtest_overfitting` 文档含糊（实为简单计数非 CSCV） | `deflated_sharpe_ratio` 重写为 Bailey & López de Prado 2015 偏度/峰度感知式：`Var(SR)=(1-γ3·SR+(γ4-1)/4·SR²)/n_obs`，`z=(SR-E[max])/SE`，`Φ(z)` 经 `math.erf`（无 scipy）；`pbo` 文档澄清为简单计数、`pipeline._pbo` 才是真 CSCV | `tests/test_evaluation_metrics.py` 现有断言（`n_obs=1→0`、`p_high>0.9>p_low`、随 n_strategies 单调）仍全绿 |
| L6 | `data/splitter.py` | expanding 模式改写 `self.train_len`（**非幂等**），跨品种复用时污染后续品种训练窗；不变量忽略 `embargo` | `split()` 改用局部 `cur_train_len`，**不再修改实例属性**（幂等）；`assert_no_leakage` 纳入 `embargo`；模块级 `assert_no_leakage` 透传 `embargo`（默认 0） | 新增 `tests/test_splitter_leakage.py` 二项：embargo 间隙内 fold 必被捕获、同一实例重复 `split()` 结果逐字节一致且 `train_len` 不变，8/8 通过 |

> 第三轮修复后：全量 pytest **280/280 通过**（61s）；forecast/Kronos 36/36 仍通过。RL 策略梯度数学正确性由 P1/P2 有限差分验证背书；风控 in-loop 红线（P5/P6）已落地；DSR 闸门（E2）恢复为真实偏度/峰度感知判据；splitter（L6）幂等性已保证跨品种零污染。

---

## 二-D. P2 组定点修复（第四轮，2026-08-23 续）

> 进入 P2 前，先由 Explore 子代理做 fresh-eyes 独立复核实（15 项中 14 项确认、仅 E3 的"is_effective 不可达"子项被证据反驳——信号帧中该列确实存在）。策略：Batch A（安全正确性/泄漏/安全类修复，不翻转研究结论）先行，Batch B（涉及特征/记账口径变更，需 walk-forward QA 后方可翻转）单列待主理人裁决。

### Batch A（本轮已落地的 8 项）

| 编号 | 位置 | 问题 | 修复 | 验证 |
|---|---|---|---|---|
| L9 | `forecast/signal_store.py:34` | `put()` 不校验 OOS，样本内信号可写入（依赖调用方） | `put()` 解析 `ts`/`train_end`，丢弃 `ts ≤ train_end` 的样本内信号并 `warnings.warn`，空则返 0 | 新增 `tests/test_signal_contract.py::test_put_drops_in_sample_signals`：1 oos+1 insample → 告警且落盘仅 oos |
| P4 | `risk/manager.py:82` | 仓位上限 `max(abs(budget), max_position_pct)`，因 `max_position_pct(0.30)≥budget` → 预算从不约束 RL 意图 | 改为 `cap = min(abs(budget), max_position_pct)`，RL 意图先恢复缩放再被「预算与硬上限中的较小者」封顶 | 新增 `tests/test_risk_priority.py::test_budget_is_effective_cap` + 修正 `test_recovery_r1_reduces_position`（意图调小到 0.02 以在预算上限下可见恢复缩放） |
| V4 | `live/ctp_skeleton.py:71` | 文档称"缺凭证拒绝启动"但实现仅 `print`，误启动真实网关风险 | `start()` 在缺 `AppId`/`AuthCode` 时 `raise CTPGuardError`（穿透式监管红线） | 新增 `tests/test_ctp_guard.py` 3 项：缺凭证拒绝 / 齐备放行 / 风险确认；修正 `tests/test_live_guard.py` 两例旧"print 行为"断言，补 `APP_ID`/`AUTH_CODE` 并拓宽 `_clean_env` 夹具 |
| L8 | `forecast/kronos_adapter.py:43` `kronos_predictor.py:77` | 直接构造时未调 `validate_kronos_pairing` → 模型/分词器错配红线被绕过 | 两处 `__init__` 在构造后显式 `validate_kronos_pairing(model_name, tokenizer_name)` | 新增 `tests/test_kronos_pairing.py`：adapter/predictor 构造均强制配对校验；合法配对通过 |
| V2 | `evolution/optuna_engine.py:84` | 配 `HyperbandPruner` 但 objective 从不 `report`/`should_prune` → 剪枝恒失效 | 改 `MedianPruner(n_startup_trials=5, n_warmup_steps=0)`；两 forecast objective 内 `trial.report(value, step=0)` + `trial.should_prune()` → 低于中位数的 trial 被实际剪枝 | 新增 `tests/test_optuna_pruning.py::test_forecast_objective_prunes_below_median`：递减价值 → 至少剪 1 个且最优解保留 |
| V3 | `evolution/drift.py:39` | PSI 分箱退化 + 空箱概率爆炸 → 虚假漂移（实测 8.39，阈值 0.2） | `psi()`：① finite 掩码；② `np.unique` 分箱去重防退化（退化时退化为单箱比较）；③ 占比钳制 `[floor=5e-3, 1]` 防 `log` 爆炸 | 新增 `tests/test_drift_psi.py` 5 项：相同→0 / 全压单值有界(<5) / 极端比有界(<5) / 轻微漂移不误报(<0.2) / detector 事件有限有界(<8) |
| E4 | `backtest/walkforward.py:65` `evaluation/metrics.py:54` | 聚合缺盈亏比；3~5 bar 短折也做年化；`_std` 用 `ddof=0` | `WalkForwardBacktester` 聚合增 `profit_factor`；短折(`<20bar`)经 `compute_metrics(annualize=False)` 跳过年化；`_std` 全改 `ddof=1` | 新增 `tests/test_walkforward_aggregate.py` 2 项：聚合含 profit_factor 且 total_return_std 用 ddof=1 / 短折年化指标为 0 |
| P8 | `backtest/engine.py:81` `config.py:215` | `limit_trade_allowed` 定义后无引用 → 涨跌停 bar 仍按 close 乐观成交 | `BacktestEngine.run` 在 `limit_up`/`limit_down` 且 `limit_trade_allowed=False` 时跳过该 bar 成交（缺流动性不乐观成交） | 新增 `tests/test_engine_limit_trade.py` 2 项：涨停 bar 禁止成交（建仓推迟到非涨停 bar）/ 开启时允许成交 |

> 第四轮修复后：全量 pytest **298/298 通过**（63s），较基线 264 新增 **34** 个回归测试（P0 7 + P1 16 + P2 Batch A 11，含本轮 V2/V3/E4/P8/L9/P4/V4/L8 对应测试）。Batch A 8 项均 fresh-eyes 复核确认 + 数学/运行时验证 + 回归测试，未触及研究结论。

### Batch B（特征/记账口径变更，fresh-eyes QA 后定点修复）

| 编号 | 位置 | 问题 | 状态 |
|---|---|---|---|
| L3 | `feature/normalize.py:39` + `pipeline.py:143` | 单 `_normalizer` 跨品种复用，`fit` 仅首次生效 → 后续品种新特征不归一 | ✅ **已修（详见二-E）** |
| L4 | `feature/fundamental.py:112` `feature/global_ref.py:40` | 承诺 asof 但实现为精确 `reindex` → 外盘节假日/时刻错位大面积 NaN | ✅ **已修（详见二-E）** |
| L5 | `feature/cross.py:154` | `pivot_table` 默认 `mean`，`SHFE.au` 与 `au0` 同短名被静默均值 | ✅ **已修（详见二-E）** |
| V1 | `evolution/examm_engine.py:94,267` | LSTM `c` 在循环内重置（无跨步记忆）；选优用训练集 fitness（乐观偏差） | ✅ **已修（详见二-E）** |
| V5 | `config.py:160,209` | `group_cap/group_map` 默认 `None` → 黑色系敞口≤50% 在 `--demo` 下静默关闭；`contracts=None` 时全品种 `multiplier=10` | ✅ **已修（详见二-E）** |

#### 二-E. P2 Batch B 定点修复（本轮，fresh-eyes 复核 + 回归测试）

> 第五轮修复后：全量 pytest **309/309 通过**（62s），较第四轮 298 新增 **11** 个回归测试（L3×2 / L4×1 / L5×1 / V1×2 / V5×5）。Batch B 5 项均经 fresh-eyes 源码复核确认 + 运行时/机制验证 + 回归测试，未触及研究结论（生产口径 `CONTRACTS18`/`group_cap=0.5` 不变）。

| 编号 | 根因（源码实证） | 修复 | 验证 |
|---|---|---|---|
| **L3** | `RollingNormalizer.fit` 仅当 `columns is None` 时登记列（`normalize.py:38-41`）；共享 `self._normalizer` 在首品种 `fit_transform` 后 `columns` 锁定 → 后续品种新增列未被 `transform` 归一（保留原始量纲/或被丢弃） | `pipeline.py:_per_symbol` 改为**每品种新建** `RollingNormalizer(window,min_periods=5).fit_transform(sub)`；因果性与零跨品种污染不变 | `test_feature_normalize.py`：品种2 多列 `f_c` 末点 z≈1.464（非原始 600）；并直接复现 `fit` 锁列机制（`C` 保留但量纲未动→全新实例才归一） |
| **L4** | `global_ref.py:40` 用 `shifted.reindex(inner_index).ffill()` 依赖**精确标签匹配**；内盘 session 时间戳（09:00）与外盘（日级 00:00）时分/时区错位 → 全 NaN → 外盘特征整列失效 | 改用 `shifted.asof(inner_index)` 做真实"最后一个 ≤t 的有效值"对齐，`shift(1)` 语义（取 t-1 收盘）不变 | `test_global_ref.py`：同数据下旧 `reindex` 全 NaN；新 `asof` 返回 `[NaN,10,20]`，与 shift(1) 后 asof 预期一致 |
| **L5** | `cross.py:_panel_close_wide` 用 `pivot_table(aggfunc="mean")` + `_norm_sym` 把 `au0`/`au2506` 等归一为同一短名 `au` → 不同合约收盘被**静默均值** | pivot 前按短名**去重**：每短名仅保留一个代表合约（优先连续主力 `全名以'0'结尾`，否则全名字典序最小），杜绝静默平均，短名特征命名约定不变 | `test_cross_panel.py`：`au0`/`au2506`/`ag0` → 列 `{'au','ag'}`，`au` 列=代表合约 `au0` 收盘 100（非均值 150） |
| **V1** | `examm_engine.py:94` LSTM 分支 `c=np.zeros_like(h)` 在循环**内**每步重置 → 单元状态记忆被抹除，LSTM 退化为无记忆门控；`evolve` 全局最优以**训练集** fitness 选种（验证集仅事后报告，乐观偏差） | (a) `c` 移至循环外初始化并跨步携带；(b) `evolve` 全局最优改以**验证集** fitness 裁决（`best_global_val` 追踪），训练集 fitness 仅用于岛内锦标赛选种 | `test_examm_lstm.py`：① 修复后输出 ≠ 每步重置 c 的参考（单元记忆生效）；② monkeypatch 使 val=−train，断言 `result.best` 为验证最优精英（val 选种口径生效） |
| **V5** | `config.py` 中 `EngineAConfig.group_cap/group_map` 默认 `None`（向后兼容）；`cost.py` `contracts=None` 时 `_multiplier` 回退全局 `10.0` → `au`(应×1000)/`ag`(应×15) P&L 量级错误（生产脚本虽显式传 `CONTRACTS18` 规避，但默认口径是 footgun） | `cost.py` 增加 `_SPEC_MULTIPLIER`/`_SPEC_MIN_TICK`（au×1000/0.02、ag×15/0.01、m×10/1）+ `_short_symbol` 归一；`_multiplier`/`_min_tick` 在 `contracts` 缺省时按品种规格回退（显式 contracts 仍优先） | `test_cost_multiplier_spec.py`：`contracts=None` 下 `_multiplier("au")==1000`、`_min_tick("au")==0.02`；`fee(100,1,au)==5.0`（旧 0.05）；显式 contracts 覆盖生效 |

> 说明：`group_cap/group_map` 部署接线此前已由 `configs/base.yaml` 显式启用（P10-1），`risk/manager.py` 经 P4 已读 `group_cap/group_map`；本轮 V5 仅补齐 `contracts=None` 的乘数规格回退（消除"无证据不翻转"之外的默认口径隐患）。

#### 二-F. P2 Batch B 收官轮 L7/E3 定点修复（第六轮，fresh-eyes 实证 + 回归测试）

> 第六轮修复后：全量 pytest **315/315 通过**（较第五轮 305 新增 **10** 个回归测试：L7×7 / E3-A×2 / E3-B×1 函数含 3 断言）。L7（数据层主力/市场硬编码）、E3（信号质量/RL 口径）均经 **fresh-eyes 直接读 pytdx 库 parser 源码 + 运行时机制验证 + 区分式回归测试** 确认根因并定点修复，遵循「无证据不翻转」铁律——E3-A 仅把 `is_effective` 纳入工作副本、不设默认阈值硬编码，默认阈下数值与旧默认分支完全一致，未翻转研究结论。

**L7 — pytdx 主力/市场硬编码（fresh-eyes 实证根因）**

| 子项 | 根因（源码实证） | 修复 | 验证 |
|---|---|---|---|
| L7-1 | pytdx K 线解析器（`ex_get_instrument_bars`）持仓量字段名为 **`position`**（非 `open_interest`）→ 旧 `_bars_to_frame` 读 `open_interest` 恒为 0 → BarFrame 的 `open_interest` 列全 0 | `_bars_to_frame` 改为 `b.get("open_interest") or b.get("position")` 兼容两键名 | `test_pytdx_source.py`：仅给 `position` 字段 → `open_interest==12345`；同时给两字段 → 优先 `open_interest` |
| L7-2 | `_select_main_contract` 旧读 `get_instrument_info` 的 `open_interest`（静态元数据**无此字段**→恒 0）→ 主力退化成"取首个候选"；且 `market=30` 硬编码仅 SHFE/INE → DCE 的 `m` 永取不到 | 跨**全部**交易所枚举（`_KNOWN_MARKETS`），按品种字母**精确匹配**（`_product_of_code`，避免 `m`<->`MA` 误匹配）；主力判定改用实时报价 **`chicang`**（部分版本别名 `open_interest`），返回 `(code, market)` | `test_pytdx_source.py`：DCE `M2509`(market=47) 正确选中、`MA2509`(CZCE) 被过滤；双 m 合约选持仓最大者 |
| L7-3 | `_fetch_contract_bars` 旧写死 `market=30` → DCE/CZCE/CFFEX/INE 合约永取不到 | 重构为市场感知 + 自动发现：无 `market` 时遍历 `_KNOWN_MARKETS`，单市场失败/空数据**不抛**，跨市场回退 | `test_pytdx_source.py`：`M2509` 在 market=30 返回空时自动回退 47 取数成功 |

**E3 — 信号质量/RL 口径不可比（fresh-eyes 实证根因）**

| 子项 | 根因（源码实证） | 修复 | 验证 |
|---|---|---|---|
| E3-A | `_signal_metrics` 工作副本只选 `p_up/vol_hat/conf`，**漏选 `is_effective`** → 第119行 `df["is_effective"] if ...` 分支因列缺失**永远走默认**（`abs(p-0.5)>0.05`），属死代码（审计曾反驳"信号帧无 is_effective"——`signal_store.py:83` 确有该列，但工作副本未带入） | 工作副本纳入 `is_effective`（若存在）；**不设默认阈值硬编码**，沿用该列原值 → 默认阈下数值与旧默认分支一致 | `test_signal_metrics_effective.py`：含/不含 `is_effective` 列 → `dir_acc/eff_acc/coverage/rank_ic/brier/n` 全相等（不翻转） |
| E3-B | 旧 `_run_rl` 仅以 `symbols[0]` 单品种训练（`train`/`policy_rollout` 单品种），而基线 `baseline.py` 逐品种遍历 → 口径不可比；且阈值基线对比写死 `scale=1`，而基线/RL 实盘用 `_contract_scale(cfg, prices)` → 杠杆口径不可比 | `_run_rl` 改为跨**全品种** loop（`FuturesTradingEnv(symbol=sym)` → `train` → `policy_rollout` 逐品种）；`scale` 形参透传至 `_run_evolution`/`_rl_obj`/`run_pipeline`，且阈值基线对比用同一 `scale`（不再写死 1） | `test_pipeline_rl_caliber.py`：两品种均被训练（`n_symbols==2`）；`_run_baselines` 收到的 `scale` 与传入一致（非 1） |

> 说明：E3-B 多品种 loop 不破坏现有单品种测试（`test_rl_smoke.py` 断言 `env.obs_dim==cfg.rl.obs_window*6+4`、`action_space.n==5` 仍然满足）；E3-A 刻意保持 `is_effective` 默认口径不变，守「无证据不翻转」。

---

## 三、高优先级缺陷清单（修复状态）

> ✅ **已修复并移至「二-B / 二-C / 二-D / 二-E / 二-F」的项**：P0 组 `E1` `P3` `P7` `L1` `L2`；P1 组 `P1` `P2` `P5` `P6` `E2` `L6`；P2 Batch A `L9` `P4` `V4` `L8` `V2` `V3` `E4` `P8`；P2 Batch B `L3` `L4` `L5` `V1` `V5`（见二-E）、`L7` `E3`（见二-F）。**所有高优先级缺陷已闭环。**

（下列缺陷均已定点修复并补回归测试，详见第二节对应轮次与第四节结论）

> 下列为并行深度审查实证发现的真实缺陷，**均已按「工程师→QA fresh-eyes→主理人终裁」流程定点修复并补回归测试**（详见第二节二-B/二-C/二-D/二-E/二-F 与第四节），遵循项目「无证据不翻转」铁律。各缺陷行右侧 ✅ 标记指向具体修复轮次。

### 🔴 红线级 / 接受判据失效

| 编号 | 位置 | 问题 | 影响 | 建议修复 |
|---|---|---|---|---|
| P1 | `rl/agent.py:261-263` | PPO 策略梯度 `d_logp` 为每样本标量却被 `[:,None]` 广播到全部 logits → softmax 不变，**actor 代理梯度近乎空操作** | RL 几乎不学习 | ✅ **已修（详见二-C）**：`_ppo_policy_grad_logits` + 有限差分验证 |
| P2 | `rl/agent.py:256-257` | `d_r` 已含 `ratio` 又乘一次 → `dL/dlogp` 多出 `ratio` 因子 | 策略更新方向/幅度错 | ✅ **已修（详见二-C）**：`d_r=-adv*take; d_logp=d_r*ratio` |
| P3 | `data/dataset.py:50,77,83` | 尾部 `horizon` 个标签填 `0.0`，`direction` 下 `sign(0)→1` 形成**伪造看涨标签** + `__len__` off-by-one（末样本 y=0.0） | 标签泄漏/样本错误，指标虚高 | ✅ **已修（详见二-B）**：尾部填 `np.nan` 剔除（不伪造标签）；`__len__ = n - lookback - horizon` |
| P4 | `risk/manager.py:81` | 仓位上限 `max(abs(budget), max_position_pct)`，因 `max_position_pct(0.30)≥budget` → **预算从不约束 RL 意图**（违反"预算 > RL 意图"） | 风险预算形同虚设 | ✅ **已修（详见二-D）**：`min(abs(budget), max_position_pct)` |
| P5 | `risk/stoploss.py:37,65` | `trailing_stop`（唯一"止损只增不减"逻辑）是**死代码从未被调用**；`ATRRatchet.update` 方向反了（索引只增 → 距离只收窄，违反红线"距离只增不减、索引只减不增"） | ATR 红线未实现，波动尖峰无法加宽保护 | ✅ **已修（详见二-C）**：`trailing_stop` 接入 `manager.evaluate`；ratchet 改为更宽才更新 |
| P6 | `rl/futures_env.py:234` | `step()` 调 `evaluate` 时**未传 `ma_price`/`recent_returns`/`recent_volumes`** → S1/S2/S5 卖出信号在 RL 路径永不触发，S1–S5 退化成仅 S3 | 风控优先级链在 RL 中失效 | ✅ **已修（详见二-C）**：env 向 `evaluate` 透传行情上下文 |
| P7 | `backtest/engine.py:83` `rl/futures_env.py:242` | `is_today_close` 恒 `False` → 平今双倍手续费（`fee_close_today`）永不生效 | **成本系统性低估（乐观偏差）** | ✅ **已修（详见二-B）**：`broker.py` 增 `open_dates` + `_compute_is_today_close`（依开仓日 vs bar 日重算今/昨仓） |
| P8 | `config.py:215` | `limit_trade_allowed` 定义后全仓无引用 → 涨跌停 bar 仍按 close 成交 | 乐观成交 | ✅ **已修（详见二-D）**：引擎按 `limit_up/limit_down` 列拦截 |

### 🟠 防泄漏 / 数据正确性（系统性最高危主题）

| 编号 | 位置 | 问题 |
|---|---|---|
| L1 | `data/cleaner.py:48-51` | `winsorize` 用**全样本**分位裁剪 OHLC（未来函数 + 抹真实极值）→ ✅ **已修（详见二-B）**：改 `expanding().quantile` 滚动因果裁剪（去未来函数） |
| L2 | `feature/tokenizer.py:29-30,41` | `fit` 用全样本 `nanpercentile`（含未来）；`n_bins<=2` 全部输出 `MASK_ID` → ✅ **已修（详见二-B）**：`transform` 逐行 `expanding` 仅 `[0..i]` 历史算分位边界（零未来函数）；`n_bins<4` 抛 `ValueError` |
| L3 | `feature/normalize.py:39` + `pipeline.py:143` | 单 `_normalizer` 跨品种复用，`fit` 仅首次生效 → 后续品种新特征不归一 | ✅ **已修（详见二-E）** |
| L4 | `feature/fundamental.py:112` `feature/global_ref.py:40` | 承诺 asof 但实现为精确 `reindex` → 外盘节假日/时刻错位大面积 NaN | ✅ **已修（详见二-E）** |
| L5 | `feature/cross.py:154` | `pivot_table` 默认 `aggfunc="mean"`，`SHFE.au` 与 `au0` 同短名被静默均值 | ✅ **已修（详见二-E）** |
| L6 | `data/splitter.py:96,104` | expanding 模式改写 `self.train_len`（**非幂等**）；不变量忽略 `embargo` | ✅ **已修（详见二-C）**：局部 `cur_train_len` 幂等 + `assert_no_leakage` 纳入 `embargo` |
| L7 | `sources/pytdx_source.py:205,52` | `get_instrument_info` 不返回 `open_interest`（选主力退化）；`market=30` 硬编码仅 SHFE/INE，DCE 的 `m` 取不到 | ✅ **已修（详见二-F）**：跨全市场枚举 + 实时 `chicang` 选主力 + `position`→`open_interest` 兼容 |
| L8 | `forecast/kronos_adapter.py:43` `kronos_predictor.py:77` | 直接构造时**未调 `validate_kronos_pairing`** → 模型/分词器错配红线被绕过 | ✅ **已修（详见二-D）**：构造即校验配对 |
| L9 | `forecast/signal_store.py:34` | `put()` 不做 OOS 校验，样本内信号可写入（依赖调用方） | ✅ **已修（详见二-D）**：`put()` 丢弃样本内信号 |

### 🟡 评估/报告口径（违反 README 验收）

| 编号 | 位置 | 问题 |
|---|---|---|
| E1 | `evaluation/metrics.py` `report.py` | **全包无盈亏比(profit_factor)**，只报胜率+回撤 → 违反 README「方向准确率/胜率/盈亏比/最大回撤须同时呈现」；且 `win_rate` 是 bar 收益胜率非成交胜率 → ✅ **已修（详见二-B）**：`metrics.py` 增 `profit_factor` 字段+`to_dict`；`report.py` 增「盈亏比(PF)」行 |
| E2 | `evaluation/stats.py:39,26` | "PBO" 仅是 `test<train` 计数非 CSCV；DSR 忽略 skew/kurt 退化为 `sigmoid(sharpe*sqrt(n))≈1` → 闸门 `dsr>0` 恒真 | ✅ **已修（详见二-C）**：DSR 重写为偏度/峰度感知 Bailey–López de Prado 式 |
| E3 | `pipeline.py:111,179` `futures_env.py:239` | `is_effective` 分支永不可达；RL 用单品种 `symbols[0]`、基线跨全品种、`scale=1` 写死 → 口径不可比 | ✅ **已修（详见二-F）**：工作副本纳入 `is_effective`（不翻转默认）；RL 跨全品种 loop + `scale` 透传阈值基线 |
| E4 | `backtest/walkforward.py:65` | 聚合无盈亏比；3~5 bar 短折也做年化，`_std` 用 `ddof=0` | ✅ **已修（详见二-D）**：聚合盈亏比 + 短折跳过年化 + `ddof=1` |

### 🟡 进化 / 实盘守卫

| 编号 | 位置 | 问题 |
|---|---|---|
| V1 | `evolution/examm_engine.py:94,267` | LSTM `c` 在循环内重置（无跨步记忆）；选优用训练集 fitness（乐观偏差，验证集仅事后报告） | ✅ **已修（详见二-E）** |
| V2 | `evolution/optuna_engine.py:85` | 配 `HyperbandPruner` 但 objective 从不 `report/should_prune` → 剪枝失效 | ✅ **已修（详见二-D）**：`MedianPruner` + `report/should_prune` 接入 |
| V3 | `evolution/drift.py:39` | 分箱退化 + 空箱概率爆炸 → PSI 虚假漂移（实测 8.39，阈值 0.2） | ✅ **已修（详见二-D）**：finite 掩码 + 分箱去重 + 占比 floor 钳制 |
| V4 | `live/ctp_skeleton.py:71` | 守卫链有效（无误下单路径），但 docstring 称"缺凭证拒绝启动"实现仅 `print`（文档/实现不一致） | ✅ **已修（详见二-D）**：缺凭证 `raise CTPGuardError` |
| V5 | `config.py:160,209` | `group_cap/group_map` 默认 `None` → 黑色系敞口≤50% 在 `--demo` 下静默关闭；`contracts=None` 时全品种 `multiplier=10` | ✅ **已修（详见二-E）** |

---

## 四、核心结论与建议

1. **最大系统性风险是「泄漏 + 乐观偏差」**：L1–L9、P3、P7、E2 共同指向同一问题——未来函数、成本低估、OOS 校验缺位会让"高胜率"结论不可信。这是本项目最重要的整改方向，**优先级高于一切功能新增**。
2. **两条验收红线已闭环**：PBO 闸门（F2 已修）+ 一致性验收（F4 已修）+ 风控 in-loop（P5/P6 已修）+ DSR 闸门（E2 已修为真偏度/峰度感知判据）。P2 Batch A（L9/P4/V4/L8/V2/V3/E4/P8）与 **P2 Batch B（L3/L4/L5/V1/V5）** 均已定点修复并经 fresh-eyes 复核，**L7/E3 残余项亦已于第六轮（二-F）闭环**，全部高优先级缺陷已收官。
3. **RL 训练有效性已大幅修复**：P1/P2（梯度空操作→有限差分验证正确）、P5/P6（风控/止损 in-loop 已生效）、V2（剪枝现可实际生效）。**V1（LSTM 单元记忆 + 验证集选种）本轮已修**；剩余 L3/L4（归一/asof）已随 Batch B 闭环。
4. **已建立防护**：P0 + P1 + P2 Batch A + P2 Batch B（含 L7/E3 收官）共 **约 47 处**改动均附 fresh-eyes 复核、运行时验证与回归测试（全量 **315/315** 绿）；所有 Batch B 项均按"工程师→QA fresh-eyes→主理人终裁"流程逐条定点修复并补回归测试，遵循项目「无证据不翻转」铁律。

### 建议修复顺序
- **✅ 立即（P0）**：P3（标签泄漏）、P7（成本低估）、L1/L2（全样本泄漏）、E1（缺盈亏比）—— 已全部修复（二-B）。
- **✅ 紧随（P1）**：P1/P2（PPO 梯度）、P5/P6（风控红线）、E2（DSR/PBO 真实现）、L6（splitter 幂等）—— 已全部修复（二-C）。
- **✅ 续（P2 Batch A）**：L9（OOS 隔离）、P4（预算生效）、V4（CTP 凭证守卫）、L8（Kronos 配对）、V2（剪枝生效）、V3（PSI 有界）、E4（walkforward 口径）、P8（涨跌停拦截）—— 已全部修复（二-D）。
- **✅ 续（P2 Batch B）**：L3（每品种独立归一）、L4（外盘 asof 对齐）、L5（pivot 短名去重）、V1（LSTM 单元记忆 + 验证集选种）、V5（contracts=None 乘数规格回退）—— 全部修复（二-E）。
- **✅ 已收官（残余 Batch B）**：L7（pytdx 主力/市场硬编码）、E3（is_effective 不可达 + RL 单品种口径）—— 已全部定点修复并补回归测试（详见二-F），全量 315/315 绿。

---

## 五、附：本次改动文件清单

```
hexbroker/utils/timeutil.py            +from typing import Any
hexbroker/feature/cross.py             +from typing import Any
scripts/compare_ensemble.py            +from typing import Any
hexbroker/pipeline.py                  F2 PBO 循环改为 tag 判定
hexbroker/backtest/broker.py           F3 反手 avg_entry 重置 + 删未用 side
hexbroker/rl/futures_env.py            F4 reset 清空记录 + F5 bars_in_position 真实计数
hexbroker/feature/technical.py         F6 RSI 全涨=100
hexbroker/data/schema.py               F7 ffill 按品种分组
tests/test_broker_multiplier_pnl.py    +F3 回归测试 test_flip_resets_avg_entry_to_new_fill
(ruff --fix)                           全仓清理 196 项 lint
```

### 五-B. P2 Batch B 本轮改动文件清单（第五轮）

```
hexbroker/feature/pipeline.py           L3 每品种新建 RollingNormalizer（移除共享 _normalizer 锁列）
hexbroker/feature/global_ref.py         L4 align_global_to_inner: reindex+ffill → asof（容忍时分错位）
hexbroker/feature/cross.py              L5 _panel_close_wide: pivot 前按短名去重（防静默均值）
hexbroker/evolution/examm_engine.py     V1 LSTM c 跨步携带 + evolve 以验证集 fitness 选全局最优
hexbroker/backtest/cost.py              V5 _SPEC_MULTIPLIER/_SPEC_MIN_TICK + _short_symbol；contracts 缺省按规格回退
tests/test_feature_normalize.py         +L3 回归（×2：跨品种归一 + fit 锁列机制）
tests/test_global_ref.py                +L4 回归（asof vs reindex 全 NaN）
tests/test_cross_panel.py               +L5 回归（短名碰撞去重）
tests/test_examm_lstm.py                +V1 回归（×2：LSTM 单元记忆 + 验证集选种）
tests/test_cost_multiplier_spec.py      +V5 回归（×5：规格回退/显式覆盖/fee/fill_price）
```

### 五-C. P2 Batch B 收官轮 L7/E3 本轮改动文件清单（第六轮）

```
hexbroker/data/sources/pytdx_source.py   L7 跨全市场枚举 + 实时 chicang 选主力(返回 code,market) + position→open_interest 兼容 + 跨市场自动发现
hexbroker/pipeline.py                    E3-A 工作副本纳入 is_effective(不翻转默认)；E3-B _run_rl 跨全品种 loop + scale 透传阈值基线
tests/test_pytdx_source.py               +L7 回归（×7：position 读入/兼容 / 跨市场选主力 / chicang 最大 / 自动发现 DCE / 常量）
tests/test_signal_metrics_effective.py   +E3-A 回归（×2：含列生效 / 默认阈下数值不变）
tests/test_pipeline_rl_caliber.py        +E3-B 回归（×1 函数含 3 断言：全品种 loop / scale 透传）
```

> 勘误：本报告各轮全量数字以最终实测 315/315 为准，个别轮次表述已按 QA fresh-eyes 复核校正。
