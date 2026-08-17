# RL 奖励逐日化报告（2026-08-17）

> **任务（P0）**：修复 RL 训练奖励的 5 日归因幻觉——`SentinelTradingEnv` 奖励改为当日收益（fwd 1 日 mark），与 BacktestEngine 逐 bar 口径一致，重训验证。
> **结论**：**奖励逐日化有效**——BacktestEngine 完整口径 OOS：年化 0.41%→1.04%、Sharpe 0.22→0.35，证明训练-执行口径对齐方向正确。

---

## 一、修改

`hexbroker/rl/sentinel_env.py`：
```python
# 前：fwd = closes.shift(-5) / closes - 1.0   （5 日归因，重叠窗口放大）
# 后：fwd = closes.shift(-1) / closes - 1.0   （当日收益，与 BacktestEngine 逐 bar 一致）
```

## 二、结果（OOS 2024-07-18~2026-08-14 真新数据，BacktestEngine 完整口径）

| 训练口径 | 年化 | 回撤 | Sharpe |
|---|---:|---:|---:|
| 5 日归因（前） | +0.41% | -5.1% | 0.22 |
| **奖励逐日化（后）** | **+1.04%** | **-10.4%** | **0.35** |
| 规则基线同段 | -0.13% | -3.1% | -0.13 |

**改善**：年化 ×2.5、Sharpe +59%——训练目标与执行口径对齐后，策略在真实逐 bar 执行下确实更好。

## 三、进化适配（自动重搜权重）

新奖励尺度（1 日收益 ~2bp vs 5 日 ~10bp）下，进化自动收敛到 **w=[0.458, 0.189, 2.409]**（G3 精英，valid 口径 Sharpe 1.98）——低 pnl 权重 + 高趋势惩罚，与规则基线的"重趋势过滤"精神一致。

## 四、诚实分级

1. ✅ **P0 修复有效**：口径对齐改善真实执行水平（Sharpe 0.22→0.35）；
2. ⚠️ **绝对水平仍弱**（Sharpe 0.35）：env 模拟（逐日归因）与 BacktestEngine（逐 bar 持仓路径）仍有差距；进化选择在 valid 段；下一步更彻底对齐 = **进化适应度直接改用 BacktestEngine**（成本高但最真实）；
3. ✅ RL 相对规则基线（-0.13%）优势保持；
4. ⚠️ 回撤放大（-10.4%）：新权重下持仓更集中，需风控约束。

## 五、交付物

- `hexbroker/rl/sentinel_env.py`（奖励逐日化）
- `scripts/sentinel_phase4_evo.py`（重训 + BacktestEngine 验证）
- `sentinel-phase4-evo-2026-08-17.json`（更新结果）
