# Sentinel-2 P10：group_map 配置落地 + A10 边界补测（2026-08-20）

> 阶段：P10（P9 遗留 2 项闭环）｜ 流程：工程师（寇豆码）→ QA（严过关）→ 主理人终裁 ｜ 状态：✅ 完成（QA VERIFIED + 1 建议项已处置）

## TL;DR

**P9 遗留 2 项全部闭环**：① `group_map` 配置落地（QA 阻塞项）——EngineAConfig 新增 `group_map` 字段 + `configs/base.yaml` 启用 `group_cap=0.5`（黑色系合并 `ferrous_all`），pytest 208 passed；② **A10 边界补测**——A0/B100（纯引擎 B）OOS 1.622 为下界最优（随 w_a 下降单调改善），但 QA 裁决 **ComboConfig 维持 A10/B90 不更新**（增量小、全样本反降、A0 是测试下界有 overfit 嫌疑、与 P9 先例一致）。

## P10-1 group_map 配置落地（QA 阻塞项，P0）✅

| 项 | 实现 |
|---|---|
| config.py | `EngineAConfig.group_map: dict[str,str] \| None = None`（pydantic dict 字段，yaml 可覆盖） |
| 解析优先级 | `_resolve_group_map/_resolve_group_cap`：显式传入 → 读 config → 兜底 GROUPS_V2 / 不启用（参考 `_resolve_cache_path` 模式，异常兜底） |
| base.yaml | `backtest.engine_a.group_cap: 0.5` + `group_map` 18 键（i0/j0/jm0/rb0/hc0 → **ferrous_all** 合并黑色系；其余 13 品种组名与 GROUPS_V2 一致） |
| 验证 | `load_config("configs/base.yaml")` → cap=0.5/18 键生效；无参 → None（向后兼容）；pytest **208 passed**（197+11 新增） |
| 部署接线 | **显式加载部署 yaml + 显式传参**（主理人已验证：1968 做多行正常执行） |

**QA 独立验证**：静态审查全正确（入口统一 engine_a_targets_cs → engine_a_selection，grep 无漏路）；复跑 p6_5 全 PASS；group_map 18/18 品种无缺漏。

## P10-2 A10 边界补测（QA 要求，P1）✅

权重下界对比（vol=N，复利口径 OOS，QA 逐位一致 1e-9）：

| 配置 | OOS Sharpe | OOS 复利 | 全样本 Sharpe | OOS MaxDD |
|---|---:|---:|---:|---:|
| **A0/B100**（纯 B） | **1.622** | +31.05% | 0.972 | -6.67% |
| A5/B95 | 1.618 | +29.87% | 0.998 | -6.40% |
| **A10/B90（生产）** | **1.612** | +28.70% | **1.023** | **-6.13%** |
| A15/B85（P8 基线） | 1.602 | +27.54% | 1.049 | -5.86% |

**关键事实**：OOS Sharpe 随 w_a 下降**严格单调改善**（引擎 A OOS IC 为负 -0.064 拖累）；但**全样本反向**（A0=0.972 < A10=1.023）、OOS MaxDD 略深（-6.67% vs -6.13%）——引擎 A 提供全样本稳健性与回撤缓冲。

## QA 关键裁决（主理人采纳）

| 决策点 | QA 意见 | 主理人终裁 |
|---|---|---|
| **ComboConfig 是否更新** | ❌ 不建议——OOS 增量小（A0 +0.011）、全样本反降、A0 是测试下界非内部最优（边界 overfit 嫌疑）、与 P9"因先验选 A10"先例一致；若必须折中 A5 优于 A0（保 5% 分散化） | **维持 A10/B90**（不更新） |
| **load_config() 无参语义** | ✅ 保持现状——无参=代码默认是项目一贯语义（base.yaml 是 Hydra 组合片段），改会破坏 40+ 处既有调用 | **保持现状** |
| **group_map 完整性** | 18/18 品种全覆盖 | ✅ 采用 |
| **DISCREPANCY-1（设计缺口）** | 读配置分支生产中不可达（无参 load_config 永远 None）——三选一处置 | **采纳建议①：显式传参固化为部署标准** + 写入开发者指南部署接线说明（见 §9.18） |

## 部署接线标准（DISCREPANCY-1 处置，固化）

```python
# 生产调用（部署 yaml 显式启用 group_cap）：
cfg = load_config("configs/base.yaml")          # 显式加载部署配置
engine_a_targets_cs(
    prices,
    group_cap=cfg.backtest.engine_a.group_cap,  # 0.5
    group_map=cfg.backtest.engine_a.group_map,  # 18 键 ferrous_all
)
```
⚠️ 无参 `load_config()` 不加载 base.yaml（向后兼容）；生产部署必须显式加载 + 显式传参，否则 group_cap 不生效。

## 文件清单

- 📄 本报告 + QA 复核：`p10-QA-review-2026-08-20.md` + `qa_p10_independent_verify.py`
- 🔧 代码：`hexbroker/config.py`（+group_map）/ `scripts/p5_engineA_cross_section.py`（+_resolve_group_map/_resolve_group_cap）/ `configs/base.yaml`（部署启用）
- 🐍 脚本：`scripts/p10_weight_lower_bound.py`
- 🧪 测试：`tests/test_engine_a_group_resolve.py`（+8）/ `tests/test_config.py`（+3）
- 📊 数据：`artifacts/p10_weight_lower_bound.csv`
- 📄 开发者指南：更新至 **v3.13**（§9.18）

## 遗留登记

- P 系列提交后新一轮变更（config.py/base.yaml/p10 脚本/测试）待 git 提交
- 生产运行脚本接入时按部署接线标准执行（load_config("configs/base.yaml") + 显式传参）
