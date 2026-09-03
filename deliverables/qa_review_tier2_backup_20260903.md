# QA 独立复核报告 —— Tier-2 备源通道（P0 停摆事故修复）

> 复核人：严过关（Yan，QA）｜复核对象：commit `a31299f`（feat）+ `9d17146`（docs）
> 复核日期：2026-09-03｜方法：fresh-eyes，不假设实现正确；反例取证 + 变异测试
> 结论：**PASS（附条件）** —— 备源通道设计正确、护栏确未降级，但发现 **3 个 P0 缺口
> + 1 个结构性缺陷**，均已修复并补 18 个反例用例锁死。

---

## 一、结论摘要

| 复核重点 | 结论 | 说明 |
|---|---|---|
| **P0 护栏零降级** | 🟢 PASS | `RAW_ALIGN_TOL`/`K_TOL`/`MIN_OVERLAP` 一字未动；备源未洗白主路失败 |
| **P0 备源绝不自造后复权** | 🟢 PASS | `k_d` 确由 `graft_adjusted` 结果反解，无手工乘 k |
| **P0 fail-closed 完整性** | 🔴 **FAIL→已修** | 6 个失败分支中，**3 类脏数据全部可穿透**（见 P0-1/P0-2） |
| **P0 绝不写主湖** | 🟢 PASS | 真实主湖 181 文件 sha256 + mtime_ns 全量零变化（独立复核） |
| **P1 20 个测试是否假绿** | 🟢 PASS | 变异测试 6/6 击杀，断言真实（1 处环境依赖，见 🟡-4） |
| **P1 OHLC 包络 / 负零 k** | 🟡 **部分→已修** | 缩放保序成立，但备源脏数据不设防；负/零 k 结构性不可达 |
| **P1 G5 原子写 / 删除红线** | 🟢 PASS | `mkstemp + os.replace`，`audit_no_delete_calls` = CLEAN |
| **P2 重复测试定义** | 🟢 已清理 | 4 组逐字重复已删，**计数零影响（1223 不变）** |

---

## 二、🔴 P0 问题清单（已修复）

### P0-1 锚点退档 → 用旧段 k 外推，写出幽灵台阶（本轮最严重）

**文件**：`scripts/refresh_pull_local.py`（修复位置 L398–L405）

**机理**：`graft_adjusted` 内部 `t0 = common.max()` 是「两源重叠的最新日」，
**不是**「湖的最末日」。备源（sina/akshare）在湖末日有缺口时，t0 会**退档**；
若换月恰好发生在湖末日，取到的 `k0` 就是**旧段**复权比 → 新 bar 整段偏移。

**取证**（`artifacts/_tmp/qa_yan/probe_yan_rollover.py`）：

```
湖:   08-24~08-31 旧段 k=1.201347 ｜ 09-01 换月切新段 k=1.181055
备源: 覆盖 08-24~08-31 与 09-02，独缺 09-01
修复前 → ACCEPTED: close=3820.283460（隐含 k=1.201347，旧段）
         正确值    = 3755.754900（3180.0 × 1.181055）
         偏差      = +1.72%   ← 幽灵台阶
         info['warnings'] = []  ← 完全静默
```

**三道护栏为何全部接不住**：

| 护栏 | 修复前实测 | 为何失效 |
|---|---|---|
| `n_align < MIN_OVERLAP` | 6 ≥ 3 | 只数重叠样本**个数**，不看落在哪个复权段 |
| `anchor_gap_days > 7` | 2 ≤ 7 | 跨一次换月只需 2 天，远小于阈值 |
| 接缝跨源名义价 | 偏差 0.53% | 只查**名义价**连续性，查不出**复权比**跳段 |
| graft 告警 | 空 | 重叠区全在旧段 → 比值恒定 → 零告警 |

**正确做法（已实施）**：锚点必须落在**湖的最末一根 bar** 上。

