# P2-8 + Q1：io.py 原子写异常路径去删除调用 & 删除类红线审计器推广到基建（2026-09-02）

## 0. TL;DR

| 项 | 内容 | 状态 |
|---|---|---|
| P2-8 | `hexbroker/utils/io.py::_atomic_write` 异常路径 `os.remove(tmp)` → **保留 tmp** | ✅ 已修 |
| P2-7 推广 | 红线审计器抽为基建共享模块 `hexbroker/utils/safety_audit.py`，参数化扫描 `hexbroker/utils/*.py` 全部 **8** 文件；**模块别名绕过已堵**（QA 复核后补，见 §7）；**P2-8c 包级扩扫 + `SignalStore.clear()` 死代码删除**（主理人裁决 2026-09-03，见 §8） | ✅ 已落地 |
| Q1 | `broker.py` L5 docstring「单品种保证金」→「全组合口径」 | ✅ 已修（随本轮 feat） |
| QA 复核 | fresh-eyes 独立复核（software-qa-engineer-3）：**0 🔴 / 4 🟡**，逐项处置见 §7 | ✅ 通过 |
| 回归 | **1174 passed**（1160 基线 + 14 新增），0 failed | ✅ 实测 |
| 提交 | feat + docs 双提交（hash 见 §6），含 QA 处置 follow-up | ✅ |

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
- **新测试** `tests/test_io_atomic_write_safety.py`（**13 用例**，QA 复核后 +1）：
  1. 参数化扫描 `hexbroker/utils/*.py` 全部 **8 文件**（初稿口径「7 文件」已过期——
     创建 safety_audit.py 本身即第 8 个）零删除调用 —— **fail-closed**：
     新增 utils 文件自动纳入，谁写 `os.remove` 回归直接红；
  2. io.py 保留 `os.replace(tmp, path)` 主路径自证（护栏不空转）；
  3. 审计器负向自测：5 类违规必抓 + 注释/docstring/`list.remove`/`os.replace`
     四类正向对照不误伤；
  4. **模块别名绕过必抓**（QA 🟡2 处置）：`import os as o; o.remove(p)` /
     `import shutil as s; s.move(a, b)` / `o.rename` 经 Pass 1 别名表解析后命中；
     无别名 `os.replace` 正向对照不误伤；
  5. **P2-8 核心行为测试**：写盘失败 → 异常传播 + 原档逐字节不变 + tmp 保留
     （若有人改回 `os.remove(tmp)` 旧行为，此断言必红）；
  6. 成功路径：目标被原子替换、无 tmp 残留。

## 3. Q1：`broker.py` L5 docstring 口径修正（doc-only）

- **证据**：L5 原文「单品种保证金占用 <= 预算上限」与 L179-189 实现
  （`_margin_after` 遍历 `positions` 全量求和，含新开仓目标品种）矛盾。
- **修正后**：「全组合总保证金占用（`_margin_after` 遍历 positions 全量求和，
  含新开仓目标品种）<= 预算上限（budget_ratio * equity）」。
- **层次关系（不变，措辞对齐实现）**：planner `max_margin_pct=0.20`（单品种）
  ⊂ broker `budget_ratio=0.40`（全组合），**叠加而非重复**。
- 零行为变化：仅模块 docstring 措辞。

## 4. 范围外删除调用（QA 🟡3 复核后登记 **P2-8c**，待主理人裁决）

全仓 `unlink|os.remove|shutil.move|os.rename` 扫描发现 utils/ 之外还有 4 处删除调用，
**初步判断均为设计内的锁文件/标记清理**，未纳入本轮红线（盲扫会把设计内行为误判为违规）。
QA fresh-eyes 复核确认并补充定性：

| 位置 | 现状 | QA 定性 |
|---|---|---|
| `hexbroker/forecast/signal_store.py:200` | `f.unlink()` | **最重**：`clear()` 遍历全库逐文件 unlink，信号文件轮换清理 |
| `hexbroker/data/rebuild.py:141, 359` | `p.unlink()` / `missing_marker.unlink()` | 数据层标记清理，疑似设计内 |
| `hexbroker/diagnostics/health_check.py:154` | `pid_path.unlink()` | 僵尸 PID 锁文件清理，疑似设计内 |

