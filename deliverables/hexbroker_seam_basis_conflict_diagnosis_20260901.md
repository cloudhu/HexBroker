# SEAM_BASIS_CONFLICT 换月日健壮性专项排查（2026-09-01）

> 立项动因：2026-09-01 夜盘前刷新，tqsdk 主路 `refresh_pull_local.py --include-today`
> **0/18 落盘、退出码 3**，18 品种全报 `SEAM_BASIS_CONFLICT: 接缝日 2026-09-01`。
> 本次排查定位根因、验证修复，并揭示一个**必须成对实施**的依赖陷阱。

---

## TL;DR

| 项 | 结论 |
|---|---|
| 现象 | 换月接缝日 tqsdk 主路 18/18 系统性 `SEAM_BASIS_CONFLICT` |
| 根因 | 主湖已含 0901（**新主力**），tqsdk `KQ.m` 领先窗口内仍报**旧主力**→ 接缝日名义价天然错位；`seam_date=min(lake_last,tqs_last)=0901 ∉ tail_dates` → 硬拒 |
| 性质 | **非代码 bug，是"过度严格的护栏"**：把"合法换月领先窗口"误判为"基准不一致" |
| 当前是否可用 | 可用——靠 pandadata 全量兜底（但**每次换月日都强制依赖 pandadata**，架空了 tqsdk 主路存在的意义）|
| 修复 | Part A（放宽接缝）+ **Part B（编排层按 seam 状态定向补 pandadata）必须成对**，否则回归 |

---

## 一、证据链

### 1.1 主湖末交易日 = 2026-09-01（全部 18 品种）
实测 `data/raw/processed/<sym0>/1d/2026.parquet` 末 `datetime`：

| 品种 | last | raw_close | close | k |
|---|---|---|---|---|
| ag0 | 2026-09-01 | 16245 | 11110.12 | 0.683910 |
| rb0 | 2026-09-01 | 3174 | 3748.67 | 1.181055 |
| cu0 | 2026-09-01 | 109220 | 161191.48 | 1.475842 |
| au0 | 2026-09-01 | 959.94 | 736.31 | 0.767038 |
| hc0 | 2026-09-01 | 3388 | 7427.89 | 2.192413 |
| ta0 | 2026-09-01 | 5930 | 6102.35 | 1.029064 |
| jm0 | 2026-09-01 | 1694 | 2463.62 | 1.454322 |
| m0  | 2026-09-01 | 3377 | 10629.46 | 3.147604 |

→ `lake_last = 2026-09-01` 对全部品种成立，故 `seam_date = min(0901, 0901) = 0901`。

### 1.2 脚本自身文档证实"领先窗口"
`scripts/refresh_pull_local.py` L18-21 明确记载：
> 湖/pandadata 0817 已切 jm2701，**tqsdk KQ.m 0819 才切**（官方规则偏保守，
> KQ.m = 当日主力合约原始价、硬拼接不复权）。

即 **tqsdk 比主湖/pandadata 晚切主力合约**。0901 换月日，主湖已是新主力、tqsdk 仍报旧主力 → 名义价天然不对齐。

### 1.3 离线忠实复现（不依赖 tqsdk 网络）
`artifacts/_tmp/seam_repro.py` 直接复刻 L226-274 算法 + 引用脚本真实常量
（`RAW_ALIGN_TOL=0.005 / K_TOL=5e-4 / MIN_OVERLAP=3`），mock 现场
（`lake.0901=新主力 raw=17000`，`tqsdk.0901=旧主力=16245`）：

```
=== 现场复刻：ag0 换月日 0901（主湖已含新主力，tqsdk 仍报旧主力）===
lake_last=2026-09-01  tqs_last=2026-09-01
tqsdk.close(0901)=16245  lake.raw_close(0901)=17000

--- [原版] 结果 ---
  ⛔ 拒绝：SEAM_BASIS_CONFLICT: 接缝日 2026-09-01 与 tqsdk 名义价不对齐
        （主连换月时点分歧窗口）→ 拒绝扩展，回退 pandadata

--- [修复版] 结果 ---
  ✅ k=0.683910 flag=ROLLOVER_LEAD_WINDOW(soft-pass)
     tail=2026-08-18~2026-08-31 ext=0 (发射区间不含 0901，由 pandadata 补新主力 bar)
```

**原版精确复现误拒；修复版软放行且正确排除 0901（不发射错主力价）。**

---

## 二、根因剖析

`refresh_pull_local.py` L265-274 的接缝校验逻辑：

```python
seam_date = min(lake_last, tqs_last)
if seam_date not in set(tail_dates):
    raise ValueError("SEAM_BASIS_CONFLICT: ...")
```

**设计意图（L29-30 注释）**：湖最新日必须落在对齐稳定段内，否则 tqsdk 与湖在接缝处基准不一致（= 换月领先窗口）→ 拒绝扩展、回退 pandadata。
这本来是一道**安全护栏**——当 tqsdk 滞后、无法用 k 锚定新主力 bar 时，宁可回退也不瞎编。

**健壮性缺口**：这道护栏是"钝器"。它把两种本质不同的情形混为一谈：

| 情形 | 特征 | 正确处置 |
|---|---|---|
| ① 湖滞后 + 接缝不对齐（真需要扩展但锚不定）| `ext` 非空、`seam ∉ tail_dates` | **硬拒 + 回退 pandadata** ✅ 正确 |
| ② 湖已含接缝日 + 接缝不对齐（换月领先窗口，无新 bar 要扩展）| `ext` 为空、`seam ∉ tail_dates` | 当前**硬拒 ❌** → 应软放行 |

