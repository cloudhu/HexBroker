# HexBroker 数据源代码审计报告 —— QA 独立复核

**复核人**：严过关（QA 工程师）
**复核日期**：2026-08-30
**复核对象**：`deliverables/data_source_code_audit_20260830.md`
**复核方法**：不采信原报告任何结论，全部独立重跑取证（全湖扫描 + 代码逐行比对 + 场景实跑复现）
**取证环境**：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`（pandas 3.0.3 / pyarrow 24.0.0）
**安全约束遵守**：全部取证只读；P1-2 最小复现在 `tempfile.mkdtemp()` 临时目录进行，未触碰 `data/` 下任何生产数据；未执行 git gc/repack/prune

---

## 一、复核结论总表

| 缺陷ID | 报告结论摘要 | 我的独立验证结果 | 判定 | 关键证据（命令+数值） |
|---|---|---|---|---|
| **P0-1** | ag0/au0/m0 三品种 2024 年尾部截断，缺失约 112 交易日；diff 扫描检不出此形态 | 全湖 18 品种 × 9 年度扫描：ag0=131、au0=131、m0=130，区间均止于 **2024-07-17**；其余 15 品种均为 242 行、止于 2024-12-31。以 15 个正常品种 2024-07-18~12-31 交易日并集为基准（112 天），三品种**各缺 112 天**。年内空档扫描 >5 天共 443 处（6~11 天，全为节假日），**>10 天恰 72 处全为 11 天春节/国庆，无一处 >11 天**；跨年边界空档 >5 天**恰 3 处，均 169 天** | **成立**（1 处小修正） | `01_lake_scan.py` / `03_gaps_amount_rawclose.py` / `10_missing_days_and_sideeffect.py`；缺失天数实测 112/112/**112**（报告称 m0 约 113，实为 112） |
| **P0-2** | `boundary_fake_gaps` 默认阈值 10% 对长时间静默缺失失效，三个跳变 6.27%~9.24% 全部漏检 | 三个跳变值**精确复现**：ag0 −6.69%、au0 +6.27%、m0 −9.24%，均 <10% → 现行阈值确实全部漏检。四个对照值也精确复现（−0.09%/−0.66%/+0.39%/+1.00%）。**但报告"信号极强、高一个数量级"是选择性取样**：全样本 144 个边界中，正常边界 \|jump\| 的 p90=2.43%、p95=3.17%、p99=5.61%、**max=6.87%（ag0 2025→2026，5 天正常跨年）**，另有 sc0 2020→2021 = +6.10%（4 天正常跨年）。ROC 证明**不存在任何 \|jump\| 阈值能做到 TP=3 且 FP=0**（最佳 6.2% → TP=3/FP=1，精确率 75%）；缺失边界最小值 6.27% < 正常边界最大值 6.87%，跳变域**不可分**。报告建议的日历跨度方案经我独立验证**完美**：正常跨度 2~5 天 vs 缺失 169 天，阈值取 (5,169) 内任意值均 TP=3/FP=0 | **成立（核心结论+处置方案）<br>但理由需修正** | `02_boundary_jumps.py` / `09_p02_threshold_roc.py`；正常边界 max \|jump\| = 6.87% > 缺失边界 min 6.27% |
| **P1-1** | 数据湖 root 约定分裂；`failover._anchor(root="data")` 返回 None → NoAnchor | 4 个源默认 `root="data/raw"`（sina:95、akshare:65、pytdx:102、czce:84）；`DataLake` 默认 `root="data"`（store.py:46）；`scripts/fetch_data.py:57 = "data"`。**实跑端到端**：`fetch_adjusted(root="data")` 在主源 Quota 失败后得 `primary/pandadata:Quota; backup/backup:NoAnchor`，`tier_hit=''`、`ok=False` —— 危险链路真实存在。`DataLake('data').load_processed` 抛 FileNotFoundError，`DataLake('data/raw')` 返回 2098 行 | **成立**（调用点清单需补全） | `04_exec_p1_1_4_5.py`；报告调用点表漏列 `failover.py:215`、`rebuild.py:314`、`scripts/p11_truth_rebuild.py:177` |
| **P1-2** | `save_processed` 按年整区覆盖写（不 merge），窄窗口 + save=True 截断整年 | **实跑最小复现成功**（临时目录）：全年 261 行 → 窄窗口 [2026-08-24, 2026-08-28] → 落盘回读**仅剩 5 行，丢失 256 行**。复现用的是生产代码 `base._clip_range` + `store.save_processed`。源码确认无 merge/concat，仅 `write_parquet` 整区覆盖；四源 `save` 默认均为 True | **成立** | `05_exec_p1_2_truncation.py`；261 → 5 行，丢 256 行 |
| **P1-3** | Tier3 缺 try/except 与 mark_provisional；Tier2 两者都有 | **实跑双向验证**：Tier3 graft 抛错 → HexDataError **冒泡出 `fetch_adjusted`**，整批 Outcome 丢失；Tier2 同场景**被捕获**，正常返回并留痕 `backup/backup:Graft`。mark_provisional 调用：Tier2 = 2 次（每品种 1 次），Tier3 = **0 次**。**但** Tier3 的 `o.provisional = True`（failover.py:354）**是设置了的** —— 缺的是**持久化 sidecar（mark_provisional）**，内存标志已置位 | **成立<br>但表述需修正** | `06_exec_p1_3.py`；Tier2 mark_provisional=[('processed','rb0',2026,8), ('processed','ag0',2026,8)]，Tier3=[]；两者 `Outcome.provisional` 均 True |
| **P1-4** | freshness 豁免连带跳过覆盖性检查，临界点 08-25 拦截 / 08-24 放行 | **实跑精确复现**：源停更 2026-06-01、today=2026-08-30、max_stale_days=5 → `end=2026-08-25` **拦截** HexStaleDataError；`end=2026-08-24` **放行**并返回 latest=2026-06-01。08-26/08-28/08-30 均拦截，08-23/08-20 均放行。**临界点与报告完全一致** | **成立** | `04_exec_p1_1_4_5.py`；判据 ref−5d = 2026-08-25 |
| **P1-5** | 交叉校验后 `break`，第 3 个源 czce 永不执行 | **实跑注入探针**：正常路径实际调用顺序 `['sina','akshare']`，**czce 未执行**；对照实验（sina 失败）→ `['sina','akshare','czce']`，czce 执行。证明 czce 仅在前两源之一失败时才可达 | **成立** | `04_exec_p1_1_4_5.py` + `backup.py:242-243` |
| **P1-6** | sina / pytdx 用 `df2, _ =` 丢弃告警，仅 akshare 正确 logger.warning | sina:220 `df2, _ =` ❌；pytdx:296 `df2, _ =` ❌；akshare:118-120 `out, repairs =` + `logger.warning` ✅。**报告漏了第 4 处**：`czce_source.py:260` 同样是 `df, _ = repair_envelope(...)` ❌ | **成立<br>但不完整** | grep `repair_envelope` 全仓 4 处调用点，报告只列 3 处 |
| **P1-7** | pytdx `_fetch_continuous` 两次 `_connect()`，每品种泄漏 1 个连接 | **实跑（mock 走通完整链路）**：单品种新建 api 对象 **2** 个、connect **2** 次；`fetch_bars` 完整路径 disconnect **1** 次 → **泄漏 1 个/品种，18 品种 = 18 个**，与报告数字完全一致。**团队特别问的"`_connect` 是否复用"**：`a1 is a2` → **False**，即**从不复用，每次新建**，较早对象被 `self._api` 覆盖且 disconnect 从未被调用 | **成立** | `08_exec_p1_7_leak.py`；2 created / 2 connected / 1 disconnected / 1 leaked |
| **P1-8** | pytdx 默认 `count=1200`（日线），只取最近 N 根，历史回填必然失败 | 代码确认 `pytdx_source.py:360-361`：`count = 1200 if freq == "1d" else 2000`；`fetch_bars` 不按 start/end 请求，先取最近 N 根再交给 `_clip_range` 裁剪 | **成立**（代码确认，**未实跑网络**） | `pytdx_source.py:355-361, 384`；需真实 pytdx 服务器才能端到端，本次未实跑 |
| **P2-1** | `health_check` 只 `try import`，不探测连通性 | base:44-46、sina:294-300、akshare:178-184、czce:282-288、pytdx:394-400 —— 五个实现全部为 `try: import xxx; return True / except ImportError: return False` | **成立** | 代码比对，5/5 命中 |
| **P2-2** | `amount` 列 18/18 品种 100% 为零；"feature/factor 层未引用，**暂无下游影响**" | 全湖 **37385/37385 = 100.00%** 为零，18/18 品种；`amount` 确在 `REQUIRED_COLS`（constants.py:84）。**但"暂无下游影响"是错的**：`hexbroker/forecast/kronos_predictor.py:188` 把 `["open","high","low","close","volume","amount"]` 直接喂进 `predictor.predict_batch()`（Kronos 基础模型），而 `config.py:100` 默认 `model_name="NeoQuasar/Kronos-small"`、`configs/forecast/kronos_small.yaml` 已配置 → **一个恒零通道正在输入生产预测模型**。（feature/ 与 factor/ 目录确无 amount 引用，报告这部分对） | **需修正<br>（应升级 P1）** | `03_gaps_amount_rawclose.py`（37385/37385）；grep `kronos_predictor.py:188`、`configs/forecast/kronos_small.yaml` |
| **P2-3** | akshare 用 `set` 求交决定列顺序 | `akshare_source.py:109`：`out = renamed[list(set(AK_COLUMN_MAP.values()) & set(renamed.columns))].copy()` —— set 无序，列序不确定 | **成立** | 代码确认 |
| **P2-4** | czce `sort_values` 未指定稳定排序，与"并列取第一个，保证确定性"注释矛盾 | 代码确认 `czce_source.py:167-169` 无 `kind` 参数 → 默认 quicksort（非稳定），注释却称"保证确定性"。**但我未能实证出差错**：构造 12 行并列 OI 表，quicksort 与 `kind='stable'` 结果一致；30 种不同输入行序下，两者选出的主力集合**相同**。代码与注释确实不符，但**实践严重度低于报告暗示** | **成立（代码层）<br>严重度需下调** | `07_exec_p2_4_p1_7.py`；quicksort=stable=CF600，30 种行序下两者均得 {CF600,CF601,CF602} |
| **P2-5** | czce 逐日 HTTP 请求，无缓存 | `czce_source.py:220`：`for day in pd.bdate_range(start, end): table = self._fetch_day(day)` | **成立** | 代码确认 |
| **P2-6** | caliber.py 注释称"raw_close 恒等于 adj_close"与实测不符（rb0 4047 vs 5451.02） | **报告引用的具体数值精确吻合**：rb0 `2024-01-02` 实测 `raw_close=4047.0`、`adj_close=5451.019017`。18/18 品种 2024 年 `close == adj_close` **100%**（0 处不等）→ 报告"close ≡ adj_close、是后复权值；raw_close 是名义价"**成立**。15/18 品种 raw_close 与 adj_close 100% 不等 → 注释确已过时 | **成立** | `02_boundary_jumps.py`（口径核验段）、`03_gaps_amount_rawclose.py` |
| **P2-7** | 交叉校验告警黑洞，`RawPull.warnings` 被丢弃 | `backup.py:242` 生成 warns 并在 252-253 extend 到 `RawPull.warnings`；但 `failover.py:230` 调用 `fetch_close_series()` 只返回 `{s: p.close}` → RawPull（含 warnings）被丢弃。报告所标行号 251 略有偏差，丢弃实际发生在 230 的调用处 | **成立（行号需修正）** | `backup.py:256-263`、`failover.py:230` |
| **P2-8** | 测试全绿但无真实湖完整性基线 | 180 项测试，grep 无任何"覆盖度/日历跨度/首尾"断言。**实跑基线：collected 180，exit 0，1 skipped → 179 passed / 1 skipped**（报告写"177 passed / 1 skipped"，与 180 总数不符） | **成立**（报告自身计数有小误） | `pytest hexbroker/data --collect-only` 分文件计数：24+5+22+20+19+12+14+17+25+22 = 180 |
| **P2-9** | czce 主力规则与 sina/pytdx 完全不同却共用 symbol key | czce = 逐日 OI 最大（`czce_source.py:167-170`）；pytdx = 当前主力全部历史（docstring `274-275` 明写"仅取当前主力近期历史"）；三者均输出 `cf0` 形式 | **成立** | 代码比对 |

---

## 二、误报清单

**严格意义上的误报：无。**

我逐条审视了报告的每个判定，未发现把**正常现象**错判为缺陷的情况。特别核查了团队点名的两个可疑点，报告均处理正确：

1. ✅ **`sc0` 2018 年仅 189 行**：实测 `sc0` 2018 首日 = **2018-03-26**（原油期货上市日），189 行属上市时间晚的正常现象。报告**未**将其列为缺陷，处理正确。
2. ✅ **"年内空档"**：报告正确识别为春节/国庆假期，未误判。我实测 443 处 >5 天空档全部落在 6~11 天的节假日区间，无一例外。

另有两处我一度怀疑、经排查后**判定为非缺陷**，一并说明以示排查完整性：

3. ✅ **`caliber.py:69-71` 的 `if b - a != 1: continue`（跳过非相邻年）** —— 初看像漏检，实为有意设计且已写进 docstring（"缺失年跨界拼接的幅度是多年累积，不判罪"），报告未误判。
4. ✅ **ag0/au0/m0 的 `k = adj_close/raw_close ≈ 1`**（2018-2024 年 k==1 占比 82%~100%，而其余 15 品种 ~0%）—— 我一度怀疑这是未复权缺陷，但 `caliber.py:102` 已明确预见："k≈1 也可能是真值特征（滚动价差极小的品种，**如 ag/au/m**）"。实测这三品种 k 值域 [1.00,1.02]，**确属真值特征，不构成缺陷**。（仅建议在 P0-1 回填时顺带确认 2018-2024 与 2025-2026 两段的口径衔接。）

**但需标注 2 处"严重度需要调整"：**

- **P2-4 严重度被高估**：代码层缺陷成立，但我用 30 组输入行序实证未能复现任何分歧，实际风险低于报告暗示。
- **P2-2 影响面结论错误（方向相反，是低估而非高估）**：见下节 L1。

---

## 三、漏报清单

### 🔴 L1（P1 级，建议升级立项）：`amount` 恒零正在污染生产预测模型的输入通道

**证据**：
- `hexbroker/forecast/kronos_predictor.py:188`
  ```python
  ctx = sdf.iloc[start : t + 1][["open", "high", "low", "close", "volume", "amount"]]
  ```
  该 `ctx` 在紧随其后（第 209 行）被送入 `predictor.predict_batch(...)` —— Kronos 基础模型。
- `hexbroker/config.py:100` 默认 `model_name: str = "NeoQuasar/Kronos-small"`，`config.py:335-341` 对 `kronos`/`kronos_small`/`kronos_mini` 做启用校验；`configs/forecast/kronos_small.yaml`、`kronos_mini.yaml` 已配置 → **Kronos 是已接线、默认启用的预测后端，不是死代码**。
- 而 `amount` 全湖 **37385/37385 = 100.00%** 为零。

**与报告的差异**：报告 P2-2 称"feature/factor 层未引用，**暂无下游影响**"。feature/factor 层确实无引用（这部分对），但 forecast 层有，且是模型直投。**结论方向错误，建议将该条从 P2 升为 P1**。

**建议处置**：要么在 Kronos 输入通道中剔除 `amount`，要么走 czce 源把真实成交额填上；二选一，不能维持现状。

---

### 🟡 L2（P2）：`DataLake.__init__` 在**只读路径**上创建目录（副作用）

**证据**：`store.py:49-50`
```python
for layer in ("raw", "interim", "processed"):
    (self.root / layer).mkdir(parents=True, exist_ok=True)
