# 撮合假设清单（P0-1，≥14 条）

> 每一条假设均标注【内容 / 默认值 / 影响方向（乐观·保守·中性）/ 对应代码位置】。
> 影响方向以「相对真实市场成交」为参照：乐观 = 高估策略绩效；保守 = 低估；中性 = 无明显方向。
> 程序化结构检查见 `tests/test_matching_assumptions.py`（PRD A1.1）。

| ID | 内容 | 默认值 | 影响方向 | 代码位置 |
|---|---|---|---|---|
| MA-01 | 成交时点：逐 bar 收盘时点依据前向填充目标判定是否调仓并成交 | 同 bar close 判定 | 乐观 | `BacktestEngine.run` / `hexbroker/backtest/engine.py` |
| MA-02 | 成交价基准：以 bar `close` 作成交参考价（滑点另行叠加） | `close` 列 | 乐观 | `BacktestEngine.run`（marks 取值） |
| MA-03 | 滑点：每笔按 1 个最小变动价位向不利方向叠加 | `slippage_ticks=1.0` | 保守 | `CostModel.fill_price` / `hexbroker/backtest/cost.py` |
| MA-04 | 手续费：开仓/平仓 0.005%，平今（当日平仓）0.010% | `fee_rate_open/close=0.00005, fee_rate_close_today=0.00010` | 中性 | `CostModel.fee` / `hexbroker/backtest/cost.py` |
| MA-05 | 保证金：名义价值 × 12% 用于估值口径（无逐笔预算硬约束） | `margin_rate=0.12` | 中性 | `CostModel.margin` / `hexbroker/backtest/cost.py` |
| MA-06 | 涨跌停拦截：涨跌停 bar 且 `limit_trade_allowed=False` 时禁止以 close 成交 | `limit_trade_allowed=False` | 保守 | `BacktestEngine.run`（P8） |
| MA-07 | 成交量约束：默认不启用（`volume_cap=None`，假设流动性无限） | `volume_cap=None` | 乐观 | `ExecutionConfig` / `hexbroker/backtest/execution.py` |
| MA-08 | 超量处理：`partial` 按比例部分成交（`delta ≤ bar.volume × cap`），剩余丢弃不追单；`reject` 整单拒绝（delta 置 0，不自动重试） | `volume_cap_mode="partial"` | 保守 | `cap_order_qty` / `hexbroker/backtest/execution.py` |
| MA-09 | 下一 bar 执行：开启时成交价取下一 bar `open` ± 滑点，无下一 bar 跳过成交 | `next_bar_execution=False` | 保守 | `next_bar_ref_price` / `BacktestEngine.run` |
| MA-10 | 开盘跳空：以 bar `open` 作为跳空参考（开启 next_bar 口径时） | `open` 列 | 中性 | `next_bar_ref_price` / `hexbroker/backtest/execution.py` |
| MA-11 | 平今判定：以当前净持仓整体开仓日 == 平仓 bar 日判平今（双倍手续费） | `open_dates` 跟踪 | 中性 | `SimBroker._compute_is_today_close` / `hexbroker/backtest/broker.py` |
| MA-12 | 复权口径：价格已由 `ContractStitcher` 后向复权（换月跳空消除） | `adjust_method="backward"` | 中性 | `hexbroker/data/contract.py` / `configs/base.yaml` |
| MA-13 | 数据对齐：bar 时间戳以「结束时刻」标注、区间左闭右开；多品种共享时间轴 | `freq` 语义 | 中性 | `hexbroker/data/schema.py` |
| MA-14 | 订单类型：市价单——按参考价 ± 滑点全额成交（无限价/排队/冰山） | 市价单 | 乐观 | `SimBroker.execute` / `hexbroker/backtest/broker.py` |
| MA-15 | 资金约束：无逐笔资金/保证金预算硬约束（仅做 mark-to-market 估值） | 无约束 | 乐观 | `SimBroker` / `hexbroker/backtest/broker.py` |
| MA-16 | 跨 bar 依赖：单 bar 决策一次，不跨 bar 连锁追单（`reject` 后不自动重试） | 单 bar 决策 | 中性 | `BacktestEngine.run` / `hexbroker/backtest/engine.py` |

## 汇总说明

- 默认口径合计：**5 项乐观（MA-01/02/07/14/15）+ 3 项保守（MA-03/06/09）+ 8 项中性**。
  核心偏差来源是「同 bar 收盘价成交 + 无成交量约束」（MA-01/02/07），
  `next_bar_execution` / `volume_cap` 双开关即用于量化该偏差（见 `run_dual_caliber`）。
- 切换保守口径（`next_bar_execution=True`）后，MA-01/MA-02 变为「决策 bar 收盘判定、下一 bar 开盘成交」，
  与 freqtrade 默认 next-bar-open 对齐。
- 撮合假设仅影响回测撮合层；SignalStore OOS 物理隔离与双闸门判定逻辑零改动。
