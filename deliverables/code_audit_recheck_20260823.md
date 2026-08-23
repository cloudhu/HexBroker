# HexBroker 代码审核报告复核裁决（2026-08-23）

> 复核对象：`deliverables/code_audit_20260823.md`（《HexBroker 代码审核报告（2026-08-23）》，6 轮 / 约 47 处定点修复 / 声称全量 315/315）
> 复核方法：QA 严过关 fresh-eyes 独立复核（17 项源码实证 + 22 个回归测试断言质量 + 全量 pytest 实测 + git diff 核查）→ 主理人齐活林独立抽验 → 终裁
> 主理人裁决时间：2026-08-23

---

## TL;DR

**审核报告基本可信（✅ 属实），修复质量评级「良」（B+，接近优）。全部收尾条件已闭环（git 提交链 `9c290a4`→`dbd5a61`→`05ad068`→`6922dbf`→`a2c116c`→`3053a16`，叠加既有 `876117a`/`ef149fd`/`4c04362`/`bb0ff3b`），全量 pytest 315/315 绿。本报告已按问题级将修复提交逐一标记（见 §2.6 映射表），审计 31 个问题全部有提交可溯源，无未提交残余。**

---

## 一、复核证据链（三重验证）

### 1.1 QA fresh-eyes 独立复核（严过关，03:05 报告）

| 维度 | 结果 |
|---|---|
| 源码实证（17 项抽查 F2/F3/F4/F5/F7/E2/P1/P2/P5/P4/L2/L6/L3/L4/L5/V1/V5/L7/E3-A/B） | ✅ **17/17 属实**，均给出 文件:行 + 关键代码 证据 |
| 补充抽查（F1/F6/L1/E1/V2/E4/P8/L8/V4/L9） | ✅ 全部属实 |
| 测试存在性（22 个文件） | ✅ **22/22 存在**，断言均为"修复后行为"（失败即暴露回归），评级 A |
| 全量 pytest 实测 | ✅ **315/315 通过（65.98s），失败 0 错误 0**，EXIT=0，无 warnings 汇总 |
| 回归文件单独复核 | 20 个回归文件 65 passed / 2.36s |
| 关键修复运行时验证 | PBO 返回 0.45（非恒 0.5）；DSR(1.5,500,10)=0.9458、负偏态=0.0（非恒≈1）；E3-B scale 透传=7（非 1） |
| 未提交改动核查 | ⚠️ 发现 P0 组 8 文件 M 状态 + test_tokenizer.py untracked（详见 §2.1） |

### 1.2 主理人独立抽验（本裁决补充）

| 核验项 | 结果 |
|---|---|
| 报告点名的 22 个回归测试文件存在性 | ✅ 全部存在于 `tests/` |
| git 提交与报告清单吻合性 | ✅ `876117a`(P1组) / `ef149fd`(BatchA) / `4c04362`(BatchB) / `bb0ff3b`(L7/E3) 改动文件与报告二-C/D/E/F 完全对应 |
| **P0 组未提交**（QA 关键发现复核） | ✅ **属实**：broker.py(P7)、cleaner.py(L1)、dataset.py(P3)、schema.py(F7)、report.py(E1)、technical.py(F6)、tokenizer.py(L2)、timeutil.py(F1) 全为 `M`；test_tokenizer.py 为 `??`；git 全历史无 P0 组修复提交 |
| 残留脚本 | ✅ `_fix_e3.py`（116 行 E3 行级补丁脚本）untracked 残留 |

---

## 二、主理人裁决

### 2.1 判定

- **报告真实性：✅ 基本可信**。全部核心缺陷修复（PBO 闸门、一致性验收、PPO 梯度、风控红线、DSR 真值、L1–L9 泄漏、V1–V5、L7/E3 收官）经「独立源码实证 + 运行时验证 + 回归测试」三重确认均为真实修复；315/315 实测属实，无"声称修复但实际未修复"项，无测试复刻 bug 逻辑，无回归迹象。
- **修复质量：良（B+，接近优）**。数学类修复（P1/P2 PPO 有限差分、E2 DSR）有机制级验证；泄漏类（L2/L6）测试构造严谨（增删未来值、embargo 间隙、重复 split 逐字节一致）；守「无证据不翻转」（E3-A 默认口径不变、V5 未动生产 CONTRACTS18）。扣分项：P0 组未入 git + 报告数字自洽性瑕疵。

### 2.2 遗留问题（按优先级）

