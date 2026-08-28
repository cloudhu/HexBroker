# HexBroker 六方案 · 定量 QA 补全报告（18 终裁归档留项①闭环）

> 批次：1C/2A/1B/2B 定量 QA 补全 + 3B 数值口径诚实更正
> 日期：2026-08-28
> 性质：**定量补全**（status 零翻转）——云侠项目既有 walk-forward 产物为第一手数据源，
> HexBroker 治理账本/寄存器同步更新；非重新回测（产物自带 QA 链，铁律"缺陷翻转须 fresh-eyes"
> 不适用于本批次，因无任何状态翻转）。

## TL;DR

- **1C / 1B / 2B / 3B：`pending_qa` 解除**，精确定量从云侠 `temp/` 第一手产物回填账本。
- **2A：部分补全**（pass 率 Δ=+14.89pp 已录；ΔWR/ΔMaxDD 确无完整 fwd5 结算产物，诚实保留 `pending_qa=true` 待影子期）。
- **3B 诚实更正（P2 误录）**：`maxdd -0.15→0.1885`（组合口径）、`net_pnl -0.004→+0.0031`（fwd5 均收）、`PBO 0.61/DSR 0.20→null`（无计算证据）；**状态 NOT_PASS 不翻转**，更正留痕入 `ledger.history`。
- `source_doc` 六方案统一改为云侠产物路径（闭环 18 文档留项②引用偏差）。
- 联锁端到端复验：1C/2A→live、3B/1B/2B→shadow，判定不变。

---

## 一、四方案定案级定量（第一手产物）

### 1C · 跨年（PASS，解除 pending）
来源：`云侠 temp/bt_chase_tier_deep_out.json`（深快照 walk-forward，fwd=5）+ `temp/code_audit/quant_review_20260811.json`

| 窗口 | 组 | n | WR | 均收 |
|---|---|---:|---:|---:|
| IS2024 | would_allow | 2379 | 50.02% | +0.51% |
| IS2024 | would_reject | 337 | 42.43% | -0.08% |
| **OOS2025** | **would_allow** | **2077** | **58.69%** | **+1.30%** |
| OOS2025 | would_reject | 454 | 42.4% 量级 | — |
| 08月段（08-11 复核） | would_allow | 483 | **60.25%** | +2.05% |

红线 `would_allow WR≥45% ∧ n≥30`：三窗口全过 → **PASS 成立**。
分层单调性：allow > caution > reject（质量分层有效）。

### 2A · 大盘分档（PASS，部分补全，保留 pending）
来源：`temp/_bt_2a_aug_out.json` + `bt_regime_tiered_out.json` + `quant_review_20260811.json`

- 定量已录：开仓资格 **pass 率 Δ=+14.89pp**（fail-open 分档重判 boxRange→oscillatingUp，日级记录）。
- **诚实披露**：08-11 复核要求完整 fwd5 结算的 ΔWR/ΔMaxDD；08-04~08-07 候选仅部分结算
  （fwd_avail=3~0），**此后无完整结算产物** → ΔWR/ΔMaxDD 待影子期。
- 定案 PASS 为主理人基于 pass 率增益拍板（`pending_qa=true` 保留，联锁以 status=PASS 为准不受影响）。

### 1B · 试错（NOT_PASS，解除 pending）
来源：`temp/bt_1b_veto_counterfactual_out.json`（2026-08-09，深快照）

| 段 | n | fwd5 WR | 均收 | 判定 |
|---|---:|---:|---:|---|
| IS（2026-04 单月） | 53 | 90.57% | +9.80% | 单月幸存者段，不可外推 |
| **OOS（2026-05~07）** | **134** | **40.30%** | **-0.98%** | **WR<45% 红线 ∧ 均收<0** |
| 对照放行组 OOS | 24 | 37.5% | -0.54% | — |

`judgement_oos.verdict = veto_correct`（否决是保护）：被否决组 OOS 不达红线 → 放行不可行 →
**试错放宽准入 NOT_PASS 实锤**。月度分解：2026-05 WR 仅 17.65%（-3.88%）。

### 2B · 个股开关（NOT_PASS，解除 pending）
来源：`temp/bt_2b_dual_track_out_2025.json`（2025 全年 OOS，walk-forward warmup2024H1/IS2024/OOS2025；合规披露：2025 数据此前从未用于 2B 判定）

| 轨 | n（OOS 旧轨） | WR | MaxDD | 总收益 |
|---|---:|---:|---:|---:|
| old（单轨最优） | 815 | 58.77% | 34.63% | 1698.10% |
| dualA | — | 60% 量级 | 41.23% | 1284.57% |

