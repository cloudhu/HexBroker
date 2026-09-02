# Part A+B 成对实施 · 换月接缝韧性修复（2026-09-02）

> 裁决来源：主理人 2026-09-02 09:45 裁决「Part A+B 成对实施」。
> 方案依据：`deliverables/hexbroker_seam_basis_conflict_diagnosis_20260901.md` §三。
> 执行纪律：先读当前文件 + 实地取证（无证据不翻转）；改动最小化；测试锁定行为。

---

## TL;DR

| 项 | 结论 |
|---|---|
| Part A | ✅ `refresh_pull_local.py` 接缝三分判定落地：领先窗口软放行 + 两处 fail-closed 收紧 + `_SEAM_STATUS.json` 机器可读状态 |
| Part B | ✅ 编排层（08:00/20:00 自动化）按 seam 状态**定向** pandadata 补数，全量兜底仅留 exit≠0 场景 |
| 端到端验证 | ✅ 09-01 现场（昨日 18/18 硬拒的同一输入）复跑：rb0 软放行、接缝日不发射、exit 0 |
| 回归 | ✅ 全量 pytest 通过（见 §五），新增 6 个锁定用例 |
| 🆕 新发现 | 🔴 **cu0/ni0 湖内 k 伪台阶疑似污染**（0820~0825，扩展日旧约排放向量）——疑似未定罪，已单独立项待独立源复核 |
| 今晚预判 | rb0 若 tqsdk 已切约 → 自愈 exit 0；若仍旧约 → 首晚场景 ext 非空 → 硬拒 exit 3 → pandadata 全量兜底（与现状同，安全） |

---

## 一、取证链（动工前）

1. **编排流程确认**（读 08:00/20:00/23:30 三个自动化 prompt）：湖**只**经 JSON 应用管线更新（2a tqsdk 主路 → 2b 仅 exit≠0 才 pandadata 全量兜底 → 2c 应用器融合 + p22 重建）；无独立 pandadata 湖写入步骤。08-31 晚报告 `seam=0831` 证明当晚有多轮执行（20:00 + 23:30 补刷共用目录、报告被覆盖）。
2. **应用器契约确认**（`p6_4_fill_gaps.py` L494-533）：parse 落湖时 `raw_close = close`（后复权复制品），靠 `enrich_raw_close`（P1-c）从外部备源回填真实名义价；`underlying_symbol` 按大写基础代码过滤。
3. **湖内 k 序列全量扫描**（18 品种 × 0814~0901）：rb0/hc0/j0/ta0/al0 单台阶、段内恒定至 1e-10（干净）；**cu0 三次台阶（0821/0824/0825）、ni0 两次台阶 + 段内 1e-4 抖动（0820~0824）**，违反「段内 k 恒定」不变量，与 08-31 报告中两者 5 个基准冲突日（tqsdk 滞后 ≥4 天）时空吻合 → **疑似**「扩展日旧约排放」污染向量（seam∈tail + ext 非空路径，ROLLOVER 守卫对 ~1.7% 跳变失明）。**按铁律未定罪**，待独立源逐位复核（§六 P-NEW）。

## 二、Part A 实现（`scripts/refresh_pull_local.py`）

1. **接缝三分判定抽纯函数 `_seam_decision`**（可单测）：
   - 接缝日 ∈ 对齐稳定段 → `OK`（不变）；
   - 接缝日 ∉ 稳定段 且 **ext 为空**（湖不滞后于 tqsdk）→ 换月领先窗口 → 软放行 `ROLLOVER_LEAD_WINDOW`（旧版此处硬拒 = 09-01 18/18 误杀根因）；
   - 接缝日 ∉ 稳定段 且 **ext 非空** → **仍硬拒**（安全护栏不削弱）。
2. **两处 fail-closed 收紧**：
   - 软放行前提：湖必须**确实含**接缝日行，缺失 → 硬拒（「湖内缺失行 → fail-closed 拒绝」），绝不静默跳过数据；
   - 发射逻辑不变：接缝日既不在 tail 也不在 ext → 天然不发射（不把旧主力价写进缓存）。