👉 处置选项（登记 **P2-8c**，待主理人裁决）：①红线扫描扩展到 `hexbroker/` 全包 +
显式豁免清单；或 ②对 4 处逐一立整改单（G5 化改造）。

## 5. 回归

```
全量（不带路径参数）：1174 passed（1160 基线 + 14 新增），0 failed
```

新增用例分布：`tests/test_io_atomic_write_safety.py` 14 个
（8 参数化 + 6 专项，含 P2-8c 包级扩扫）。

**插曲（R26b 全量回归实测）**：首轮全量 2 failed ——
`hexbroker/data/test_backup.py::TestFreshness` 两用例，与 R26b 改动无关，
系**时间炸弹测试**：硬编码 `end=2026-08-28` + `max_stale_days=5`，而
`assert_fresh` 历史回填豁免用 `date.today()` 真实时钟，2026-09-03 起滚过
豁免边界（`today-5d > end`）→「陈旧必须失败」静默反转成「豁免通过」。
修法：`TestFreshness` 加 autouse fixture `_freeze_reference_clock`，
在 backup 模块层包一层注入 `today=2026-08-28`（isinstance 语义与被测实现
零改动），3 用例随冻结时钟确定性通过。教训已固化进 MEMORY.md：日期型
用例必须冻结参考时钟。

## 6. 提交

| commit | 内容 |
|---|---|
| feat `07b9c4e` | `hexbroker/utils/io.py` + `hexbroker/utils/safety_audit.py` + `tests/test_io_atomic_write_safety.py` + `hexbroker/paper/broker.py`（Q1 docstring），4 文件 +274/−4 |
| docs `b9c691a` | 本报告初版 |
| feat `78cd798` | QA 复核处置（R26b）：`safety_audit.py` 模块别名覆盖 + 威胁模型 docstring + 测试文件补 bypass 用例（2 文件 +48/−8） |
| fix `f808484` | `TestFreshness` 冻结参考时钟（时间炸弹测试修复，1 文件 +18） |
| docs `<本提交>` | 本报告 §5 插曲 / §6 hash / §7 处置表 |

`git fsck --no-dangling` 收尾须无输出。

## 7. QA fresh-eyes 复核与处置（software-qa-engineer-3，2026-09-02）

QA 独立复核结论：**通过，0 🔴 / 4 🟡**。三项 ✅ 均附实测证据：

| 项 | QA 结论 | 处置 |
|---|---|---|
| ✅ 正常路径 | 旧实现复刻 A/B 对拍，3 种 payload（JSON/10KB/空串）写盘 SHA256 逐字节一致（f9d86028/e4ee97ec/e3b0c442…） | 无需动作 |
| ✅ 旧行为零依赖 | 72 处 `write_json/write_parquet/_atomic_write` 调用点逐一 grep；tests 内 4 处 tmp 断言全为成功路径「无残留」，失败路径零断言 | 无需动作 |
| 🟡1 孤儿 tmp 永不清理 | 随写盘失败次数堆积（docstring「下次成功写盘覆盖」仅指目标文件） | **登记 P2-8b**：启动盘点只列不删 + WARNING；需选启动钩子位置（动生产入口），待主理人 |
| 🟡2 审计器可绕过 | 16 形态攻击矩阵实测 10/16 绕过；**模块别名 `import os as o; o.remove(p)` 是唯一「无辜像样」形态且修复极廉** | **本轮已堵**：Pass 1 别名表 + Pass 2 根名解析（补 `test_audit_catches_module_alias_bypass`）；其余动态形态（变量别名/getattr/importlib/eval/subprocess/functools.partial/methodcaller 等）定性为**不在威胁模型内**——审计器定位是「防手滑回归绊线」非安全边界，已写入模块 docstring |
| 🟡3 范围外暴露面 | utils 实为 8 文件（brief 写 7 已过期，已更正）；utils 外 4 处同铁律暴露面，`signal_store.py:200` clear() 遍历全库 unlink 最重 | **登记 P2-8c**（见 §4），待主理人裁决 |

附注（QA 脚注登记）：`tests/test_p11_truth_rebuild.py:195,221` 测试夹具用 unlink
（测试侧临时文件，不在生产红线内）。

R26b 实施备注：首轮编辑漏落 Pass 1 别名收集代码（`alias_modules` 未定义 →
NameError → 8 failed），按取证 SOP 定位后补齐，复测 13 passed。