- **dualA 五项 checks 全败**：总收益 1284.57% < 0.95× 门槛 1613.19%；MaxDD 41.23% > 34.63%；
  防御段 DD 降 -7.0%（门槛 30%/3pp）；进攻段 80.19% < 94.95%；1 cell REGRESSION。
- **dualB1**：总收益持平但防御段 DD 0 改善。
- `verdicts: dualA=NOT_PASS, dualB1=NOT_PASS` → **攻防不可兼得实锤**。

### 3B · 放量突破（NOT_PASS，数值口径更正，状态不翻转）
来源：`temp/bt_3b_oos2025_report.md`（2026-08-09 10:31 主回测，fwd5 结算率 100%）

| 指标 | 突破组 | 原逆向基线 |
|---|---:|---:|
| n（OOS 2025） | **183**（100% fwd 可得） | 2354 |
| WR（fwd5>0） | **49.73%**（91/183）≥45% 红线**通过** | 60.83% |
| 均收益 | **+0.31%** | +1.47%（**劣化**） |
| 组内 MaxDD（等权簿） | 4.38% | 17.34% |
| **组合 MaxDD** | — | **18.85% > 17.34%（不劣化铁律违反）** |

追高防线成立：乖离 vs fwd5 Pearson r=-0.1664，极端组突破交易 0 笔。

#### ⚠️ P2 误录更正（诚实披露，入 `ledger.history` 留痕）
| 字段 | P2 误录 | 更正后 | 依据 |
|---|---|---|---|
| maxdd | -0.15 | **0.1885**（组合口径） | 报告"组合 MaxDD 18.85%" |
| net_pnl_vs_cost | -0.004 | **0.0031**（fwd5 均收） | 报告"均收益 +0.31%" |
| gate2_pbo | 0.61 | **null**（无计算证据） | 报告无 PBO 计算 |
| gate2_dsr | 0.20 | **null**（无计算证据） | 报告无 DSR 计算 |
| gate1_dir_acc | 0.497 | 0.4973（91/183 精确） | 报告 |

> 根因表述同步更正：3B NOT_PASS 的真实死因 = **①组合 MaxDD 劣化 + ②均收劣化**（双项不劣化
> 铁律违反），而**非**"净均收为负 / PBO>0.5 / DSR<0"（P2 表述无证据）。状态 NOT_PASS 不变。

---

## 二、HexBroker 侧落地

| 文件 | 变更 |
|---|---|
| `data/governance/calibration_ledger.json` | 1C/1B/2B/3B `pending_qa→false` + 精确指标；2A 部分补全（保留 pending）；3B 数值更正 + `history` 留痕；六方案 `source_doc` → 云侠产物路径 |
| `configs/scheme_governance.yaml` | 同步精确 gate_metrics + 根因 + 口径注释（A 股红线 WR≥45%∧n≥20/30） |
| 测试 | 无需改动（`test_scheme_governance.py` 用临时构造，断言 3B n=183 保持成立） |

端到端冒烟：寄存器 `verify_all` 通过；联锁 `resolve_scheme_mode('live', sid)` → 1C/2A=live、3B/1B/2B=shadow（**判定与补全前一致，零行为变化**）；账本定量字段读取正确（3B maxdd=0.1885 / 1C n=2077 / 1B wr=0.403）。

---

## 三、QA 核验

1. **数据溯源**：六方案定量全部回溯至云侠 `temp/` 第一手产物（bt_*_out.json / *_report.md），
   与 08-08/08-09/08-11 权威会话定案数字交叉一致。
2. **状态零翻转**：六方案 status 与 18 终裁归档完全一致（PASS/DEPRECATED/NOT_PASS 无一变化）。
3. **诚实更正**：3B 三处误录按报告实值更正并留痕 history；无证据字段置 null 不编造。
4. **默认零行为变更**：联锁/寄存器/启动自检判定结果与补全前一致；仅数据登记更精确。
5. **2A 诚实保留 pending**：无完整 fwd5 结算产物即不编造 ΔWR/ΔMaxDD，待影子期。

**QA 裁决：PASS**（定量补全可信、更正留痕完整、零行为变化）。

---

## 四、留项（更新 18 文档留项①②为已闭环）

- ~~留项① 1C/2A/1B/2B 定量补全~~ → 本批次闭环（2A ΔWR/ΔMaxDD 子项转影子期待办）。
- ~~留项② ledger.source_doc 引用更正~~ → 本批次闭环（统一指向云侠产物路径）。
- 留项③ P2 运行时动态降级（Q5）→ 仍为后续批次。
- 新增待办：2A 影子期 ΔWR/ΔMaxDD 完整 fwd5 结算补全（需影子运行数据积累）。