| 优先级 | 问题 | 处置建议 |
|---|---|---|
| 🔴 P0（环境） | `pyproject.toml` 无运行依赖声明，新人无法复现 | 报告已自认。建议补 `dependencies` + 固化激活说明（可后续处理，作为已知问题放行） |
| 🟠 P1（交付完整性） | **P0 组修复未提交 git**：broker.py / dataset.py / cleaner.py / tokenizer.py / schema.py / technical.py / timeutil.py / report.py + tests/test_tokenizer.py | **归档前必须补提交**（feat + docs 双提交），否则任何 checkout/stash 即丢失 P0 修复 |
| 🟡 P2（自洽性/卫生） | ① 报告行 13"315=90+305+10"算术错（应为 305+10=315）；② P0 轮"新增 7 测试"但全量仍写 264/264（应为 271）；③ BatchB "较 298 新增 11"却报 305（应为 309）；④ L7 报告 ×6 实为 7 用例、E3-B 报告 ×2 实为 1 函数；⑤ 根目录 `_fix_e3.py` 残留 | ✅ 已全部闭环（`dbd5a61`/`05ad068` + 删除 `_fix_e3.py`） |
| 🟡 P2（提交残余） | ~~F1 的 `scripts/compare_ensemble.py` 与 F8（ruff 清理 57+ 文件）未提交~~ | ✅ **已提交 `3053a16`**（2026-08-23 12:42）：57 个 scripts/ 文件 F401/F541/E713 安全清理（+47/-120 行为不变）+ compare_ensemble 补 typing.Any；提交前逐文件甄别无逻辑改动混入，pytest 315/315 绿 |

### 2.3 终裁：✅ 有条件批准归档 —— **已执行闭环（2026-08-23 12:10 更新）**

1. **补提交 P0 组修复**（8 个源码文件 + tests/test_tokenizer.py），按项目 git check-in 惯例拆 feat/docs 双提交；
2. **删除或归档** `_fix_e3.py` 残留脚本；
3. **修正报告算术自洽问题**及 L7/E3-B 用例数表述；
4. **环境缺口（pyproject 依赖）** 作已知问题保留 P0 标记，在归档说明中明示。

### 2.4 收尾执行记录（工程师寇豆码执行 → QA 严过关提交后回归确认 → 主理人终验）

| 条件 | 执行结果 | 证据 |
|---|---|---|
| ① P0 组补提交 | ✅ 完成 | **feat `9c290a4`**：恰 9 文件（8 源码 + test_tokenizer.py），+162/-31，无杂项卷入；抽查 P7/L2 diff 内容正确 |
| ② 清理 `_fix_e3.py` | ✅ 完成 | 文件已删除（git status 0 命中） |
| ③ 报告修正 | ✅ 完成（含 QA 发现的一处错误修正回退） | **docs `dbd5a61`**（315 构成/271/309/L7×7/E3-B×1 函数 3 断言/勘误）→ QA 回归发现"309"与二-F 矛盾 → **docs `05ad068`** 回退二-E 为 **305/305**（净增 7 用例：test_feature_normalize 为既有文件改写）+ 勘误行追加。最终数字链：二-E 305 + 二-F 新增 10 = **315** ✅ 自洽 |
| ④ 全量 pytest | ✅ 315/315 绿 | QA 独立重跑（~66s EXIT=0）；工程师 3 次 EXIT=0 |
| 提交后 git 状态 | ✅ 干净 | 9 个 P0 文件 clean；报告 clean；剩余 108 项（88M+20??）为历史遗留，无新增异常 |

> **归档状态：✅ 已闭环（4/4 条件全部满足）**。条件 1–3 已闭环（git 提交链 `9c290a4` → `dbd5a61` → `05ad068`）；**条件 4（pyproject 依赖缺口）亦已于 12:10 后闭环**（见 2.5）。

### 2.5 条件 4 执行记录（pyproject 运行依赖缺口，工程师寇豆码执行 → QA 严过关独立验证 → 主理人终验）

| 步骤 | 结果 | 证据 |
|---|---|---|
| 修复 | ✅ 16 项核心依赖补入 `[project].dependencies`（版本约束同 requirements.txt 对齐 Kronos pin）；pytest 移入新增 `dev = ["pytest>=8"]` extra；torch/sb3/optional 三组原样保留 | **feat `6922dbf`**（3 文件 +38/-1） |
| 激活说明 | ✅ README 新增「安装」小节（推荐 `pip install -e .[dev]`）；requirements.txt 声明 pyproject 为单一事实源 | 同 6922dbf |
| QA 独立验证 | ✅ 修复正确、有条件批准：16 项逐项比对一致；负面测试 `pip install --dry-run -e .`（带依赖解析）13s 成功无冲突；环境隔离 OK（未污染 envs/default）；315/315 绿 | QA 报告（3m35s） |
| QA 指出低危措辞 | ⚠️ requirements/README 原写"17 行与 pyproject 同步"，实为 16 项核心 + pytest(dev) | 工程师 **docs `a2c116c`** 校准（2 文件 +5/-4） |
| 主理人终验 | ✅ 提交链 6922dbf→a2c116c 就位；pyproject dependencies 16 项落地；三文件 clean | git log/status |

