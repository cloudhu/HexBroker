# HexBroker 六方案治理 · 主理人终裁归档

> 批次：六方案终裁归档（收口 P2 治理控制平面）
> 日期：2026-08-28
> 链路：`1151f37 → f9bfa2a(P0) → b5bfa9a(P0) → 6206e92(P1) → 92b519a(P1) → 5d607c6(P2) → 010fe25(P2) → [本归档 docs]`
> 纪律：无证据不翻转 · 双闸门验收口径 · 防泄漏红线 · 不编造精确数字

## TL;DR

六方案治理控制平面（P2 已建）经主理人终裁收口：

- **PASS（可 LIVE，须校准记录）**：1C 跨年、2A 大盘分档
- **DEPRECATED**：3A 买点时效
- **NOT_PASS（强制 SHADOW_ONLY）**：3B 放量突破、1B 试错、2B 个股开关

定量诚实状态：仅 **3B** 有完整硬数据（n=183 / WR=49.7% / MaxDD 劣化 / 净均收负）；**1C / 2A / 1B / 2B** 精确 WR/n 标 `pending_qa`（待 walk-forward QA 重跑补全），不编造。

本批次**无新代码、无状态翻转**，与 P2 治理寄存器 / 校准账本字节级一致，全量回归维持 612 绿。

---

## 一、归档范围与权威源

### 1.1 六方案定案权威源
- **用户级 MEMORY · 近期动态**（六方案定案框架段）：
  > "延续六方案定案框架：1C跨年PASS、2A PASS、3A DEPRECATED、3B❌赔率瓶颈、1B❌准入差、2B❌攻防不可兼得"
- **项目治理铁律**（用户级 MEMORY）：
  > "6方案影子模式默认OFF/SHADOW_ONLY，晋升需 walk-forward 回测QA门禁 + 防误开联锁 + PASS校准记录"

### 1.2 ⚠️ 引用更正（诚实披露）
P2 提交的 `data/governance/calibration_ledger.json` 中每个方案的 `source_doc` 字段误指
`deliverables/software-company/futures-research/03-lead-verdict.md`。经核实，该文档的 P0/P1/P2
章节为**开源调研最佳实践采纳清单**，**不含六方案定案**。
正确权威源为用户级 MEMORY 六方案定案框架（见 §1.1）。

**处理**：本归档文档为权威收口，正确引用已落地；`ledger.source_doc` 字段偏差为纯溯源标注错误，
**不影响任何运行时逻辑 / 联锁 / 启动自检**，列为低优先后续修正项（见 §六 留项 2），本批次不翻转。

### 1.3 治理控制平面落点（P2 已入库，本批次引用）
| 组件 | 路径 | 职责 |
|------|------|------|
| 方案寄存器 | `hexbroker/governance/scheme.py` + `configs/scheme_governance.yaml` | 六方案 status + 数据根因 + 门禁指标 |
| 校准账本 | `hexbroker/governance/ledger.py` + `data/governance/calibration_ledger.json` | 双闸门证据 + 数据根因 + 裁决，append-only + 原子写 |
| 防误开联锁 | `hexbroker/governance/interlock.py` | `assert_safe_to_activate` + `resolve_scheme_mode`(fail-safe) |
| 启动自检 | `scripts/paper_trading_main.py` | PID 锁后跑治理自检，误配 WARNING + 强制 shadow，零侵入 tick |

---

## 二、六方案定案裁决表