夜晚 18/18 失败属**情形 ②**：主湖已通过原始 pandadata P步 ingestion 写好 0901（新主力），
tqsdk 仅因领先窗口无法"重新校验" 0901 → 被误判为基准冲突 → 全拒。

**后果**：每次换月接缝日，tqsdk 主路形同虚设，必须全量回退 pandadata。
而这恰恰架空了本脚本的立项初衷——`refresh_pull_local.py` 正是为**消除 pandadata 配额单点故障**
（2026-08-28 因 pandadata 500009「单日总流量超限」停摆）而建。换月日反而最依赖 pandadata，
与立项目标南辕北辙。

---

## 三、修复设计（Part A + Part B 必须成对）

### Part A — `refresh_pull_local.py` 接缝校验放宽 + 显式状态

**核心改动**：仅当"存在真实扩展区（`ext` 非空）且接缝不对齐"时才硬拒；
`ext` 为空时软放行，并打 `ROLLOVER_LEAD_WINDOW` 标记。

L265-277 替换为：

```python
            # 接缝校验 + 换月领先窗口识别
            lake_last = lk.index.max()
            tqs_last = tqs.index.max()
            seam_date = min(lake_last, tqs_last)
            ext = tqs[tqs.index > lake_last]          # 扩展区（仅湖滞后时有）
            seam_in_tail = seam_date in set(tail_dates)
            if not seam_in_tail:
                if not ext.empty:
                    # 湖滞后且接缝不对齐 → 无法用 k 锚定新 bar → 硬拒，回退 pandadata
                    raise ValueError(
                        f"SEAM_BASIS_CONFLICT: 接缝日 {seam_date.date()} 与 tqsdk 名义价"
                        f"不对齐且存在扩展区→拒绝扩展，回退 pandadata")
                # ext 为空：湖已含接缝日，tqsdk 仅幂等重发重叠段；
                # 接缝错位系换月领先窗口（tqsdk 晚切主力）所致，无害 → 软放行。
                # 但接缝日新主力 bar 必须由 pandadata 补（见 _SEAM_STATUS.json）。
                seam_status = "ROLLOVER_LEAD_WINDOW"
            else:
                seam_status = "OK"

            # 扩展区换月疑点守卫
            ext_source, ext_rows = _augment_ext_with_collector(sym0, start, end, ext, lake_last)
```

并在 `main()` 末尾、写 `_LOCAL_REPORT.md` 之后，输出机器可读状态
（供编排层消费，避免"全量回退"一刀切）：

```python
            seam_status_map[sym0] = seam_status   # 循环内收集
            ...
(out_dir / "_SEAM_STATUS.json").write_text(
    json.dumps(seam_status_map, ensure_ascii=False), encoding="utf-8")
```

> 发射逻辑无需改：`emit_dates = tail_dates ∪ ext_rows.index`，
> 接缝 0901 既不在 `tail_dates`（未对齐）也不在 `ext`（空）→ 天然不发射，
> 不会把旧主力价错写进缓存。复现已验证。

### Part B — 编排层按 seam 状态**定向**补 pandadata（关键，不可省）

当前自动化步骤 2b 的触发条件是"`refresh_pull_local` 退出码 ≠ 0 → **全 18 品种**回退 pandadata"。
若只做 Part A：换月日 tqsdk 退出码变 0 → 步骤 2b 不触发 → **缓存缺 0901 新主力 bar → `fd=1` → 夜盘用过期主源**。这是**回归**，必须避免。

因此编排层需改为：

1. 读 `artifacts/p6_4_pull_<DAY>/_SEAM_STATUS.json`；
2. 若存在 `"ROLLOVER_LEAD_WINDOW"` 品种 → **仅对这些品种**的接缝日（0901）走 pandadata `get_future_daily_post` 定向补数，
   与 tqsdk 的重叠段 JSON 合并后交给 `p6_4_apply_persisted_dir.py`；
3. 仅当 tqsdk 真退出码=2/3（tqsdk 整体挂或真扩展失败）才走**全量** pandadata 兜底。

→ pandadata 依赖从"每次换月日全量 18 品种"降为"仅换月日的新主力 bar（少数品种）"，
真正落实"tqsdk 主路 + pandadata 仅补缺"的韧性目标。

---

## 四、风险与边界

- **⛔ 严禁只上 Part A**：见上，单独放宽接缝会让缓存缺 0901，制造比现在更隐蔽的回归。
- Part A 仅改变"情形 ②（ext 空）"的处置；情形 ①（ext 非空且不对齐）仍硬拒，安全护栏不削弱。
- 复现脚本 `artifacts/_tmp/seam_repro.py` 可作为该修复的**回归测试种子**（建议固化进 `tests/`）。
- tqsdk 领先窗口每换月日都会发生（脚本文档已实证），属**周期性、可预期**失效，适合用"源拆分"而非"全量兜底"根治。

---

## 五、建议与待主理人终裁

| 优先级 | 动作 | 说明 |
|---|---|---|
| P1 | 采纳 Part A + Part B 成对方案 | 根治换月日 tqsdk 系统性失效，落实 tqsdk 主路韧性 |
| P1 | Part A 改动加 `tests/` 回归用例（复用 `seam_repro` 思路）| 防止护栏被再次改回钝器 |
| P2 | 编排层自动化步骤 2b 改为"读 `_SEAM_STATUS.json` 定向补数" | 消除换月日全量 pandadata 依赖 |
| P2 | 长期：评估让 tqsdk 显式解析目标主力合约（消领先窗口）| 若可行可彻底去掉换月日对 pandadata 的依赖 |

**请主理人（齐活林 / 胡总）裁决：是否按"Part A + Part B 成对"实施？**
确认后按 SOP 走 Eng 实现 → QA fresh-eyes 复核 → 双 commit（feat+docs）入库。