> 附注（已处理，见 2.7）：① env 的 torch 2.11 不满足 torch extra `<2.6` 约束 → **已校准为 `<3`**；② gymnasium 为硬依赖但全仓库无 `import gymnasium` → **已移入 sb3 extra**。

### 2.7 已知问题闭环（2026-08-23 12:46）

| 问题 | 处置 | 证据 |
|---|---|---|
| torch extra `<2.6` 与 env torch 2.11 错位 | ✅ 放宽为 `torch>=2.1,<3`（env 实测 2.11+cu128 跑 Kronos 微调正常，上限过时）；同步 requirements-optional.txt | **chore `e29a24d`**（3 文件 +12/-8） |
| gymnasium 硬依赖零引用 | ✅ 从核心 dependencies 移入 `[sb3]` extra（sb3 传递依赖显式声明）；核心 16→15 项 | 同 e29a24d |

> 验证：tomllib 解析 deps=15 且 gymnasium not in core、extras 四组正确；pip 构建元数据 done；pytest 315 passed（65.01s）。**至此全部已知问题清零**。

### 2.6 修复提交映射表（问题级，2026-08-23 12:22 更新）

> 将审核报告每个问题条目标记到其修复提交（`git log -S` 逐项定位）。提交归属：`9c290a4`=P0 组，`876117a`=P1 组，`ef149fd`=P2 Batch A，`4c04362`=P2 Batch B，`bb0ff3b`=L7/E3 收官，`6922dbf`+`a2c116c`=环境。

| 问题 | 修复提交 | 说明 |
|---|---|---|
| **第一轮 F 组** | | |
| F1 缺 `from typing import Any` | `9c290a4`（timeutil.py）+ `4c04362`（cross.py，随 L5 带入）+ `3053a16`（scripts/compare_ensemble.py） | 3 文件分属 3 提交，全部闭环 |
| F2 PBO 闸门失效（`is` 恒 False） | `bb0ff3b` | 随 pipeline.py（E3 同文件）一并提交 |
| F3 反手 avg_entry 未重置 | `9c290a4`（代码）+ `5477622`（回归测试补提交） | test_flip_resets_avg_entry_to_new_fill 曾遗漏于工作区，已补提交 |
| F4 reset 不清 _records/_target_rows | `876117a` | |
| F5 bars_in_position 布尔化 | `876117a` | |
| F6 RSI 全涨=0 | `9c290a4` | `f_rsi.mask(loss == 0, 100.0)` |
| F7 ffill 未按品种分组 | `9c290a4` | `groupby(level=0).ffill(limit=1)` |
| F8 ruff 安全清理 196 项 | `3053a16`（scripts/ 57 文件）+ `6c1c9ce`（hexbroker/ 17 + tests/ 9 残余） | 行为不变（F401/F541/E713，净删 156 行） |
| **P0 组** | | |
| E1 缺盈亏比(profit_factor) | `ef149fd`（metrics.py 字段+to_dict）+ `9c290a4`（report.py PF 行） | **跨 2 提交** |
| P3 尾部标签伪造看涨 | `9c290a4` | 尾部填 np.nan 剔除 + `__len__` off-by-one |
| P7 平今费不生效（成本低估） | `9c290a4` | open_dates 跟踪 + `_compute_is_today_close` |
| L1 winsorize 全样本泄漏 | `9c290a4` | `expanding().quantile` 滚动因果裁剪 |
| L2 tokenizer 未来函数 | `9c290a4` | transform 逐行 expanding + n_bins<4 ValueError + test_tokenizer.py |
| **P1 组** | | |
| P1 PPO 梯度空操作 | `876117a` | `_ppo_policy_grad_logits` + 有限差分测试 |
| P2 d_r 冗余 ratio | `876117a` | 同上 |
| P5 trailing_stop 死代码/ratchet 反向 | `876117a` | manager 接线 + `int(new)<=int(tier)` |
| P6 env 缺 S1/S2/S5 上下文 | `876117a` | `_market_context` 透传 |
| E2 DSR/PBO 恒真 | `876117a` | Bailey–López de Prado 偏度/峰度感知式 |
| L6 splitter 非幂等 | `876117a` | 局部 cur_train_len + embargo |
| **P2 Batch A** | | |
| L9 signal_store 无 OOS 校验 | `ef149fd` | put() 丢弃样本内信号 |
| P4 预算从不约束 | `ef149fd` | `min(abs(budget), max_position_pct)` |
| V4 CTP 凭证仅 print | `ef149fd` | `raise CTPGuardError` |
| L8 Kronos 配对绕过 | `ef149fd` | 构造即 validate_kronos_pairing |
| V2 Optuna 剪枝失效 | `ef149fd` | MedianPruner + report/should_prune |
| V3 PSI 虚假漂移 | `ef149fd` | finite 掩码 + 分箱去重 + floor 钳制 |
| E4 walkforward 口径 | `ef149fd` | 聚合 PF + 短折跳年化 + ddof=1 |
| P8 涨跌停乐观成交 | `ef149fd` | limit_up/down 拦截 |
| **P2 Batch B** | | |
| L3 归一器跨品种锁列 | `4c04362` | 每品种新建 RollingNormalizer |
| L4 外盘 reindex 全 NaN | `4c04362` | `asof` 对齐 |
| L5 pivot 短名静默均值 | `4c04362` | 按短名去重 |
| V1 LSTM c 重置 + 训练集选种 | `4c04362` | c 移循环外 + 验证集 best_global_val |
| V5 contracts 乘数规格 | `4c04362` | `_SPEC_MULTIPLIER/_SPEC_MIN_TICK` 回退 |
| **收官轮** | | |
| L7 pytdx 主力/市场硬编码 | `bb0ff3b` | 跨市场枚举 + chicang 选主力 + position 兼容 |
| E3 is_effective 不可达/RL 单品种 | `bb0ff3b` | 工作副本纳入 + 全品种 loop + scale 透传 |
| **环境** | | |
| pyproject 无运行依赖声明 | `6922dbf` + `a2c116c` | 16 项 dependencies + dev extra + README 安装小节 |

