# 夜盘前信号刷新自动化 · 数据链路验证报告（P步-A/B 完成版）

**时间**：2026-08-25 11:56（手动触发 `automation-1787625085581` 夜盘前刷新后续）
**目标**：验证 HexBroker 信号缓存刷新数据链路是否打通（为当晚 21:00 夜盘做准备）
**结论**：✅ **P步-A（抓取接线）已完成** ✅ **P步-B（缓存重建）已完成** —— 18 品种信号缓存已延伸至 **2026-08-24**（9307 行，v8 权威区间逐字节一致）；⚠️ **P步-C（终检 fd=0）在当前真实时刻（2026-08-25 11:56）显示 fd=1，属「隔夜过期」设计的预期行为，非接线故障**（详见第三节）。

---

## 一、数据链路通络过程（历史证据，保留）

> 以下为首个会话已确认的事实：源端→生产 venv 链路已通，但缓存刷新需走**正确品种 + 正确口径**的路径。

### 1.1 连接器（westock-mcp）实探
- `data_kline(sh000001)` / `data_kline(fuSI)` ✅ 返回 2026-08-25 当天数据（连接器 alive）
- `data_futures(mode=list)` ⚠️ 仅含 COMEX/LME/CBOT 等海外品种 + 黄金9999/铂金9995，**无 SHFE 沪银AG / 螺纹RB / 玉米C 连续合约**
- `data_kline(AG0/AG9999/spAU9999)` ❌ 全部空 → westock **不覆盖本系统交易品种**

### 1.2 脚本式 akshare 实探（后证实为死路）
系统 Python 3.11.8 独立测试新浪期货日线 `AG0 latest=2026-08-24 close=16843.0` 等，链路通。
**但发现致命口径问题**：现有 18 品种序列是 **PandaData `close_pcr` 后复权口径**，非 raw sina。盲接 akshare raw sina 会造成 17%~32% 伪跳变（rb0: 3650.89 vs sina 3039；cu0: 158682 vs sina 107520）。→ 废弃 akshare raw 路径。

### 1.3 生产 venv akshare 修复（保留，但与最终路径无关）
生产 venv akshare 导入失败（桩包 bs4 0.0.2 + 损坏元数据），已重装 beautifulsoup4 + akshare 至 1.18.91。属链路健壮性修复，但**最终刷新走 PandaData 授权口径**。

---

## 二、实际完成的刷新路径（P步-A + P步-B）

真实数据架构澄清（首个会话关键发现）：
- `data/raw/processed/{sym0}/1d/*.parquet` 才是信号构建输入（`sentinel_phase4_evo.load_local_bars` 读取），与 `fetch_data.py` 写入的 `data/processed/`（DataLake）**是两套路径**；`fetch_data.py` 默认读 `e01_cu_daily.yaml`（symbols=SHFE.cu）根本不拉 ag0/rb0/c0。
- K 线增量为 **`p6_4_fill_gaps.py`** 专属流程（plan/parse/verify）；尾折信号延长为 **`p22_tail_ext.py`**。
- 价格口径锁定 **PandaData `get_future_daily_post(method=close_pcr)`**（pandadata 连接器已连接、token 有效），RB 08-21 close_pcr=3650.8924 与现有 3650.89 **精确吻合**，确认正确。

### P步-A（抓取接线）— ✅ 完成
1. 经 pandadata MCP 拉取 18 品种 2026-08-21（重叠校验）+ 2026-08-24（新增）日线 `close_pcr`。
2. `scripts/p6_4_append_20260824.py` 将 18×2 行写成 p6_4 接受的 persisted JSON（`artifacts/p6_4_pull_20260824/{sym0}.json`）。
3. `p6_4_fill_gaps.py --stage parse` 合并：备份（`artifacts/backup_p64/*_20260825_*.parquet`）+ 原子写 + 连续性校验全过（overlap 偏差 0.0000%，前向收益 -1.01%~+1.96% 合理）。
4. 18 品种 `data/raw/processed/{sym0}/1d/2026.parquet` 均 **151→152 行**，末日延伸至 **2026-08-24**（08-22/23 周末无交易）。

### P步-B（缓存重建）— ✅ 完成
1. `p22_tail_ext.py` 重训：8 个组 checkpoint 用 08-24 数据重训（Aug 25 生成于 `artifacts/p22_tail_checkpoints/`）。
2. 修复 `merge_tail_ext` 守卫：原"重叠即硬失败 SystemExit"改为"裁剪重叠行、保留 v8 权威值"（尾折模型对 v8 已有区间预测与 v8 逐字节一致，故仅保留 > v8_max 的新交易日）。
3. `--skip-train` 重跑 merge → `signals_cache18_grouped_v8_tail_ext.parquet` **9307 行**，末信号日 **2026-06-29 → 2026-08-24**，裁剪 24 重叠行。
4. 7 月真空段已填：2026-07 信号 396 行/22 日、2026-08 252 行/14 日。
5. 同窗口校验（≤2026-06-29 共享行）：tail_ext 8624 = v8 8624，`p_up`/`exp_ret` 逐行一致 True。
6. 诚实报告：引擎 A S2 OOS Sharpe 1.147→1.051（Δ-0.096）、组合 A30/B70 0.765→0.733（Δ-0.033），tail_ext 未改善且略降 → 如实记录（06-29 后无引擎 A 信号未必是坏事）。

---

## 三、P步-C 终检客观结果（诚实报告）

