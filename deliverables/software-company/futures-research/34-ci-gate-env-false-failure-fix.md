# 34 · CI 环境依赖型回归锁假失败修复报告

> 日期：2026-08-28 · 主理人：齐活林 · 触发：GitHub Actions CI 报红 2 例
> 状态：✅ 已修复并验证（全量 663 绿）

---

## TL;DR

CI 上红的 2 个用例**不是生产代码回归，而是我上一轮写的回归锁只认本地开发环境**。
根因已用硬证据定位：① CI 的 ruff 只装在 lint job，test job 没装；② 两个缓存 parquet
被 `.gitignore` 排除、不属于 git 仓库，CI 全新 checkout 里必然不存在。
修复采取「**环境差异按环境分治 + 缺失场景正面验证反假绿行为**」，CI 上反而把 D2 缺陷
锁得更死，而非简单 skip 了事。

---

## 一、失败现象

```
FAILED tests/test_audit_ruff_gate.py::test_ruff_gate_is_interpreter_agnostic
    assert _ruff_cmd() is not None, "当前环境无任何可用 ruff，门禁将静默失效"
    assert None is not None  + where None = _ruff_cmd()

FAILED tests/test_signal_refresh_gate.py::test_production_paths_are_probeable
    AssertionError: 存在无法判定的生产缓存：
      ['signals_cache18_grouped_v8_tail_ext.parquet', 'signals_cache18_grouped_v8.parquet']

2 failed, 647 passed, 14 skipped in 113.33s
```

用例总数 663 与本地一致（本地 649 passed + 14 skipped），说明**没有新增/丢失用例**，
仅这 2 个从「通过」翻转为「失败」——是环境差异，不是代码回归。

---

## 二、根因取证（硬证据）

| # | 根因 | 证据 |
|---|------|------|
| 1 | CI 的 `test` 与 `lint` 是**两个独立 job**，ruff 只装在 lint job | `.github/workflows/ci.yml:60` 为 lint job 的 `pip install ruff black mypy`；`test` job（第 14–42 行）依赖安装段无 ruff |
| 2 | 两个缓存 parquet **未入 git** | `.gitignore` 第 7 行 `artifacts/`、第 28 行 `*.parquet`；`git ls-files --error-unmatch` 报 `did not match any file(s) known to git` |

补充：本地 ruff 正常（`C:/Users/.../Python311/Scripts/ruff` v0.15.20），托管 venv 有
ruff 0.15.21 包但无二进制 —— 这正是 `_ruff_cmd()` 当初要做 PATH 优先 + 模块回退的原因。

**判定：这两个文件是数据产物，本就不该入库。禁止通过改 `.gitignore` 让它们入库来迁就测试。**

---

## 三、修复方案

### 3.1 `.github/workflows/ci.yml` — test job 补装 ruff

F821/F811/E9 门禁属于 pytest 套件的一部分，但工具只装在 lint job。补装后 CI 上该门禁
**真正跑起来**，而不是整条 skip（静默失效）。

### 3.2 `test_ruff_gate_is_interpreter_agnostic` — 按环境分治

| 环境 | 无 ruff 时行为 | 理由 |
|------|----------------|------|
| **CI**（`CI` / `GITHUB_ACTIONS` 置位） | **硬失败** | CI 上没 ruff ⇒ `test_ruff_f821_gate` 整条 skip ⇒ F821 门禁静默失效，这正是本锁要防的，必须红 |
| **本地** | `skip` | 开发机可能没装 ruff，不应为此阻断本地开发 |

### 3.3 `test_production_paths_are_probeable` — 改三段式

| 段 | 适用 | 断言 |
|---|------|------|
| **A** 常量断言 | CI / 本地均成立 | `resolve_cache_paths()` 非空；`len(probes) == len(paths)`（`probe_files` 绝不静默丢弃路径） |
| **B** 文件齐备 | 本地 | 全部可判定新鲜度（原断言） |
| **C** 文件缺失 | **CI** | **不 skip**：断言每个缺失探针 `unknown` 为真，且 `format_banner()` 含「无法判定」、不含「检查通过」 |

> C 段的设计要点：D2 缺陷的内核是「**缺失必须显性告警，不得静默计为通过**」。
> 这条**不需要真实文件**即可验证。CI 恰好天然处于「文件缺失」状态，
> 因此用它**正面锁死**反假绿行为，比 skip 掉强得多。

---

## 四、验证结果（实际执行输出）

| 验证项 | 结果 |
|--------|------|
| 本地全量 pytest | **663 绿，EXIT=0，0 个 FAILED/ERROR** ✅ |
| 定向用例（本地，走分支 B） | 19 passed ✅ |
| **模拟 CI**（`CI=true GITHUB_ACTIONS=true` + 两个 parquet 改名移除） | **15 passed** ✅（走分支 C，跑完已还原） |
| ruff 门禁三分支 | 见下 ✅ |
| `ruff check` 改动文件 | All checks passed ✅ |

ruff 门禁分支取证（强制 `_ruff_cmd()` 返回 None）：

```
[强制无 ruff] _ruff_cmd() -> None
[CI 分支]   Failed（硬失败）: CI 环境无可用 ruff —— test_ruff_f821_gate 将整条 skip...
[本地分支]  Skipped（跳过）: 本地无可用 ruff（PATH 与当前解释器均无），F821 门禁跳过
[对照：ruff 可用] 通过 ✅
```

---

## 五、文件清单

| 文件 | 改动 |
|------|------|
| `.github/workflows/ci.yml` | test job 补 `pip install ruff`（+4 行，含注释说明） |
| `tests/test_audit_ruff_gate.py` | `import os`；`test_ruff_gate_is_interpreter_agnostic` 改 CI 硬失败 / 本地 skip |
| `tests/test_signal_refresh_gate.py` | `test_production_paths_are_probeable` 改 A/B/C 三段式 |

**零生产代码改动**（`hexbroker/` 无 diff），`.gitignore` 未动。

---

## 六、主理人裁决与教训

- **裁决**：按修复方案执行，CI 复跑后转绿即结案。
- **教训（已写入 SOP skill）**：**回归锁不得隐式假设本地开发环境**。
  本次是「接线校验门禁」的反面 —— 上一条教训是「测试用 tmp 假数据，抓不到接线缺陷」，
  这一条是「测试依赖本地真实产物，在 CI 上必然假失败」。
  通式：**环境依赖型断言必须在依赖缺失时给出确定性分支（skip 或正面验证缺失行为），
  而不是无条件依赖本地才有的东西**。
- **流程备注**：本次按 BugFix 快捷路径尝试组队，但本环境无 `software-engineer`
  agent 类型（`Task agent software-engineer is not available`），
  按既定约定降级为「主理人直接实施 + 自验」，全部验证证据如上。

---

## 七、遗留

- CI 复跑确认（推送后由 GitHub Actions 自动触发）。
- 先前遗留项仍未决：**P1-b** 配额重置后自动补刷；**P1-c** 数据源冗余；
  **P1-d** 08-27 日盘任务为何 0 落盘。