```python
lake_last = pd.Timestamp(anchor.index.max())
if t0 != lake_last:
    raise ValueError(f"BACKUP_ANCHOR_NOT_LATEST: ...")
```

理由：k 的物理含义是「该日复权比」，只有湖末日的 k 才保证属于**当前**复权段；
备源覆盖不到湖末日 = 无法确知当前段 → 拒绝，而非猜。

---

### P0-2 NaN / 0 值 OHLC 穿透「缺列检查」，直落主湖成为零价 bar

**文件**：`scripts/refresh_pull_local.py`（修复位置 L438–L466）

**机理**：工程师新增的 `BACKUP_NO_OHLC` 只检查 **OHLC 列是否存在**，
不检查 **值是否可用**。sina/akshare 缺字段有两种常见编码 —— `NaN` 与 `0.0`，
两者都能通过「列存在」检查。

**后果链条**（已实证）：

```
备源 open=NaN / open=0.0
  → round(nan*k) = nan / 0.0
  → json.dumps 默认 allow_nan=True → 产出非标准字面量 NaN（严格 JSON 解析器拒收）
  → Python 解析器接受后，融合端 p6_4_fill_gaps.coerce_schema L557-558:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
  → NaN/0 一律落成 0.0
  → 主湖出现 open/high/low = 0 的零价 bar
```

**这正是工程师自己在 `hexbroker/data/backup.py` 的 `RawPull.frame` 注释里亲手定为红线的
结果**（「只给 close 会写出 open/high/low=0 的零价 bar，比不补更糟」）——
**缺列这条路被堵住了，NaN 与 0 值两条路敞着，终点完全一样。**

**取证**（`probe_yan.py`，7 个反例全部在修复前 ACCEPTED）：

| 反例 | 修复前 | 修复后 |
|---|---|---|
| 帧 `open=NaN` | ACCEPTED，json 含 `NaN`（非法 JSON） | `BACKUP_BAD_OHLC` |
| 帧 `open/high/low=0` | ACCEPTED，零价 bar | `BACKUP_BAD_OHLC` |
| 帧 `volume/OI=NaN` | ACCEPTED，json 含 `NaN` | `BACKUP_BAD_OHLC` |
| 帧 `low=5000 > high`（包络破坏） | ACCEPTED | `BACKUP_OHLC_INCOHERENT` |
| 备源仅 2 天重叠 | `BACKUP_ALIGN_INSUFFICIENT` ✅ | 同（本就正确） |
| 备源含未来日期 09-05 | 正确过滤 ✅ | 同（本就正确） |
| close 有 09-31、帧缺该行 | `BACKUP_NO_OHLC` ✅ | 同（本就正确） |

**正确做法（已实施）**：逐日校验 `open/high/low` 为**有限正值**、
`volume/open_interest` 为**有限非负值**，并校验包络
`low ≤ min(open, close) ∧ high ≥ max(open, close)`（名义价口径）。

---

### P0-3 备源落盘在 `try` 之外 → 序列化异常穿透 `main()`，18 品种全停

**文件**：`scripts/refresh_pull_local.py`（修复位置 L757–L790）

**机理**：工程师的 2b 备源分支把 `json.dumps` / `_atomic_write_text` 留在
`try/except` **之外**，与主路分支**不对称**：

- 主路分支：落盘在 `try` 内 → 异常只让**该品种**失败；
- 备源分支：落盘在 `try` 外 → 异常**穿透 `main()`**，整脚本崩溃。

违反项目 R22 原则（不引入新的全停失效模式）。该缺陷被我新增的
`allow_nan=False` 纵深防御触发并暴露（`ValueError: Out of range float values
are not JSON compliant`）。

**正确做法（已实施）**：把 payload 构造与落盘一并移入 `try`，
`ValueError`/`Exception` 分支统一计该品种失败 → 退出码 3。

---

## 三、🟡 P1 问题清单

