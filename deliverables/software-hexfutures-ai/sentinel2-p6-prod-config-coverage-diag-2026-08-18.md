# P6 生产配置固化 + 引擎A信号覆盖率诊断报告

**日期**：2026-08-18 ｜ **流程**：工程师实现 → QA fresh-eyes 复核（VERIFIED ×2）→ 主理人终裁

## TL;DR

| 子任务 | 判定 | 关键结论 |
|---|---|---|
| P6-1 固化 Sentinel-2 v1.0 生产配置 | ✅ **VERIFIED** | `EngineBConfig` + `ComboConfig` 固化进 BacktestConfig（+32 行，向后兼容，28 项断言 PASS） |
| P6-2 引擎A信号端覆盖率诊断 | ✅ **VERIFIED** | 三重根因定位：折叠测试窗截断 / 折叠相位漂移 / 2026 数据尾部真空 |

---

## 一、P6-1 生产配置固化 ✅

### 固化内容（hexbroker/config.py +32 行）
```python
class EngineBConfig(BaseModel):
    enabled: bool = True
    win: int = 252          # basis_ratio 品种内滚动分位窗口
    thr: float = 0.70       # 做多分位阈值
    notional_frac: float = 0.20  # 名义占权益比例

class ComboConfig(BaseModel):
    w_engine_a: float = 0.15   # P5 终裁：A≤15%（OOS 负贡献者）
    w_engine_b: float = 0.85   # B 为基（OOS Sharpe 1.260）
    vol_target: bool = False   # P4-1 终裁：不叠加组合层波目标
    vol_target_ann: float = 0.175
    vol_ewma_halflife: int = 10
```

### 验证（QA 独立复跑）
- `p6_config_validate.py`：28 项断言全过（默认/嵌套默认/旧 yaml 兼容/嵌套覆盖）
- 向后兼容：QA 独立最小 yaml 17/17 PASS；嵌套覆盖（thr=0.8 / w_a=0.0）生效
- 回归：test_config.py 6 passed + 相邻模块 14 passed

## 二、P6-2 覆盖率诊断 ✅（根因定位）

### 信号缓存为何只有 976 天且覆盖率骤降

**三重根因**（QA 独立复现）：
1. **结构性截断**：walk_forward 每折叠测试窗 60 天 → lookback 损耗 30 → ~31 信号 → `cal_split=0.5` 只输出后半 ~16 天/折 → 覆盖率仅 ~25%
2. **折叠相位漂移（主因）**：折叠网格基于品种整数索引，数据缺口不同 → 同一折叠序号映射到不同日历日 → run 相位漂移。**铁证**：2022/2023 低谷月 = `cu0/i0/rb0`（恰是 2022 年缺 28 天品种）；2024/2025 = `i0`（2024 又缺 ~130 天）
3. **2026 尾部真空**：15/18 品种本地数据止于 **2026-02-24**（gap=213 天），仅 au0/ag0/m0 到 2026-08-14 → 之后仅 3 品种可产信号

### 年度覆盖率（QA 逐位一致）
| 年份 | 日均品种/天 | 信号数 |
|---|---|---|
| 2019-2021 | 13.2 | ~1200/年 |
| 2024 | **5.7** | 992 |
| 2025 | 5.9 | 1152 |
| 2026 | **2.2** | 112 |

### 修复方案（QA 审查后采纳）
- **方案 A（主修信号端，采纳附条件）**：`_calibrate_and_split` 增加"校准后返回全部信号"模式或 `cal_split=None` → 每折 16→31 条，**覆盖率翻倍（~1900 天）**，交易信号零泄漏（校准只改 p_up/is_effective 不改 exp_ret，引擎 A 只用 exp_ret.rank）
- **方案 B（治 2026 真空，互补）**：回补 15 品种 2026-02-24 后数据 + cu0/rb0/i0 2022 年 28 天 + au0/ag0/m0/cu0/rb0 2024 年缺口
- **方案 C（缓存重建）**：A+B 落地后重跑 group_modeling_v2.py 重建缓存，保留 min_symbols=3 防御

## 三、主理人终裁

1. **P6-1 采纳**：Sentinel-2 v1.0 生产配置已固化，`load_config()` 向后兼容，作为生产基线
2. **P6-2 诊断采纳**：根因清楚——引擎 A 的 OOS 劣势主要是**信号覆盖率结构性不足**（非模型本身无 alpha），修复路径明确
3. **方案 A 批准实施（P6-3）**，附 QA 条件：① 明示 p_up 弱监督口径 ② 重建信号缓存 + 重跑 P5 截面 rank 重估 ③ 并行执行方案 B 补数据
4. **登记**：P6-3 = 信号缓存重建（方案 A+C）；P6-4 = 数据补齐（方案 B）

## 四、文件清单

| 文件 | 说明 |
|---|---|
| `hexbroker/config.py` | +32 行：EngineBConfig / ComboConfig 固化 |
| `scripts/p6_config_validate.py` | 配置验证（28 断言） |
| `scripts/p6_signal_coverage_diag.py` | 覆盖率诊断（只读） |
| `artifacts/p6_coverage_diag.csv` | 634 行覆盖率统计（月度/年度/品种/缺口） |
| `artifacts/_qa_backup_p6/` | QA 复核证据 |
| `docs/developer-guide.md` | 更新至 v3.3（§9.8 P6） |
