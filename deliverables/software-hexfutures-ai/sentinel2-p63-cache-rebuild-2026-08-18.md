# P6-3 信号缓存重建报告（覆盖率翻倍 + 引擎A重估）

**日期**：2026-08-18 ｜ **流程**：工程师实现 → QA fresh-eyes 复核（数字 VERIFIED，归因修正）→ 主理人终裁

## TL;DR

| 项目 | 结论 |
|---|---|
| 实现 | `cal_return_all` 模式上线（前 k 拟合校准器 + 全部应用），v3 缓存重建 15,407 条（+94%）、唯一日 1,464（+50%）、日均品种 10.52（+29%） |
| 引擎 A 重估 | v2 OOS -0.337 → v3 OOS **-0.437（恶化）**；组合 A15/B85：0.999 → **0.962（下降）** |
| **QA 归因修正** | ❌ 工程师"新增日信号质量差"**不成立**（新增日命中率 48.4% 更优）；✅ 真因 = **既有日截面稀释**（v3∩v2日期 OOS -0.454 < v2 -0.337）+ 暴露翻倍 |
| 附带发现 | **生产环境校准器从未执行**（31 条信号 → k=15 < 20 阈值 → scaler=None）→ v3 = v2 全部行 + 未校准前半行，exp_ret 逐字节相同 |
| **主理人终裁** | **v3 不采纳为生产缓存**，维持 v2 + w_a=0.15/w_b=0.85/vol_target=False；v3 保留作候选 |

---

## 一、实现（最小改动，向后兼容）

| 文件 | 改动 |
|---|---|
| `scripts/refine_lightgbm_champion.py` | `_calibrate_and_split` 新增 `cal_return_all=False`：前 k 拟合、全部应用（`_apply_calibrator_one` 仅 transform 不 refit）；`_train_eval_fold` / `walk_forward_lightgbm` 双路径透传 |
| `scripts/group_modeling.py` / `group_modeling_v2.py` | `--cal-return-all` + `--output` 参数透传 |
| `scripts/p5_engineA_cross_section.py` | `engine_a_targets_cs` 增加 `cache_path` 参数 |
| `tests/test_refine_lightgbm.py` | +4 单测（True 返回全部 / False 返回 k: / 校准器只前 k 拟合 / exp_ret 零泄漏）→ 8 passed |
| `scripts/p6_3_rebuild_signal_cache.py` | 编排脚本（verify/rebuild/coverage/eval 分段） |

## 二、覆盖率对比（v2 vs v3）

| 指标 | v2 | v3 | 变化 |
|---|---:|---:|---:|
| 总行数 | 7,952 | **15,407** | +94% |
| 唯一信号日 | 976 | **1,464** | +50% |
| 日均品种 | 8.15 | 10.52 | +29% |
| <5 品种天数 | 52.5% | 36.8% | -15.7pp |

## 三、引擎 A + 组合重估（QA 独立复现）

| 配置 | v2 OOS Sharpe | v3 OOS Sharpe | 变化 |
|---|---:|---:|---:|
| 引擎 A S2 (min=3) | -0.337 | **-0.437** | -0.100 |
| 组合 A15/B85（生产配置, vol=N） | **0.999** | 0.962 | -0.037 |
| 组合 A25/B75 | 0.812 | 0.753 | -0.059 |
| 纯 B（参考） | **1.260** | — | — |

## 四、QA 独立分析（关键修正）

### 4.1 恶化真因（DISCREPANCY vs 工程师归因）
```
[v3∩v2日期] OOS = -0.454  （同 976 天，仅截面稀释 → 比 v2 -0.337 更差 -0.117）
[v3 全量]   OOS = -0.437  （新增 488 天边际 +0.017）
```
- **v3 恶化主因 = 既有日截面稀释 + 交易暴露翻倍**（OOS 做多行 382→731）
- 不是"新增日拖累"——QA 独立验证新增日选中 fwd5 -0.00367（不更差）、命中率 48.4%（更好）

### 4.2 校准器空转（附带发现）
- 60 日测试窗 → lookback 30 → 仅 31 条信号 → k=15 < 20 阈值 → `scaler=None`
- **生产环境校准器从未执行**：v3 公共键 p_up 与 v2 逐字节相同；exp_ret 零改动 → 引擎 A（只用 exp_ret.rank）零影响

### 4.3 唯一日 1,464 < 预估 1,900 机制
- 单品种平均仅覆盖 45%（每折 lookback 消耗 31/60），18 品种并集 77.3% → 日历高度对齐使缺口重叠

## 五、主理人终裁

1. **v3 不采纳为生产缓存**：组合 OOS 0.962 < 0.999 明确变差（QA 与工程师一致）
2. **归因修正采纳**：问题不是"新增日质量差"，而是**引擎 A 自身 OOS 负 alpha**（选中 fwd5 -0.004、IC -0.26）+ v3 截面稀释放大暴露——引擎 A 的根子在模型信号端（exp_ret 在 OOS 无预测力），不在覆盖率
3. **v3 保留作候选**：它是合法 OOS 信号集（零泄漏），待引擎 A 信号修复或数据补齐后复评
4. **登记 QA 附带发现**：生产环境校准器空转（cal_split=0.5 下测试窗太短）→ 建议后续将测试窗 60→120+ 或调低校准最小样本阈值，否则 p_up/is_effective 从未真正校准
5. **生产基线维持**：v2 缓存 + w_a=0.15/w_b=0.85/vol_target=False（组合 OOS 0.999）；纯 B OOS 1.260 仍为最强单引擎

## 六、文件清单

| 文件 | 说明 |
|---|---|
| `scripts/refine_lightgbm_champion.py` | +cal_return_all 模式（向后兼容） |
| `scripts/group_modeling.py` / `group_modeling_v2.py` | +参数透传 |
| `scripts/p5_engineA_cross_section.py` | +cache_path 参数 |
| `scripts/p6_3_rebuild_signal_cache.py` | 编排脚本 |
| `tests/test_refine_lightgbm.py` | +4 单测（8 passed） |
| `artifacts/signals_cache18_grouped_v3.parquet` | 15,407 条候选缓存（保留） |
| `artifacts/p6_3_cache_coverage.csv` / `p6_3_engineA_compare.csv` | 对比数据 |
| `artifacts/_qa_backup_p63/` | QA 复核证据 |
| `docs/developer-guide.md` | 更新至 v3.4（§9.9） |