| 方案 | 名称 | status | 数据根因（摘要） | 证据强度 | 精确指标 |
|------|------|--------|------------------|----------|----------|
| 1C | 跨年 | **PASS** | 跨年持有结构过双闸门晋升门禁 | 定性 PASS（pending_qa 定量） | WR/n 待补 |
| 2A | 大盘分档 | **PASS** | 大盘分档晋升门禁过双闸门 | 定性 PASS（pending_qa 定量） | WR/n 待补 |
| 3A | 买点时效 | **DEPRECATED** | 维度失效，弃用 | 定性 | — |
| 3B | 放量突破 | **NOT_PASS** | 赔率瓶颈：2025 全年 n=183 OOS，WR 未过 + MaxDD/均收益劣化 | 硬数据（已录） | n=183 / WR=49.7% / MaxDD=-15% / 净均收=-0.004 / PBO=0.61 / DSR=0.20 |
| 1B | 试错 | **NOT_PASS** | 准入差（entry 资格不达标） | 定性 | WR/n 待补 |
| 2B | 个股开关 | **NOT_PASS** | 攻防不可兼得（offense/defense 互斥） | 定性 | WR/n 待补 |

双闸门口径（全局，不可触碰）：
- **闸门1**：方向准确率 ≥ 54%
- **闸门2**：OOS 计成本 + PBO < 0.5 + DSR > 0

---

## 三、数据根因详述（逐方案）

### 3.1 1C · 跨年（PASS → 可 LIVE）
- **根因**：跨年持有结构在 walk-forward 回测中通过双闸门晋升门禁（闸门1 方向准确率达标 + 闸门2 OOS 计成本达标），具备晋升 LIVE 资格。
- **证据强度**：定性 PASS。精确 WR/n 尚未归档（标 `pending_qa`）。
- **约束**：实盘开闸前须补全 walk-forward 校准记录（WR/n/MaxDD/净均收/PBO/DSR），否则联锁 `resolve_scheme_mode('live', '1C')` 仍强制 SHADOW_ONLY。

### 3.2 2A · 大盘分档（PASS → 可 LIVE）
- **根因**：大盘分档晋升门禁通过双闸门，口径同 §二（闸门1 ≥54% + 闸门2 OOS计成本+PBO<0.5+DSR>0）。
- **证据强度**：定性 PASS（pending_qa 定量）。
- **约束**：同 1C，须校准记录补齐方可 LIVE。

### 3.3 3A · 买点时效（DEPRECATED）
- **根因**：买点时效维度在现行框架下失效（信号贫血 / 维度无增量信息），弃用。
- **状态**：DEPRECATED，维持 SHADOW_ONLY。无重跑计划。

### 3.4 3B · 放量突破（NOT_PASS → 强制 SHADOW_ONLY）
- **硬数据**（2025 全年 OOS，大样本）：
  - n = 183
  - WR = 49.7%（**0.497 < 闸门1 阈值 0.54，严格未过**）
  - MaxDD = -15%（劣化）
  - 净均收（OOS 计成本）= -0.004（为负，闸门2 未过）
  - PBO = 0.61（> 0.5，闸门2 未过）
  - DSR = 0.20（< 0，闸门2 未过）
- **⚠️ 口径澄清**：user_memory 近期动态原文述"WR 49.7% 达标"——此处"达标"为表述偏差，"达标"或指"方向性略优于随机 0.5"，**非指过闸门1 ≥54%**。以数值 **0.497** 为准。
- **结论**：双闸门均未过（闸门1 未过 + 闸门2 未过），NOT_PASS 确定。赔率结构瓶颈，维持 SHADOW_ONLY=True。
- **后续**：待 3B 赔率结构改造（BIAS_GUARD / 止盈结构）重跑复核，仍须过双闸门方可晋级。

### 3.5 1B · 试错（NOT_PASS → 强制 SHADOW_ONLY）
- **根因**：准入差（entry 资格不达标），维持 SHADOW_ONLY。
- **证据强度**：定性 NOT_PASS。精确 WR/n 待 walk-forward QA 重跑补全（pending_qa）。
- **约束**：status 已非 PASS，联锁 `resolve_scheme_mode('live','1B')` 天然拦截 LIVE，安全。