## 8. P2-8c 取证与处置方案（R26c 取证完成，**待主理人裁决**）

### 8.1 四处现场取证（2026-09-03 实读）

| 位置 | 语义（实读代码） | 关键证据 | 风险评级 |
|---|---|---|---|
| `forecast/signal_store.py:200` `clear()` | 遍历 `root.glob("*/*.parquet")` 全库逐文件 unlink | **全仓零调用**（hexbroker/ + scripts/ + tests/ 均无调用方）—— 死代码 API | 🟡 低（当前无人触发）但属「 invites misuse」：未来谁调用即整库删除 |
| `data/rebuild.py:141`（`clear_provisional` 尾部） | sidecar 全部年份清空后删除空 sidecar 文件；函数 docstring 与 L348-351 注释**明说**「全部年份清空后删除 sidecar 文件」 | 设计内 marker 生命周期，写/删语义自洽 | 🟢 设计内 |
| `data/rebuild.py:359` | 分区按真值重建后删除 `_MISSING_{year}.json` 标记（消费式清理） | 设计内 marker 生命周期 | 🟢 设计内 |
| `diagnostics/health_check.py:154`（`_read_pid_file`） | 读 PID 文件 → 判进程已死 → 删僵尸锁文件（OSError 吞掉） | 功能上删不删都返回 None（下次读取同样判死） | 🟢 设计内，可选优化 |

### 8.2 方案对比

| 方案 | 内容 | 代价 | 收益 |
|---|---|---|---|
| ① 扩扫 + 显式豁免清单 | 参数化审计从 `utils/` 扩到 `hexbroker/` 全包（**排除 third_party/**，其内 vendored 库 unlink 遍布）；豁免清单只登记 `rebuild.py` / `health_check.py` 两文件（注释锚定） | 审计测试 +~20 行；豁免按文件粒度（文件内新增 unlink 不再红） | 新增隐藏删除调用必红；红线可见性覆盖全包 |
| ② 逐处整改单（G5 化） | 4 处全部改造 | **marker/PID 类无 G5 等价物**（空文件仍命中 `.exists()`/glob；os.rename 同在红线内）——改法只能改为「payload 置空 + 消费方感知」，侵入 3 个消费点，回归风险大于收益 | 基本无净收益 |

### 8.3 处置（**已实施**，主理人裁决 2026-09-03「P2-8c 方案」采纳推荐）

**方案 ① + 零风险整改，已落地**：
1. ✅ 审计扩扫：`tests/test_io_atomic_write_safety.py` 新增
   `test_hexbroker_package_no_unexpected_delete_calls` —— 扫描 `hexbroker/`
   全包（~150 文件，`third_party/` 在 repo 根目录天然排除），豁免清单
   `AUDIT_PACKAGE_EXEMPT = {data/rebuild.py, diagnostics/health_check.py}`
   （文件粒度 + 语义注释锚定，新增豁免须主理人裁决）；带 `scanned > 100`
   防空转断言；
2. ✅ **删除 `SignalStore.clear()` 死代码**（`signal_store.py` 原 L198-200，
   全仓零调用，根除唯一整库 unlink 暴露面）；
3. ✅ `health_check.py:154` 僵尸 PID 清理保持不动（设计内，豁免登记）。

自测：安全测试文件 14 passed；全量回归 **1174 passed**（见 §5）。
QA fresh-eyes 复核已发出（software-qa-engineer-3），结论回填后如有🟡另记。

### 8.4 附：Q4 取证结论（c0 宇宙归属，2026-09-03 实测）

- `CONTRACTS18`（`scripts/build_signals18.py:18`）键集 = au0/ag0/m0/cu0/rb0/i0/al0/zn0/ni0/hc0/y0/p0/j0/jm0/sr0/cf0/ta0/sc0 —— **无 c0** ✅
- 主湖 `data/raw/processed/` 实测 18 目录，**无 c0**（`ls` 报 No such file or directory）✅
- 与 MEMORY 记载一致：c0 在生产宇宙（configs/paper.yaml）但不在 CONTRACTS18、不在主湖；复算历史 ATR 会 FileNotFoundError，兜底 sina 名义价。Q4 闭合。

---

*风险提示：本报告为工程质量治理记录，不构成投资建议。*
