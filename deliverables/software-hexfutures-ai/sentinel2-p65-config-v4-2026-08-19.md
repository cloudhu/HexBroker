# P6-5 v4 生产配置固化报告

**日期**：2026-08-19 ｜ **流程**：工程师实现 → QA fresh-eyes 复核（VERIFIED）→ 主理人终裁

## TL;DR

**v4 生产配置已固化进 `BacktestConfig`**：引擎 A 默认读 v4 缓存（`signals_cache18_grouped_v4.parquet`）、top_k=0.30、S2 min_symbols=3、组合 A15/B85。QA 独立验证：42 项断言全 PASS、全量 pytest 228 passed、向后兼容完整——**生产配置 READY**。

## 一、固化内容

### `hexbroker/config.py`（+EngineAConfig，挂在 BacktestConfig）
```python
class EngineAConfig(BaseModel):
    model_config = _MODEL_CFG
    enabled: bool = True
    signal_cache: str = "artifacts/signals_cache18_grouped_v4.parquet"  # 生产缓存 v4
    top_k: float = 0.30       # 每日截面做多分位阈值
    min_symbols: int = 3      # S2 稀疏防御
    notional_frac: float = 0.20
```

### `scripts/p5_engineA_cross_section.py`（默认缓存解析）
- `_resolve_cache_path()`：显式 cache_path 优先 → None 时读配置 v4 → 配置异常兜底回退 v2
- 相对路径按 ROOT 解析；向后兼容（显式 v2 路径仍可用）

## 二、QA 验证（独立复现）

| 检查项 | 结果 |
|---|---|
| 默认配置 | engine_a(v4/0.30/3/0.20) + engine_b(252/0.7) + combo(A15/B85/vol=False) ✅ |
| 断言 | 42 项全 PASS（工程师声称 34，实测 42，记录性差异）✅ |
| pytest 全量 | **228 passed**（工程师声称 17，实测更广）✅ |
| 旧 yaml 兼容 | 5 份真实 configs yaml 全部可加载，engine_a 取默认 ✅ |
| yaml 覆盖 | top_k=0.25 / signal_cache=v2 覆盖生效 ✅ |
| **默认缓存解析（内容级证明）** | 无 cache_path → 输出 662 天 == v4 缓存天数（v2=976）→ **实读 v4** ✅ |
| 显式 v2 兼容 | cache_path=v2 → 976 天 == v2 缓存天数 ✅ |
| 异常兜底 | load_config 抛异常 → 回退 v2 ✅ |
| 缓存/数据未动 | v2/v4 mtime 早于 P6-5 文件，未被触碰 ✅ |

## 三、主理人终裁

1. **P6-5 验收通过**：v4 生产配置已固化，`load_config()` 默认即生产口径
2. **Sentinel-2 v1.0 生产基线最终确认**：
   - 引擎 A：v4 缓存 + top_k 0.30 + S2 min=3（按日截面 rank）
   - 引擎 B：win252/thr0.70/名义 20%
   - 组合：A15/B85 / vol_target=False → **OOS Sharpe 1.384**
3. **登记 QA 建议项**：① docs 补 P6-5 EngineAConfig 文档 ② config.py 未提交改动建议 git 提交 ③ 显式相对 cache_path 低风险观察
4. **引擎 A 信号质量迭代仍为最高优先级**（OOS -0.024 待转正）

## 四、文件清单

| 文件 | 说明 |
|---|---|
| `hexbroker/config.py` | +EngineAConfig（v4 默认缓存） |
| `scripts/p5_engineA_cross_section.py` | +_resolve_cache_path（默认读 v4） |
| `scripts/p6_5_config_validate.py` | 42 项断言验证 |
| `deliverables/software-hexfutures-ai/sentinel2-p65-config-validate-QA-review-2026-08-19.md` | QA 复核报告 |
| `docs/developer-guide.md` | 更新至 v3.8（§9.13） |
