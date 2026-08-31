# c0 信号接入落点清单（P2-1）

> 用途：明确 c0 品种「0 成交」的根因与接入外部信号源的具体落点。**本文档仅记录结论与操作清单，不编码、不伪造信号。**

## 1. 结论

- **c0 当前 0 成交是「外部信号源缺失」导致的硬阻塞，不是代码缺陷。**
- 模拟盘已有的机制已经闭环：
  - `symbols.c0.mode = accumulate`：c0 前 30 个交易日内仅做日线积累（盘中跟踪 OHLC、收盘落 `c0_daily.csv`），不新开仓；满 `accumulate_days=30` 后由调度器自动切到 `trade` 模式（`scheduler._effective_mode`）。
  - 主源信号缺失时，`SignalEngine` 回退到**技术兜底**，但技术兜底 `is_effective=False`（仅作降级方向提示，不构成 edge）。配合 P0-3「无持仓禁开」硬约束：`RiskGate._intent` 对 `is_effective=False` 返回 0 → 不触发成本门禁、不开新仓（审计 P1-1/P1-2 闭合）；有持仓则仅做风控管理。
- 因此「0 成交」是整个设计在正常数据依赖下的**预期行为**，无需改代码即可消除——只要外部信号源就绪。

## 2. 外部跑批需要产出的数据

外部批处理（如 `mx-ds-mcp` / 离线建模产出）需要生成一份**含 `c0` 行**的信号缓存 parquet：

| 要求 | 说明 |
| --- | --- |
| 文件格式 | parquet（与现有 `signal_caches` 同格式） |
| 列 schema | 对齐 v8：`symbol, ts, p_up, exp_ret, is_effective`（其余列按需补充，主源以这 5 列校验） |
| 必含行 | 至少包含 `symbol == "c0"` 的有效行（`is_effective=True`、`ts` 为交易日当天） |
| 新鲜度 | `freshness_threshold_trading_days=0`（**交易日历 lag** 口径，P3-C 2026-08-31）下，信号日须等于**最近一个已收盘交易日**（标准 T+1，lag=0）。**周末与假期后首日无需特殊刷新**——周五收盘信号在周一盘中 lag=0，正常放行；只有缓存真的停更（lag≥1）才过期 |

**建议路径落点**：将这份 parquet 加入 `configs/paper.yaml` 的 `signal_caches` 列表**首位**（作为主源，优先级最高）。当前 `configs/paper.yaml:19-21` 为：

```yaml
  signal_caches:
    - artifacts/signals_cache18_grouped_v8_tail_ext.parquet   # 主源（覆盖至 2026-08-24）
    - artifacts/signals_cache18_grouped_v8.parquet            # 兜底（止 2026-06-29）
```

新增后形如：

```yaml
  signal_caches:
    - artifacts/signals_cache_c0_v8.parquet        # 主源（含 c0 行，运维注入）
    - artifacts/signals_cache18_grouped_v8_tail_ext.parquet
    - artifacts/signals_cache18_grouped_v8.parquet
```

## 3. SignalEngine 加载（无需改代码，仅配置/数据）

- `SignalEngine` 按 `signal_caches` 顺序级联加载，首位即主源。
- 主源就绪后：
  - `latest_signal("c0")` 自动从主源取数（返回有效 `SignalFrame`）；
  - `has_symbol("c0")` 返回 `True`；
  - 技术兜底不再是 c0 的唯一信号源，`is_effective` 可为真，驱动正常开仓。
- **代码侧零改动**，仅依赖配置（列表顺序）与数据（parquet 内容）就绪。

## 4. `symbols.c0.mode` 决策（运维决定）

- **默认保持 `accumulate`**：前 30 个交易日积累日线（第 31 日自动切 `trade`），与现有机制一致，风险最低。
- 若外部信号源已就绪、且希望 c0 立即进入完整交易：可将 `symbols.c0.mode` 改为 `trade`。
- 该切换由**运维**按数据就绪情况决定，不属于本迭代的代码改动范围。

## 5. 阻塞标记（数据依赖，非代码缺陷）

- 在 c0 信号源就绪之前：`latest_signal("c0")` 仍无有效主源 → 技术兜底 `is_effective=False` → `RiskGate._intent` 返回 0 → **永不新开**。
- 这是明确的「数据依赖」阻塞，应在复盘/监控中标注为「等待外部信号源」，而非代码 bug。
- 信号源就绪（§2 parquet 就位 + §3 配置生效）后，上述链路自动贯通，c0 即按 `mode` 进入交易。

## 6. 接入核对清单（Checklist）

- [ ] 外部跑批产出含 `c0` 行的 v8 schema parquet（`symbol, ts, p_up, exp_ret, is_effective`）
- [ ] parquet 加入 `configs/paper.yaml` 的 `signal_caches` **首位**
- [ ] 每日开盘前刷新缓存即可（交易日历 lag 口径下，**周一/假期后首日无特殊要求**：周五信号在周一使用 lag=0，属标准 T+1，正常放行）
- [ ] 常规交易日（周二~周五）盘中信号为上一交易日收盘特征所生成，lag=0 ≤ 阈值 0，正常放行
- [ ] ⛔ 口径防回潮：若历史上曾按「自然日差」判定，周五→周一会算成 fd=3 而被误拦（全历史实测误拦 448/2097 = 21.36%）。现口径见 `hexbroker/paper/signals.py::_trading_lag`，验证脚本 `scripts/verify_freshness_caliber_options.py --full-history`
- [ ] （可选）`symbols.c0.mode` 由 `accumulate` 改为 `trade`（运维决定）
- [ ] 验证：`SignalEngine.has_symbol("c0") == True` 且 `latest_signal("c0").is_effective == True`
- [ ] 监控：c0 不再因「信号缺失」保持 0 成交（属数据就绪后的正常交易）