```
**实跑**：构造 `DataLake("<tmp>/never_should_exist")`（未做任何写操作）后，该目录**被创建**，并自动长出 `['interim','processed','raw']` 三个空子目录。

**危害**：`failover._anchor()` 每品种都 `new DataLake(self.root)`（failover.py:215）→ 每次 mkdir ×3。更关键的是：**root 拼错时会静默造出一个空目录而不是报错**，读空湖后仍走 FileNotFoundError → NoAnchor。这与 P1-1 叠加会放大误判 —— 运维看到"目录存在"反而更难察觉配错。

**建议**：读路径不应有写副作用。构造时不建目录，或仅在显式 save 时创建；至少在目录为空时告警（报告 P1-1 的处置建议里已提到"构造时校验非空"，可合并处理）。

---

### 🟡 L3（P2）：审计覆盖面自我声明与实际不符（影响报告可信度）

**证据**：
- 报告第六节"本次审计覆盖文件"列出 9 个模块，实测共 **1905 行**；而 `hexbroker/data/` 有 **18 个非测试模块 / 3353 行**。
- 未列入覆盖清单的 9 个模块中，包含 **`rebuild.py`（406 行，data/ 下最大模块）** 与 **`validate_sources.py`（364 行）**。
- `rebuild.py` 尤其关键：它是 `failover.mark_provisional` 的实现所在（P1-3 的处置对象），也是报告 P0-1 建议复用的重建入口（`scripts/p11_truth_rebuild.py` 依赖它）。
- 报告称代码规模"7377 行（data 5108 + sources 2269）"；我实测 **`hexbroker/data/**` 非测试 4851 行、含测试 7156 行**，两个口径都对不上 7377。

**说明**：我已补看了 `rebuild.py`，其 `rebuild_partition`（239-339）**是 merge 语义、是安全的**（逐行替换 + 追加，不做整区覆盖）—— 这反过来印证 P1-2 的破坏范围**仅限"数据源直连 save_processed"路径**，不含 rebuild 路径。这点报告没说清楚，建议补充。

---

### 🟡 L4（P2，信息项）：建议 P0-1 回填时一并固定复权口径

见第二节第 4 点：ag0/au0/m0 在 2018-2024 有 82%~100% 的行 `raw_close == adj_close`（k 值域 [1.00,1.02]），2025-2026 则无一行 k 恰为 1（值域 [0.97,1.05]）。`caliber.py:102` 已预见此为 ag/au/m 的真值特征，**判定非缺陷**；但两段数据的处理痕迹不同，回填 2024 下半年时建议核对口径衔接，避免又造出新的边界不一致。

---

## 四、总体结论

### 结论：**这份审计报告可信，可以作为后续修复立项的依据 —— 但需带 3 处修正、1 处升级。**

**可信的理由（我独立跑出来的支撑）：**

1. **核心事实零误差**。P0-1 的品种、行数（131/131/130）、截断日（2024-07-17）、缺失天数（112）；P0-2 的三个跳变值（−6.69%/+6.27%/−9.24%）与四个对照值（−0.09%/−0.66%/+0.39%/+1.00%）；P1-4 的临界点（08-25 拦 / 08-24 放）；P1-7 的泄漏量（1 个/品种、18 品种 18 个）—— 我逐条重跑，**全部精确复现，无一偏差**。
2. **最需要实跑的三条，我全部实跑成功且结论成立**：P1-2 的截断复现（261→5 行，这是我本次认为最需要实跑验证的一条，报告判对了）、P1-1 的端到端 NoAnchor 链路、P1-3 的 Tier2/Tier3 双向对照。这三条不是"读代码觉得对"，是真跑出来的。
3. **误报为零**。我刻意去找了报告有没有把正常现象当缺陷（sc0 上市晚、年内节假日空档、caliber 跳过非相邻年、ag/au/m 的 k≈1），**一条都没找到**。报告对"做得好的部分"的判断我也验证属实（validate_bars 拒 0 行、重复索引按 (symbol,datetime)、graft 重叠区校验位置、换月近似主动放弃）。
4. **处置方案经我独立验证是最优解**。P0-2 建议的日历跨度检测：正常跨度 2~5 天 vs 缺失 169 天，**任意阈值在 (5,169) 区间内都完美分离**，确实零歧义零误报。

**必须修正的 4 处：**

| # | 条目 | 修正要求 |
|---|---|---|
| 1 | **P2-2** | 从 P2 **升为 P1**：`amount` 恒零不是"暂无下游影响"，`kronos_predictor.py:188` 正把它喂进默认启用的 Kronos 模型 |
| 2 | **P0-2 理由** | 删除"信号极强、高一个数量级"的表述。实测正常边界 max \|jump\|=6.87% 已超过缺失边界 min 6.27%，**跳变域不可分**；正确说法是"跳变信号本身有噪声，只有日历跨度信号是干净的" |
| 3 | **P1-3 表述** | 改为"Tier3 缺 **mark_provisional 持久化 sidecar**"，而非"未挂 provisional"—— `o.provisional=True`（failover.py:354）是设置了的 |
| 4 | **P1-6 完整性** | 补上第 4 处 `czce_source.py:260`（同样丢弃告警） |

**另需补正的小项**：P1-1 调用点清单漏 3 处；P0-1 缺失天数 m0 应为 112（非 113）；P2-7 行号 251 → 230；P2-8 测试计数应为 179 passed / 1 skipped（非 177/1）；报告代码规模 7377 行应改为 4851（非测试）/ 7156（含测试）；P2-4 严重度下调（未实证出差错）。

**给主理人的立项建议（按我复核后的优先级重排）：**

1. **P0-1 + P0-2** —— 数据已受损，立即立项。回填时按 L4 固定口径；门禁用日历跨度方案（已验证完美分离）。
2. **L1（原 P2-2，升级）** —— 恒零 `amount` 正在输入 Kronos 模型，建议与 P0 同批或紧随其后处理。
3. **P1-1 + P1-2** —— 架构性引信，优先于所有新功能。P1-2 复现已坐实（261→5 行），且四源 `save` 默认均为 True。
4. **P1-3 / P1-4 / P1-5 / P1-6 / P1-7 / P1-8** —— 按序排期，均成立。
5. **L2 / L3** —— 治理项，随 P1-1 一并处理（L2 可直接并入 P1-1 的处置）。

**一句话总评**：这是一份**事实扎实、结论可用**的审计报告 —— 核心判定我一条都没推翻，最硬的三条实跑复现全部成功，且零误报。它的问题不在"判错了"，而在**两处表述夸大（P0-2 的信号强度）、一处影响面看漏（P2-2 的 Kronos 通道）、以及覆盖清单与实际不符**。带上述修正即可作为立项依据，不必重做审计。

---

## 五、取证脚本清单

全部落盘于 `C:\Users\Administrator\AppData\Local\Temp\qa_forensics\`（临时目录，未写入项目）：

| 脚本 | 覆盖条目 | 性质 |
|---|---|---|
| `01_lake_scan.py` | P0-1 | 全湖 18×9 扫描（只读） |
| `02_boundary_jumps.py` | P0-2 / P2-6 | 144 个年度边界跳变全分布 + 口径核验（只读） |
| `03_gaps_amount_rawclose.py` | P0-1 / P2-2 / P2-6 | 年内+跨年空档、amount、raw_close 年度分布（只读） |
| `04_exec_p1_1_4_5.py` | P1-1 / P1-4 / P1-5 | 实跑复现（注入探针） |
| `05_exec_p1_2_truncation.py` | P1-2 | **最小复现**（tempfile 临时湖，未触碰 data/） |
| `06_exec_p1_3.py` | P1-3 | Tier2/Tier3 双向对照（tempfile 临时湖） |
| `07_exec_p2_4_p1_7.py` | P2-4 / P1-7 | 排序稳定性实证 + 连接统计 |
| `08_exec_p1_7_leak.py` | P1-7 | 走通完整链路的连接泄漏计数（mock，无网络） |
| `09_p02_threshold_roc.py` | P0-2 | 阈值 ROC 分析（只读） |
| `10_missing_days_and_sideeffect.py` | P0-1 / L2 | 缺失交易日数 + DataLake 构造副作用 |
| `11_adjfactor_anomaly.py` | L4（已判非缺陷） | 复权因子 k 年度分布 |

**本报告**：`deliverables/qa_audit_review_20260830.md`