**提交覆盖统计**：`9c290a4` 10 项 ｜ `876117a` 6 项 ｜ `ef149fd` 9 项（含 E1 部分）｜ `4c04362` 6 项（含 F1 部分）｜ `bb0ff3b` 3 项（含 F2）｜ `6922dbf`/`a2c116c` 1 项（环境）｜ `3053a16` 2 项（F1-compare_ensemble + F8-scripts）｜ `6c1c9ce`（F8-hexbroker/tests 残余）｜ `5477622`（F3 回归测试补提交）。
**未提交残余**：~~F1-compare_ensemble、F8~~ → **已全部提交**；~~F3 回归测试~~ → **已补提交 `5477622`**。31 个问题全部有提交可溯源，无代码待办残留（仅 trade_plans/公众号业务数据未跟踪/未提交）。

---

## 三、QA 复核报告要点摘录

- **修复质量评级（QA）**：P1/P2（PPO 梯度）A、P5/P6（风控红线）A、E2（DSR）A、L2/L6（泄漏）A、L3/L4/L5/V1/V5（Batch B）A、L7/E3（收官）A、P3/P7/L1（P0 组）B+（代码正确但未提交）。
- **测试断言质量**：22/22 全部 A 级——显式排除 bug 区间（F3）、有限差分 atol=1e-4（P1/P2）、旧实现对照（L4/L5/V1）、monkeypatch 反转训练/验证（V1）、含/不含列数值全相等验证不翻转（E3-A）。
- **未提交改动全貌**：git status 117 项（M≈96 + ??≈21），其中审计修复未提交 8 文件 + 1 测试；ruff F8 清理约 40+ 文件（仅删未用 import，低风险）；trade_plans/weight 等业务产物正常；`_fix_e3.py` 残留。

---

## 四、文件清单

| 文件 | 说明 |
|---|---|
| `deliverables/code_audit_20260823.md` | 被复核的审核报告（已按收尾条件修正数字自洽，随 `dbd5a61`/`05ad068` 入库） |
| `deliverables/code_audit_recheck_20260823.md` | **本裁决报告（新增，含收尾执行记录）** |

## 五、后续行动建议

1. ✅ 已完成：P0 组修复补提交（feat `9c290a4`）、`_fix_e3.py` 清理、报告数字自洽（`dbd5a61`/`05ad068`）、pyproject 依赖声明（`6922dbf`/`a2c116c`）、**F1-compare_ensemble + F8 scripts（`3053a16`）**、**依赖瘦身与 torch 校准（`e29a24d`）**、**F3 回归测试补提交（`5477622`）+ F8 hexbroker/tests 残余（`6c1c9ce`）**——审计 31 个问题全部有提交可溯源，已知问题与代码待办全部清零；
2. 无待办。如需继续，可转入其它研究/开发任务。