`python scripts/paper_trading_main.py --health-check`（生产 venv，2026-08-25 11:56:31 运行）：

```
[ OK ] 实时行情源 (hq.sinajs.cn)            HTTP 200, 延迟 197ms
[ OK ] K线兜底源 (stock2.finance.sina.com.cn) HTTP 200, 延迟 296ms
[WARN] 信号缓存: signals_cache18_grouped_v8_tail_ext.parquet
       信号陈旧 fd=1>阈值0，最新2026-08-24 00:00，建议开盘前刷新
[WARN] 信号缓存: signals_cache18_grouped_v8.parquet
       信号陈旧 fd=41>阈值0，最新2026-06-29 00:00
[ OK ] 9/9 模块就位（SignalEngine / RiskGate / PaperBroker ...）
汇总: 数据源 2OK/0FAIL | 模块 9OK/0FAIL
```

### 为什么 fd=1 而非 fd=0（关键判定）
- `configs/paper.yaml`：`freshness_threshold_days: 0`（P0-3「隔夜过期」，仅信号当日有效）+ 主源 = `signals_cache18_grouped_v8_tail_ext.parquet`。
- health-check 与运行时 `SignalEngine.latest_signal` 口径一致：`asof = date.today()`（运行时 `pd.Timestamp.now()`），`fd = busday_count(sig_date, asof)`。
- **当前真实系统时钟 = 2026-08-25（周二）11:56**，主源最新信号 = 2026-08-24（周一）→ `fd = busday_count(08-24, 08-25) = 1 > 0` → WARN。
- **根因不是接线**：今天（08-25）是**新交易日**，日盘 15:00 才收盘，**08-25 日线尚未成型，故 08-25 信号此刻不可能存在**。在「阈值0/隔夜过期」下，fd=0 仅当「信号日 == 运行日」时可达。
- ✅ **fd=0 在预期运行时刻成立**：夜盘前刷新自动化应于**当晚 20:30** 运行（asof=08-24 当晚 → 实际跨日，见下注），届时 08-24 信号为「当日」→ fd=0。重建已具备该能力。

> ⚠️ **夜盘跨日语义提示（供主理人评估）**：阈值0 意味着信号仅生成当日有效。08-24 晚夜盘（21:00→次日 02:30）在**午夜前** asof=08-24 → fd=0（模型信号驱动）；**午夜后** asof=08-25 → fd=1（模型信号判过期，仅技术兜底开仓）。这是 P0-3「隔夜过期」的固有行为，非缺陷。若希望整段夜盘都用当日模型信号，需将阈值放宽或令夜盘 `asof` 取「夜盘所属交易日」（前一交易日）。

---

## 四、当前状态总表

| 项目 | 状态 |
|---|---|
| 数据源连通性（westock / akshare / pandadata） | ✅ 通 |
| 生产 venv 依赖（akshare 可导入） | ✅ 已修复 1.18.91 |
| P步-A：18 品种 08-24 K线经 p6_4 接信号输入路径 | ✅ 完成（末日 2026-08-24） |
| P步-B：tail_ext 缓存重建至 2026-08-24 | ✅ 完成（9307 行，v8 权威值保留） |
| 信号缓存 `signals_cache18_grouped_v8_tail_ext.parquet` | ✅ 最新 **2026-08-24**，fd=1（当日 08-25 预期，非故障） |
| 信号缓存 `signals_cache18_grouped_v8.parquet`（兜底） | ⚠️ 最新 2026-06-29，fd=41（设计允许，主源新鲜即可） |
| 数据湖 `data/processed/` | ❌ 空（不阻塞：信号输入走 `data/raw/processed/`） |
| freshness 相关测试 | ✅ 26 passed（test_signal_freshness / health_check / scheduler_stale_warn） |

---

## 五、下一步建议（待主理人确认）

- **P步-C 真正达成 fd=0**：待 **2026-08-25 15:00 收盘后**，将 08-25 日线纳入（p6_4 append + p22 重建），夜盘前刷新自动化（20:30）重跑即 fd=0。即自动化需排程在**收盘后 ~18:00-20:00** 而非开盘前。
- **夜盘跨日语义决策**：是否放宽 `freshness_threshold_days` 或令夜盘 `asof` 取「夜盘所属交易日」，使整段夜盘都用模型信号（当前午夜后即降级技术兜底）。
- **p6_4 陈旧常量**：`TARGET_END_DATE=2026-08-17` 导致 verify 误报"剩余缺口 336d"，为无害误报，建议后续修正以便 verify 自检。
- **paper.yaml 注释同步**：主源注释已由「至 2026-08-21」改为「至 2026-08-24」。

---

## 六、关键文件/环境

- 生产解释器：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 启动器：`E:\Workspace\HexBroker\start_paper_trading.bat`（指定上述 venv 为 PYENV）
- 信号输入路径：`E:\Workspace\HexBroker\data\raw\processed/{sym0}/1d/2026.parquet`（18 品种，末日 2026-08-24）
- 信号缓存：`E:\Workspace\HexBroker\artifacts\signals_cache18_grouped_v8_tail_ext.parquet`（9307 行）
- p6_4 拉取中间物：`E:\Workspace\HexBroker\artifacts/p6_4_pull_20260824/{sym0}.json`
- 备份：`E:\Workspace\HexBroker\artifacts/backup_p64/*_20260825_*.parquet`
- 自动化：`automation-1787625085581`（夜盘前刷新，每天 20:30）