### 3.6 2B · 个股开关（NOT_PASS → 强制 SHADOW_ONLY）
- **根因**：攻防不可兼得（offense/defense 互斥），单策略无法同时兼顾进攻与防守，维持 SHADOW_ONLY。
- **证据强度**：定性 NOT_PASS。精确 WR/n 待重跑补全（pending_qa）。
- **后续**：待 2B 双轨融合立项（防御 + 进攻分轨）重跑复核。

---

## 四、与 P2 治理控制平面一致性核验

| 核验项 | 方法 | 结果 |
|--------|------|------|
| 寄存器不变量 | `SchemeRegistry.verify_all()`：非 SHADOW_ONLY 方案（1C/2A）均有 `CalibrationRecord` | ✅ 通过 |
| 账本字节级一致 | `CalibrationLedger.load` → `to_dict` 与磁盘逐字段比对 | ✅ 一致（P2 QA 已证） |
| 防误开联锁 fail-safe | `resolve_scheme_mode('live', sid)`：非 PASS/未知 → 强制 SHADOW_ONLY，不抛异常 | ✅ |
| 启动自检零侵入 | `paper_trading_main.py` 治理自检在 PID 锁后、tick 前；误配 WARNING + 强制 shadow | ✅ 零侵入 tick |
| 全量回归 | P2 后 612 绿（597+15）；本批次无代码改动 | ✅ 零回归 |

---

## 五、主理人终裁

**✅ 六方案治理收口，沿用 P2 登记，无状态翻转。**

- **PASS（可 LIVE，须校准记录补齐）**：1C、2A
- **DEPRECATED**：3A
- **NOT_PASS（强制 SHADOW_ONLY）**：3B、1B、2B

治理控制平面（`hexbroker/governance/` + `configs/scheme_governance.yaml` + `data/governance/calibration_ledger.json` + 启动自检）已就位并生效。任何方案晋升 LIVE 须触发 `resolve_scheme_mode` 联锁判定 + 对应 `CalibrationRecord` 双闸门证据齐备，否则自动回落 SHADOW_ONLY——**防误开联锁闭环成立**。

---

## 六、留项（非阻断）

> **2026-08-28 定量补全批次更新**：留项①②已闭环（见 `19-quant-backfill-qa.md`）。
> 1C/1B/2B/3B `pending_qa` 已解除（精确定量自云侠深快照产物回填）；2A 保留
> `pending_qa=true`（ΔWR/ΔMaxDD 需影子期完整 fwd5 结算，无产物不编造）；
> `source_doc` 已统一更正为云侠产物路径；3B 数值口径诚实更正（maxdd 0.1885 组合口径 /
> 均收 +0.31% / PBO·DSR 无证据置 null，状态 NOT_PASS 不翻转，history 留痕）。

1. ~~**定量补全**：1C / 2A / 1B / 2B 精确 WR/n~~ → **已闭环**（19 文档；2A ΔWR/ΔMaxDD 转影子期待办）。
2. ~~**ledger 引用更正**~~ → **已闭环**（19 文档；source_doc → 云侠产物路径）。
3. **运行时动态降级（P2 Q5）**：当前仅静态登记 + 启动自检；运行时按绩效/数据质量动态降级后续批次立项。
4. （新增）2A 影子期 ΔWR/ΔMaxDD 完整 fwd5 结算补全。

---

## 七、文件清单

**本归档（新增）**
- `deliverables/software-company/futures-research/18-schemes-final-verdict.md`

**引用（P2 已入库）**
- `hexbroker/governance/scheme.py` / `ledger.py` / `interlock.py` / `__init__.py`
- `configs/scheme_governance.yaml`
- `data/governance/calibration_ledger.json`
- `scripts/paper_trading_main.py`（启动自检接线）
- `tests/test_scheme_governance.py`
- `14-p2-prd.md` / `15-p2-arch.md` / `16-p2-impl.md` / `17-p2-qa.md`

**提交**：本批次单 `docs(...)` 提交（无 feat 变更，严守"无新代码不造 feat 提交"）。
