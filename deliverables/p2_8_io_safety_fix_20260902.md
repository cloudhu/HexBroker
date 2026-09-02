# P2-8 + Q1：io.py 原子写异常路径去删除调用 & 删除类红线审计器推广到基建（2026-09-02）

## 0. TL;DR

| 项 | 内容 | 状态 |
|---|---|---|
| P2-8 | `hexbroker/utils/io.py::_atomic_write` 异常路径 `os.remove(tmp)` → **保留 tmp** | ✅ 已修 |
| P2-7 推广 | 红线审计器抽为基建共享模块 `hexbroker/utils/safety_audit.py`，参数化扫描 `hexbroker/utils/*.py` 全部 7 文件 | ✅ 已落地 |
| Q1 | `broker.py` L5 docstring「单品种保证金」→「全组合口径」 | ✅ 已修（随本轮 feat） |
| 回归 | 1172 passed（1160 基线 + 12 新增），0 failed | ✅ 实测 |
| 提交 | feat + docs 双提交（hash 见 §6） | ✅ |

## 1. P2-8：证据与修法

**证据**（改前实测）：`hexbroker/utils/io.py` 旧 L57-60 —— 写盘抛异常时
`os.remove(tmp)` 清理临时文件。这是项目明令禁止的删除类调用：沙箱 safe-delete
钩子会拦截 unlink/remove 并路由至回收站，项目已因此丢过生产文件
（`.git/objects` 事故）。此前 P2-7 审计器只扫 `scripts/p43_*` 修复脚本，
基建层（`hexbroker/utils/`）从未被扫过 —— io.py 因此漏网。

**修法**（最小改动，只动异常路径）：

```python
# 旧
except Exception:
    if os.path.exists(tmp):
        os.remove(tmp)        # ⛔ 删除类调用
    raise

# 新
except Exception:
    _log.error("原子写失败（原档未动，tmp 已保留）：{} -> {}", tmp, path)
    raise
```

**行为影响分析**：

| 路径 | 旧行为 | 新行为 |
|---|---|---|
| 正常（writer 成功 → os.replace） | 不变 | **逐字节不变** |
| writer 抛异常 | 异常传播 + 原档不变 + **tmp 被删** | 异常传播 + 原档不变 + **tmp 保留** + ERROR 日志 |

**孤儿 tmp 风险评估**：tmp 为 `mkstemp` 随机名 + `.tmp` 后缀，落在目标同目录；
项目所有数据扫描均为按扩展名显式匹配（`glob("*.parquet")` / `*.json` 等），
孤儿 tmp 不会被命中；量级 = 写盘失败次数（罕见）。`write_json` /
`write_parquet` 两个调用方（含 p43 修复台账 `_RAW_CLOSE_REPAIRS.json` 的落盘）
均受益。

## 2. P2-7 审计器推广到基建

- **新增** `hexbroker/utils/safety_audit.py::audit_no_delete_calls(source)`：
  从 p43 测试审计器中抽出**文件无关**的红线 —— 属性名 `unlink`/`rmtree`/`rmdir`
  命中即违规；点号全名 `os.remove`/`shutil.remove`/`shutil.move`/`shutil.rmtree`/
  `os.rename` 违规；`from os/shutil/pathlib import` 危险名违规；
  裸 `remove()`（`list.remove`）不判；纯 AST 扫描，注释天然规避、docstring 显式跳过。
- **与 p43 审计器的关系**：p43 的 `audit_script_safety` 是**超集**（额外含
  `to_parquet` 目标必须引用 tmp + 落盘后随 `os.replace` 的脚本专属检查），
  **保持不动**（其 9 类负向自测与 4 用例全绿，不碰）；共享模块负责基建红线。
  后续如需统一（p43 改为调用共享模块 + 保留 parquet 专项），登记为可选清理项。
- **新测试** `tests/test_io_atomic_write_safety.py`（12 用例）：
  1. 参数化扫描 `hexbroker/utils/*.py` 全部 7 文件零删除调用 —— **fail-closed**：
     新增 utils 文件自动纳入，谁写 `os.remove` 回归直接红；
  2. io.py 保留 `os.replace(tmp, path)` 主路径自证（护栏不空转）；
  3. 审计器负向自测：5 类违规必抓 + 注释/docstring/`list.remove`/`os.replace`
     四类正向对照不误伤；
  4. **P2-8 核心行为测试**：写盘失败 → 异常传播 + 原档逐字节不变 + tmp 保留
     （若有人改回 `os.remove(tmp)` 旧行为，此断言必红）；
  5. 成功路径：目标被原子替换、无 tmp 残留。

## 3. Q1：`broker.py` L5 docstring 口径修正（doc-only）

- **证据**：L5 原文「单品种保证金占用 <= 预算上限」与 L179-189 实现
  （`_margin_after` 遍历 `positions` 全量求和，含新开仓目标品种）矛盾。
- **修正后**：「全组合总保证金占用（`_margin_after` 遍历 positions 全量求和，
  含新开仓目标品种）<= 预算上限（budget_ratio * equity）」。
- **层次关系（不变，措辞对齐实现）**：planner `max_margin_pct=0.20`（单品种）
  ⊂ broker `budget_ratio=0.40`（全组合），**叠加而非重复**。
- 零行为变化：仅模块 docstring 措辞。

## 4. 观察登记（本轮不动，待单独裁决）

全仓 `unlink|os.remove|shutil.move|os.rename` 扫描发现 utils/ 之外还有 3 处删除调用，
**初步判断均为设计内的锁文件/标记清理**，未纳入本轮红线（盲扫会把设计内行为误判为违规）：

| 位置 | 现状 | 初判 |
|---|---|---|
| `hexbroker/diagnostics/health_check.py:154` | `pid_path.unlink()` | 自身 PID 锁文件清理，疑似设计内 |
| `hexbroker/data/rebuild.py:141, 359` | `p.unlink()` / `missing_marker.unlink()` | 数据层标记清理，疑似设计内 |
| `hexbroker/forecast/signal_store.py:200` | `f.unlink()` | 信号文件轮换清理，疑似设计内 |

👉 是否将红线扫描范围扩展到上述模块，需主理人裁决（需先逐处确认业务语义）。

## 5. 回归

```
全量（不带路径参数）：1172 passed（1160 基线 + 12 新增），0 failed
```

新增用例分布：`tests/test_io_atomic_write_safety.py` 12 个
（7 参数化 + 5 专项）。

## 6. 提交

| commit | 内容 |
|---|---|
| feat `07b9c4e` | `hexbroker/utils/io.py` + `hexbroker/utils/safety_audit.py` + `tests/test_io_atomic_write_safety.py` + `hexbroker/paper/broker.py`（Q1 docstring），4 文件 +274/−4 |
| docs | 本报告 |

`git fsck --no-dangling` 收尾须无输出。

---

*风险提示：本报告为工程质量治理记录，不构成投资建议。*