### 🟡-1 `BACKUP_ALIGN_INSUFFICIENT` 分支零用例覆盖
工程师列出 6 个失败分支，该分支**唯独没有测试**。已补
`test_qa_align_insufficient_is_rejected`。

### 🟡-2 负 / 零 k 结构性不可达（可接受，仅记录）
`adj_hist` 经 `(close > 0) & (close == close)` 过滤，`raw` 经 `(raw > 0) & (raw == raw)`
过滤 → `k_d = adj/raw > 0` 恒成立，故不会写出荒谬值。**结论：无需额外防御。**
已补 `test_qa_negative_ohlc_is_rejected` 锁死负值输入。

### 🟡-3 备源品种数无上限，全量走备源仍返回 0
若 tqsdk 返回全脏，18/18 会走备源并返回退出码 0，融合脚本照常推进 ——
但续接段 graft 明确标注 `provisional=True`。仅靠 `_LOCAL_REPORT.md` 与
`_SEAM_STATUS.json.backup` 留痕，**编排层自动化目前不消费 `backup` 字段**。
建议：编排层加一道「备源品种数 > N 则人工确认」的闸门（归 team-lead，未擅改）。

### 🟡-4 契约对质用例依赖仓库内 artifact，环境缺失时静默跳过
`test_e2e_backup_json_matches_pandadata_contract` 的
`if ref.exists():` 在 `artifacts/p6_4_pull_20260903/ag0.json` 不存在时**静默空转**。
今日（16:25 彩跑后）该文件存在，**检查确实执行**；但在干净 checkout / CI /
artifacts 清理后会退化为无断言。建议改为 `pytest.skip(...)` 显式留痕。

### 🟡-5 接缝护栏在湖缺 `raw_close` 列时 fail-open
`_seam_nominal_break` 遇 NaN/≤0 返回 `None`（放行）。rb0 实测 `raw_close` 零 NaN，
今日不触发；属既有设计（R22），仅记录。

---

## 四、🟢 已验证通过项（独立取证，非采信自述）

| 项 | 取证方式 | 结果 |
|---|---|---|
| 护栏常量一字未动 | `git diff a31299f^ a31299f` 逐行 + 独立断言 | `0.005 / 5e-4 / 3` 全等 |
| k 来自 graft 反解 | 读 `graft.py` L220/L274-276 + 实盘值反算 | `k_d ≡ k0`，无手工乘 k |
| 换月日 k 跳变 | 实盘 rb0 锚在 09-01（新段），`k=1.181055` | 与湖末值逐位一致 ✅ |
| **绝不写主湖** | 真实主湖 181 文件 sha256 + mtime_ns 前后全量对比 | **零改动** ✅ |
| 主路成功不触发备源 | 变异测试 M6 | 击杀 ✅ |
| `save=False` | 变异测试 M1b | 击杀 ✅ |
| G5 原子写 | 读码 + `audit_no_delete_calls` | `mkstemp+os.replace`，CLEAN |
| 删除红线 | 对 4 个改动文件跑审计 | 全 CLEAN |

### 变异测试（证明测试有牙齿，非假绿）

对生产代码注入 10 处缺陷，全部被测试击杀：

| # | 变异 | 结果 |
|---|---|---|
| M1b | `save=False` → `True` | 击杀（工程师用例） |
| M2 | `k_d` 硬编码 `1.0`（自造复权） | 击杀 |
| M3 | 删除 OHLC 缺列检查 | 击杀 |
| M4 | `MIN_OVERLAP` 3→1 | 击杀 |
| M5 | 锚点间隔阈值 7→365 | 击杀 |
| M6 | 主路成功也走备源 | 击杀（工程师用例） |
| M7 | 禁用新护栏 `ANCHOR_NOT_LATEST` | 击杀（新增用例） |
| M8 | `allow_nan=False` → `True` | 击杀（新增用例） |
| M9 | 禁用新护栏 `BAD_OHLC` | 击杀（新增用例） |
| M10 | 禁用包络护栏 | 击杀（新增用例） |