3. **`_SEAM_STATUS.json`**（`{asof, statuses, lead_window}`）随报告落盘，供编排层消费。
4. `_LOCAL_REPORT.md` 表格新增「接缝」列。

## 三、接线陷阱（顺带修复）

`p6_4_apply_persisted_dir.py` 原 `glob("*.json")` 会把 `_SEAM_STATUS.json` 误当品种文件（sym0=`_SEAM_STATUS` → p6_4 parse 品种映射失败 → 整批报错）。修复：提取 `_list_persisted_json(d)`，排除 `_*.json` 元数据与 `*.clean.json` 中转件，+ 测试锁定。

## 四、Part B 实现（编排层自动化 prompt）

08:00 / 20:00 两个刷新自动化在 2a 与 2b 之间新增 **a2 定向补数步骤**：

- 2a exit=0 且 `_SEAM_STATUS.json` 含 `ROLLOVER_LEAD_WINDOW` 品种 → **仅对这些品种**走 pandadata `get_future_daily_post`（14 天窗口）覆盖同名 JSON，以权威口径补齐/校正接缝日新主力 bar；
- statuses 全 OK → 跳过；pandadata 不可用 → 如实报告跳过（**不**判 2a 失败——软放行下湖已含接缝日行，管线自洽）；
- 2b 全量兜底仅在 exit≠0 时触发（不变）；23:30 补刷自动化为 pandadata-only 兜底，无需改动。

效果：换月日 pandadata 依赖从「全量 18 品种 × 多日」降为「仅领先窗口品种 × 接缝日」，落实 tqsdk 主路韧性立项目标。

## 五、验证

| 层 | 用例 | 结果 |
|---|---|---|
| 单元 | `_seam_decision` 四分支 + 发射排除锁定（6 用例，`tests/test_refresh_pull_local.py` / `test_p6_4_pull_failure_gate.py`） | ✅ 14/14 |
| 端到端 | `--asof 2026-09-01 --symbols rb0`（昨日硬拒同一输入，隔离输出目录、湖只读） | ✅ `ROLLOVER_LEAD_WINDOW`、k=1.201347（旧段锚）、发射止于 0828、exit 0、`_SEAM_STATUS.json` 正确 |
| 回归 | 全量 pytest（junitxml `artifacts/_tmp/_junit_full_part_ab.xml`） | ✅ 全绿（基线 1127 + 新增 6） |

## 六、遗留与待裁决

| 编号 | 事项 | 优先级 |
|---|---|---|
| **P-NEW** | **cu0/ni0 湖内 k 伪台阶疑似污染**（0820~0825，扩展日旧约排放向量）：需用独立名义源（sina 逐位）+ 合约级数据定罪/豁免；若定罪，按 p2 备份→原子写→逐位重建三件套修复 4~9 行。同向量仍存在于「seam∈tail + ext 非空」路径（Part A 未覆盖，ROLLOVER 守卫对 <9.5% 跳变失明）——可评估加「扩展日 vs 湖末行名义价连续性」护栏 | P1（建议下轮裁决） |
| P1-2 | hc0 类 SCALE_UNSTABLE 修法（A/B/C 选项）仍待裁决；今晚大概率自愈 | P1 |
| P2-1 / P2-2 | night 守卫时段错配 / include-today 占位 bar 入口护栏 | P2 |
| 纯天勤补点代码化 | rb2701 补点法（直连合约 + 湖 k 锚）代码化需扩展 `TqsdkSource`（新增直连合约拉取能力），本轮未做——Part B 已用 pandadata 定向补数达成同等保障，纯天勤版留作后续优化 | P2 |

## 七、改动文件清单

- `scripts/refresh_pull_local.py`（Part A：`_seam_decision` + 接缝块重构 + `_SEAM_STATUS.json` + 报告列 + 文档）
- `scripts/p6_4_apply_persisted_dir.py`（`_list_persisted_json` 元数据排除）
- `tests/test_refresh_pull_local.py`（+5 用例）
- `tests/test_p6_4_pull_failure_gate.py`（+1 用例）
- 自动化 prompt ×2（08:00 / 20:00，Part B a2 步骤）