> 注：首轮 M1 曾误报「未被击杀」，系我的变异锚点命中**文档字符串**（L296）而非
> 真实代码（L321）导致 —— 已改用「锚点必须唯一」校验重跑，M1b 证实工程师用例有效。
> 反向印证：本文件 L71/L296 的文档字符串与 L321 代码存在**同文本重复**，
> 是变异测试与未来重构的陷阱，建议后续收敛。

---

## 五、回归计数（junitxml 权威）

| 轮次 | tests | failures | errors | skipped |
|---|---|---|---|---|
| r1（复核基线，工程师代码） | **1223** | 0 | 0 | 0 |
| r2（修复 + 新增 18 用例） | **1241** | 0 | 0 | 0 |
| r3（最终确认） | **1241** | 0 | 0 | 0 |

- 增量 **+18** = 新增反例用例数；P2 去重**计数零影响**（该文件采集数 12 → 12）。
- 全量命令：`pytest --junitxml=artifacts/_tmp/junit_qa_yan_r3.xml`（**不带路径参数**）。

---

## 六、今晚 20:00 预检（新护栏会不会误杀）

对 18 品种逐一核对「备源是否覆盖湖末日 2026-09-01」：

```
会被新护栏硬拒的品种：无 —— 今晚 18/18 可安全走备源 ✅
```

（`artifacts/_tmp/qa_yan/preflight_anchor.py`；ag0 备源止于 09-02，其余 17 个止于 09-03，
**均覆盖湖末日**。）

⚠️ **需 team-lead 知悉的行为变化**：P0-1 修复把「静默写错 k」转成「硬失败（退出码 3）」。
若某品种备源在湖末日出现缺口，该品种会**停在原地并报 `BACKUP_ANCHOR_NOT_LATEST`**
（pandadata 未接线 → 无法兜底）。这是红线取舍：宁可不补，不可写错口径。
今晚预检 18/18 通过，风险为零；长期仍需主源恢复后重建。

---

## 七、残留风险（未在本轮处理，归 team-lead 决策）

1. **备源续接段无主源校验**：graft 返回 `provisional=True`，报告与
   `_SEAM_STATUS.json.backup` 已留痕，但**编排层不消费**，主源恢复后无自动重建路径。
2. **备源无上游冗余**：sina 与 akshare 同源，仅解析层冗余。
3. **续接窗口内换月不做 k 分段**：`graft_adjusted` 未传 `rollover_spreads`
   时跳过换月调整（依据 18 品种消融：近似价差为净损害）。当前 2 天缺口无影响，
   若缺口拉长至跨换月则续接值会失真。
4. **`include_today` 自动判定**：本地 16:42 判定为「含今日」。日盘 15:00 收盘后
   日线 bar 已完整，本次取证 rb0/hc0 的 09-03 值与 sina 实时一致，未见占位/陈旧。
   建议 20:00 自动化显式传 `--exclude-today/--include-today`，勿依赖时间窗自动判定。

---

## 八、本轮改动清单

| 文件 | 改动 |
|---|---|
| `scripts/refresh_pull_local.py` | +65/-6：3 道新护栏（P0-1/2）+ 落盘移入 try（P0-3）+ `allow_nan=False` |
| `tests/test_refresh_pull_local_backup_failclosed.py` | 新增，18 个反例契约用例 |
| `tests/test_refresh_pull_local.py` | 删除 4 组逐字重复定义（208→169 行，计数零影响） |
| `deliverables/qa_review_tier2_backup_20260903.md` | 本报告 |

取证脚本（本地留存，已 gitignore）：
`artifacts/_tmp/qa_yan/{probe_yan.py, probe_yan_rollover.py, lake_guard.py,
preflight_anchor.py, mutate.sh, mutate2.sh}`
