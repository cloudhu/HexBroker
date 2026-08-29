# 36 · 期货免费数据源选型调研报告（PRD）

> HexBroker 中国大宗商品期货 AI 量化交易系统
> 撰写：许清楚（产品经理） | 日期：2026-08-29 | 状态：待评审
> 铁律：**无证据不翻转**。凡未实测项一律标注「未实测」，凡推断项一律标注「\[推断\]」。

---

## 0. 摘要（TL;DR）

| 结论 | 状态 |
|---|---|
| 抽象层 5 个源里，**只有 AkShare 现在真能取到当日数据** | ✅ 实测 |
| 现有 `SinaSource` 用的新浪接口**已冻结在 2024-07-17**，且**失败时静默返回空**（不报错） | ⛔ 实测（严重） |
| 现有 `AkshareSource` **代码是坏的**（中文列名 rename 失效 → `KeyError: 'datetime'`） | ⛔ 实测 |
| `PytdxSource` 本环境**连不上任何公共服务器**（10/10 TCP 7727 超时） | ⛔ 实测 |
| 47 个单元测试**全部通过，但 100% 是 mock**，零联网、零覆盖主力连续与后复权 | 🔴 实测（假绿灯） |
| 抽象层与生产管线**有 5 处脱节**，生产刷新从未调用过抽象层 | ⛔ 实测 |
| pandadata `close_pcr` = **比例后复权**；所有免费源只给**不复权** | ✅ 实测（收益率 35/36 一致佐证） |
| **推荐组合**：主源 pandadata（口径权威）→ 备源 AkShare/新浪新接口 → 兜底 交易所官方日行情 | 建议 |
| **核心设计原则**：备源只供 raw，后复权链**自己续接**，绝不换口径 | 建议 |

**一句话结论**：抽象层不是"早就设计好但没接上"，而是**设计好了、但每个源都坏的/停更的、且测试用 mock 掩盖了这一切**。修复顺序必须是「先修源 → 再修口径 → 最后接线」，反过来做会把污染数据写进生产。

---

## 1. 现状体检（读代码 + 实跑）

### 1.0 环境与测试前提（影响所有结论的可信度）

| 项 | 值 |
|---|---|
| 生产解释器 | `C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe` → **Python 3.13.14** |
| 出网方式 | **沙箱 HTTP 代理 `127.0.0.1:52146`**（`HTTP_PROXY`/`HTTPS_PROXY` 已设） |
| 代理行为 | **有白名单**：部分主机间歇性 `ProxyError`（东财 push2his 先成功后持续失败） |
| 影响 | HTTP 源可测；**裸 TCP（pytdx 7727 / CTP）全部不可测** |

> ⚠️ 因此本报告中「不可达」一律标注为「**本环境不可达**」，不等于源本身已死。用户在本地无代理环境需复测。

### 1.1 五个源的体检结果

测试命令（可复现）：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd E:/Workspace/HexBroker && "$PY" -c "
from hexbroker.data.sources import PytdxSource, SinaSource, TqsdkSource, AkshareSource
from hexbroker.data.sources.qlib_source import QlibSource
for c in [PytdxSource,SinaSource,TqsdkSource,AkshareSource,QlibSource]:
    print(c.__name__, c().health_check())"
```

| 源 | 依赖安装 | `health_check()` | 网络实取 | 期货 | 主力连续 | 后复权 | 18 品种 |
|---|---|---|---|---|---|---|---|
| `pytdx_source.py` | ✅ pytdx 已装 | ✅ `True` | ⛔ **10/10 服务器 TCP:7727 全超时** | 设计支持 | 🟡 仅"当前主力近期"，**不跨月拼接** | ❌ `adj_close=close` | 未实测（网络不通） |
| `sina_source.py` | ✅ requests 已装 | ✅ `True` | 🟡 HTTP 200 但**数据止于 2024-07-17** | ✅ | ✅ 名义 `cu0/rb0` | ❌ `adj_close=close` | 18/18（**全部过期 2 年**） |
| `tqsdk_source.py` | ⛔ **未安装** | ⛔ `False` | 未实测 | 设计支持 | 设计支持 | ❌ | 未实测 |
| `akshare_source.py` | ✅ **1.18.91** | ✅ `True` | ✅ **数据到 2026-08-28** | ✅ | ✅ 名义主力连续 | ❌ `adj_close=close` | ✅ **18/18 覆盖** |
| `qlib_source.py` | ⛔ **未安装** | ⛔ `False` | 未实测 | ⛔ Qlib 以股票为主 | — | — | — |
| `csv_source.py`（默认） | ✅ | ✅ | — | ✅ | 视文件 | ❌ | — |

**🔴 关键发现 1：`health_check()` 只检查 `import`，不能作为可用性判据。**

3 个源 `health_check=True`，但其中只有 AkShare 能真正取到当日数据。`SinaSource` 的 `True` 掩盖了"接口已停更 2 年"的致命问题。

### 1.2 端到端实取（关键证据）

```
=== SinaSource.fetch_bars(['rb0'],'2026-08-20','2026-08-28') ===
OK rows= 0                      # ← 静默返回空！不抛异常
Empty DataFrame

=== AkshareSource.fetch_bars(['rb0'],...) ===
FAIL KeyError : 'datetime'      # ← 代码是坏的

=== PytdxSource.fetch_bars(['rb0'],...) ===
FAIL HexDataError : pytdx 无法连接任何公共行情服务器（已试 3 个）：timed out   # 耗时 15.0s
```

**🔴 关键发现 2：`SinaSource` 的静默空返回是最危险的失败模式。**

因数据源冻结在 2024-07-17，裁剪到 2026 年区间后 `pd.concat` 得到空 DataFrame，随后 `_clip_range` → `BarFrame.validate()` **全部放行**，返回 0 行 `BarFrame` 而不报错。调用方无法区分「休市」与「源已死」。这正是 2026-08-28 停摆事故的同构隐患。

**🔴 关键发现 3：`AkshareSource` 代码逻辑错误。**

`ak.futures_main_sina()` 返回**中文列名**（`日期/开盘价/最高价/最低价/收盘价/成交量/持仓量/动态结算价`），而 `akshare_source.py:41` 执行 `df.rename(columns={"date":"datetime", ...})` → **rename 完全不生效**，随后 `df["datetime"]` 抛 `KeyError`。该源从未被真正跑通过。

### 1.3 现有测试：47 绿全是假绿

```
pytest hexbroker/data/sources/test_pytdx_source.py test_sina_source.py test_free_config.py
→ 47 passed in 0.50s
```

0.50 秒跑完 47 个"网络数据源"测试，本身即证明**零联网**。逐条核查：

| 测试 | 真实性质 |
|---|---|
| `TestFetchBars::test_fetch_bars_returns_valid_barframe`（sina） | `monkeypatch` 掉 `_require_requests`，喂假数据 `[["2024-01-02",1.0,2.0,0.5,1.5,100],...]` → **纯 mock** |
| `TestFetchBars::test_fetch_bars_contract_returns_valid_barframe`（pytdx） | `monkeypatch` 掉 `_connect`，返回 `MagicMock` → **纯 mock** |
| `TestHealthCheck::test_health_check_true_when_pytdx_present` | `pytest.importorskip("pytdx")` → 只验证 **import 成功**，不验证连接 |
| `TestResolveSymbol` / `TestFreqMapping` / `TestBarsToFrame` / `TestRepairOhlc` | 纯函数单测，有值，但**不覆盖任何真实取数** |

**覆盖盲区（比测试失败更危险）**：

| 未被任何测试覆盖 | 后果 |
|---|---|
| `_select_main_contract()` | 主力合约选取逻辑（pytdx 核心）**零验证** |
| `_fetch_continuous()` | 主力连续拼接（pytdx 核心）**零验证** |
| `ContractStitcher.stitch()`（后复权） | 后复权核心**零集成测试，且无任何源调用它** |
| 18 品种真实覆盖 | 从未验证过 |

> 结论：这 47 个绿灯**不构成任何可用性证据**。它们是「代码能 import、纯函数逻辑自洽」的证据，不是「数据源能用」的证据。

### 1.4 抽象层 ↔ 生产管线的脱节点（5 处）

生产刷新管线：
```
pandadata MCP（Agent 会话层）
  → artifacts/p6_4_pull_<YYYYMMDD>/<sym0>.json
  → scripts/p6_4_apply_persisted_dir.py
  → scripts/p6_4_fill_gaps.py --stage parse
  → data/raw/processed/{sym0}/1d/{year}.parquet
  → scripts/p22_tail_ext.py（重建信号缓存）
```

**脱节点定位（具体到文件/函数）**：

| # | 脱节点 | 具体位置 | 证据 |
|---|---|---|---|
| **D1** | **生产管线从不调用抽象层** | `p6_4_apply_persisted_dir.py` 全文仅 `import json/subprocess/pathlib`，只做 JSON→`p6_4_fill_gaps` 转发；`p6_4_fill_gaps.py` 只有 `plan/parse/verify` 三个 stage，**无取数能力** | 全仓库 grep：`PytdxSource/SinaSource` 引用仅出现在 `validate_sources.py`、`compare_data_sources.py`、`build_extended_data.py`、`paper/quotes.py` —— **无一是生产刷新链路** |
| **D2** | **桥接器能力不足** | `scripts/fetch_data.py:30` `--source choices=["csv","synthetic","tqsdk","akshare"]` —— **不含 pytdx / sina**；且默认 config `e01_cu_daily.yaml` `symbols=[SHFE.cu]` 单一品种 | 即便接线也拉不到 18 品种 |
| **D3** | **symbol 命名错位** | `pytdx_source.py:281` `df["symbol"] = product.lower()`（→ `rb`）；`DataLake.save_processed` 写 `data/raw/processed/rb/1d/{year}.parquet`；生产读 `data/raw/processed/**rb0**/1d/...` | 写进去也读不到（`SinaSource` 用 `rb0` 反而对） |
| **D4** | **口径不同源** | 抽象层 5 个源 **全部** `adj_close = close`（不复权）；生产存量是 pandadata 后复权 | 混用即污染特征 |
| **D5** | **后复权能力悬空** | `hexbroker/data/contract.py` 的 `ContractStitcher` 实现了正确的**比例后复权**，但 **grep 全仓库无任何源调用它** | 后复权是「有轮子、没装车」 |

> **回答团队的核心疑问**：不是"设计好了没接上"，而是 **D1（没接）+ D2（接不上）+ D3（接了写错地方）+ D4（口径不对）+ D5（关键能力悬空）五重脱节**，且被 mock 测试掩盖。**必须先修源、再修口径、最后接线。**

---

## 2. 候选免费/低成本数据源调研

### 2.1 已在本环境实测的候选

| # | 数据源 | 是否真免费 | 期货覆盖 | 主力连续 | 后复权 | 稳定性 | 接入成本 | 已知坑 |
|---|---|---|---|---|---|---|---|---|
| **A** | **AkShare `futures_main_sina`**（新浪**新**接口） | ✅ 免费无 key | ✅ **实测 18/18** | ✅ 名义主力连续（`rb0` 等） | ❌ 仅不复权 | 🟡 依赖新浪，字段会变 | ✅ 已装（1.18.91） | 需自建设主力+复权；akshare 升级可能 break |
| **B** | **交易所官方日行情**（SHFE/INE/CZCE） | ✅ 权威免费无额度 | 🟡 **本环境 12/18** | ❌ 逐合约，需自建 | ❌ 需自建 | ✅ 最权威 | 中（逐日文件解析） | DCE 全站 412；建历史需回补 N 个文件 |
| **C** | **pytdx（通达信）** | ✅ 免费无 key | 🟡 设计支持 | 🟡 仅"当前主力近期" | ❌ | 🔴 **本环境 10/10 服务器超时** | ✅ 已装 | 裸 TCP，企业/代理网络常封；`_select_main_contract` 逐合约发实时报价 → **性能极差**〔推断，未实测〕 |
| **D** | **新浪旧接口**（现有 `SinaSource` 在用） | ✅ | ✅ 18/18 | ✅ | ❌ | ⛔ **数据冻结 2024-07-17** | — | **必须废弃** |
| **E** | **东方财富 push2his** | ✅ | ✅ 单合约（rb2610 实测到 2026-08-25） | 🟡 需品种级 secid | ❌ | 🔴 本环境代理**间歇性 ProxyError** | 低 | 不稳定，不建议主/备 |
| **F** | **TQSDK（天勤）** | 🟡 免费版需注册账号（限"指定期货公司实盘账户"）；专业版 **14888 元/年** | ✅ | ✅ **原生主连** `KQ.m@SHFE.rb`、指数 `KQ.i@SHFE.rb` | ❌ | 🟡 需账号 | 中（⛔ **未安装、未实测**） | K 线上限 **8000 根**（日线够用）；专业版才给 2016 起 tick |
| **G** | **CTP / simnow 仿真** | 🟡 需期货公司/simnow 账号 | ✅ | ❌ 只给实时 | ❌ | ✅ | 高（项目已有 `live/ctp_skeleton.py`、`gateway.py`、`vnpy_ctp_glue.py`） | 只给实时行情，历史需自建落库；日线回填不适合〔推断，未实测〕 |
| **H** | **Baostock** | ✅ | ⛔ **无期货**（仅 A 股/指数） | — | — | — | — | **直接排除** |
| **I** | **Qlib** | ✅ | ⛔ 以股票为主，无期货日线现成方案 | — | — | — | ⛔ 未安装 | 排除 |

### 2.2 实测明细

**A. AkShare `futures_main_sina` —— 18/18 覆盖，数据到今天（2026-08-28）**

```
sym0     rows       first        last       close         OI
ag0      3482  2012-05-10  2026-08-28    17215.00     261945
al0      5268  2005-01-04  2026-08-28    23920.00     247909
au0      4541  2008-01-09  2026-08-28      999.28     186533
cf0      5269  2005-01-04  2026-08-28    17180.00     604731
cu0      5268  2005-01-04  2026-08-28   108900.00     216394
hc0      3030  2014-03-21  2026-08-28     3371.00     752064
i0       3129  2013-10-18  2026-08-28      723.00     571182
j0       3733  2011-04-15  2026-08-28     2153.50      64361
jm0      3261  2013-03-22  2026-08-28     1629.00     597545
m0       5271  2005-01-04  2026-08-28     3340.00    2744836
ni0      2780  2015-03-27  2026-08-28   128310.00     130720
p0       4582  2007-10-29  2026-08-28    10171.00     579404
rb0      4232  2009-03-27  2026-08-28     3112.00    1100758
sc0      2046  2018-03-26  2026-08-28      596.50      43476
sr0      5012  2006-01-12  2026-08-28     5411.00     621217
ta0      4786  2006-12-19  2026-08-28     5630.00     868233
y0       5014  2006-01-09  2026-08-28     8965.00     772448
zn0      4731  2007-03-26  2026-08-28    26485.00     164539
覆盖: 18/18
```

字段：`日期/开盘价/最高价/最低价/收盘价/成交量/持仓量/动态结算价`。

**为什么 AkShare 能用而 `SinaSource` 不能？** —— 接口换了：

| | HexBroker `SinaSource`（❌ 死） | AkShare `futures_main_sina`（✅ 活） |
|---|---|---|
| 端点 | `stock2.finance.sina.com.cn/futures/api/**json.php**/IndexService.**getInnerFuturesDailyKLine**` | `stock2.finance.sina.com.cn/futures/api/**jsonp.php**/.../InnerFuturesNewService.**getDailyKLine**` |
| 最新数据 | **2024-07-17** | **2026-08-28** |
| 字段数 | 6（无持仓量/结算价） | 8（含持仓量、动态结算价） |
| 格式 | `[[date,o,h,l,c,v],...]` | `[{"d","o","h","l","c","v","p","s"},...]` |

> 这是本次调研**最有价值的单条发现**：备源不是没有，是**用错了 URL**。
> 注：AkShare 的 `futures_zh_spot()`（实时行情）本环境失败（`hq.sinajs.cn` 403 + 需 `py_mini_racer`），不可用。

**B. 交易所官方日行情（权威、无额度、无频控）**

| 交易所 | 接口 | 本环境实测 | 覆盖（18 品种中） |
|---|---|---|---|
| SHFE 上期所 | `https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{YYYYMMDD}.dat` | ✅ **HTTP 200**，332 合约，含 2026-08-28 | ag al au cu hc ni rb zn = **8** |
| INE 能源 | `https://www.ine.cn/data/tradedata/future/dailydata/kx{YYYYMMDD}.dat` | ✅ **HTTP 200** | sc = **1** |
| CZCE 郑商所 | `http://www.czce.com.cn/cn/DFSStaticFiles/Future/{YYYY}/{YYYYMMDD}/FutureDataDaily.txt` | ✅ **HTTP 200**（2026-08-27 数据，管道分隔） | cf sr ta = **3** |
| DCE 大商所 | POST `http://www.dce.com.cn/publicweb/quotesdata/exportDayQuotesChData.html` | ⛔ **全站 412**（WAF 拦截，含首页） | i j jm m p y = **6（不可达）** |

SHFE/INE 记录结构（已实测）：
```json
{"OPENINTEREST":96891,"HIGHESTPRICE":108990,"TURNOVER":1901316.25,
 "CLOSEPRICE":108730,"VOLUME":34962,"PRODUCTGROUPID":"cu","PRODUCTID":"cu_f",
 "ZD2_CHG":-260,"OPENPRICE":108660,"SETTLEMENTPRICE":108760,
 "DELIVERYMONTH":"2609","PRESETTLEMENTPRICE":109020,"LOWESTPRICE":108360}
```
合约代码 = `PRODUCTGROUPID` + `DELIVERYMONTH`（`cu`+`2609` → `CU2609`）。
**含成交额 `TURNOVER`（万元）—— pandadata 的 `amount` 恒为 0，官方数据反而更全。**

> ⚠️ 踩坑记录：SHFE 旧路径 `/data/dailydata/kx/kx{date}.dat` 返回 **404**（易误判为"上期所挂了"）；正确路径是 `/data/tradedata/future/dailydata/kx{date}.dat`。

**C. pytdx** —— 本环境 10 个公共服务器（含 `119.147.212.81`、`180.153.125.194`、`218.83.166.161` 等）**全部 `TimeoutError`**。裸 TCP 7727 不走 HTTP 代理，企业/沙箱网络普遍封禁。
额外风险〔推断，未实测〕：`_select_main_contract()` 对每个候选合约逐条调用 `get_instrument_quote`，全市场数千合约 → **单次主力判定可能耗时数分钟**。

**F. TQSDK** —— 官方文档与定价（WebSearch 获取，⛔ **未安装、未实测**）：
- 免费版：需注册账号，可用"指定期货公司实盘账户"，提供免费行情服务器链接、不限次数回测、模拟交易
- 专业版 14888 元/年：2016 年以来 tick 级 + 任意 K 线周期
- 企业版 30000 元/年
- **原生主连合约** `KQ.m@SHFE.rb`、指数 `KQ.i@SHFE.rb`（对本项目价值最高）
- K 线上限 **8000 根/序列**（日线约 32 年，够用）

---

## 3. 口径对齐：本次调研的核心结论

### 3.1 pandadata `close_pcr` 到底是什么

实测证据（2026-08-27，18 品种）：

| sym0 | pandadata 收盘 | 新浪（不复权）收盘 | 比值 | pandadata dominant |
|---|---|---|---|---|
| cf0 | 8142.55 | 17030.00 | 0.4781 | CF701.CZC |
| i0 | 6288.63 | 716.50 | **8.7769** | I2701.DCE |
| m0 | 10560.21 | 3355.00 | 3.1476 | M2701.DCE |
| rb0 | 3709.76 | 3088.00 | 1.2013 | RB2610.SHF |
| y0 | 8853.85 | 8828.00 | 1.0029 | Y2701.DCE |
| **比值范围** | | | **0.4781 ~ 8.7769** | |

pandadata 值是 `11393.258718306766` 这类**任意精度浮点**，而真实期货报价是整数/最小变动价位整数倍 → **pandadata 是复权值，新浪是真实价**。

### 3.2 是"比例后复权"还是"差值平移"？—— 实测判定：**比例后复权**

用日收益率判定（比例复权不改变区间收益率；差值平移会改变）：

```
--- rb0（近 9 个交易日）---
2026-08-17  pandadata +0.033%   新浪 +0.033%
2026-08-18  pandadata +0.232%   新浪 +0.232%
2026-08-20  pandadata +0.497%   新浪 +0.497%
2026-08-24  pandadata +0.888%   新浪 +0.888%
2026-08-27  pandadata +0.390%   新浪 +0.390%
--- ag0 / m0 同上，全部逐日一致 ---
--- cu0 ---
2026-08-20  pandadata +0.328%   新浪 +0.328%
2026-08-21  pandadata +0.513%   新浪 +0.299%   << 分歧
2026-08-24  pandadata +0.363%   新浪 +0.363%
```

**统计：36 个（品种, 日期）对中 35 个收益率完全一致 → pandadata 为比例（乘性）后复权。**

✅ **好消息**：这与 HexBroker 自己的 `ContractStitcher._backward_adjust()`（`adj = raw * prod(f[k])`）**方法一致**。轮子是对的，只是没人装上车（D5）。

### 3.3 那 1/36 的分歧从哪来？—— 换月规则差异

`cu0` 的 pandadata `dominant_id` 逐日序列：

```
20260814~20260821  CU2609.SHF   ← 主力
20260824~20260827  CU2610.SHF   ← 切换
```

pandadata 在 **08-21 → 08-24** 之间换月；新浪主力连续切换时点**不同**〔推断：新浪用自有规则〕。换月点错开 → 那一天的收益率序列分歧 → **特征被污染**。

> **这是多源切换最隐蔽的风险**：不是价格水平对不上（那个肉眼可见），而是**换月日那一根的收益率对不上**，会静默污染动量/波动率类特征。

### 3.4 结论：多源切换的口径纪律

**⛔ 绝对禁止**：把 A 源的后复权序列与 B 源的不复权序列**直接拼接**。实测比值跨度 0.478~8.777，拼接 = 制造 20%~777% 的假跳空。

**✅ 推荐方案：不换口径，只换 raw 供给；备源供 raw，后复权链自己续接。**

续接算法（针对生产的「10 个交易日窗口」场景）：

```
设：t0 = 存量最后一期（pandadata 后复权 adj[t0] 已知，权威锚点）
    raw_backup[t] = 备源（AkShare/交易所）的真实（不复权）价格

情形 1 —— 窗口内无换月（dominant_id 不变，绝大多数情况）：
    adj[t] = adj[t0] * raw_backup[t] / raw_backup[t0]
    推导：比例复权在段内为常数因子 k，收益率与 raw 完全一致（已由 35/36 实测佐证）

情形 2 —— 窗口内发生换月（需 dominant 日历）：
    换月日 R，新主力 N、旧主力 O：
    factor = raw_N[R] / raw_O[R-1]
    adj[t] for t < R  : 保持不变
    adj[t] for t >= R : adj 段乘以 factor（等价于 ContractStitcher 的连乘）
```

**关键约束（必须写进验收标准）**：

| 约束 | 说明 |
|---|---|
| **① dominant 日历唯一权威 = pandadata** | pandadata payload 自带 `dominant_id` 字段。**备源不得自行决定换月日**，必须沿用 pandadata 最后一次已知的 dominant 映射 |
| **② 备源只提供 raw，不得提供"自己的主力连续"** | 用哪个合约由 dominant 日历定，备源只按合约代码返回 raw 价 |
| **③ 锚点冻结** | `adj[t0]` 与 `raw_backup[t0]` 必须用**同一天**的 pandadata 与备源值，一次标定后冻结 |
| **④ 校准校验（P0）** | 每日对比 `adj[t]/raw[t]` 是否为**常数**（段内）。若偏离 > 阈值 → 判定换月，转情形 2 并告警 |
| **⑤ 换源 = 换基准，需全量重建** | 若被迫从"续接"降级为"用备源自建全历史"，则**必须全量重建 parquet + 重建信号缓存 + 重跑 QA**，且**信号阈值需重新校准**（因水平值整体变了） |

> **④ 是防止污染的核心闸门**：段内 `adj/raw` 应为常数，这提供了一个**几乎零成本的在线一致性校验**，无需依赖 pandadata 在线。

---

## 4. 选型建议（PRD 主体）

### 4.1 推荐组合：主源 + 备源 + 兜底

| 层 | 数据源 | 角色 | 理由 |
|---|---|---|---|
| **主源** | **pandadata MCP（保留）** | 后复权口径权威 + 日线主力连续 | 存量历史全部基于此口径，换掉需全量重建；它是唯一免费提供**后复权**的源 |
| **备源** | **AkShare `futures_main_sina`（新浪新接口）** | raw 主力连续，供 §3.4 续接 | ✅ 实测 18/18 覆盖、数据到当日、含持仓量/结算价、已安装、零新增依赖。**只改 URL 即可救活** |
| **兜底** | **交易所官方日行情**（SHFE/INE/CZCE） | raw 逐合约，自建主力+复权 | 权威、无额度无频控、不受第三方停更影响；本环境覆盖 12/18（DCE 6 品种需换网络环境复测） |
| **观察** | **TQSDK** | 候选升级项（原生主连 `KQ.m@`） | 有账号后可评估；目前未安装未实测，不进 P0 |
| **废弃** | 现有 `SinaSource` 旧接口 | — | 数据冻结 2024-07-17，且静默返回空 |
| **排除** | Baostock / Qlib / 东财 push2his | — | 无期货 / 股票为主 / 本环境不稳定 |

**为什么这么配：**

1. **不换主源** —— 换主源 = 换口径 = 全量重建 + 重训 + 重校准，成本远高于"修备源"。pandadata 的 3 次失败是**配额/接线**问题（可重试、可修），不是源本身死了。
2. **备源只补 raw，不碰口径** —— 这是唯一能"零重建续接"的路径（§3.4 算法），把切换成本从"全量重建"降到"10 行数据 + 1 次校准校验"。
3. **兜底走交易所官方** —— 第三方接口（新浪）随时可能像旧接口一样停更；交易所官方数据有制度性保障，且字段最全（含成交额）。
4. **pytdx 不入 P0** —— 本环境不可达，且 `_select_main_contract` 有性能隐患；作为既有资产保留，修好后在**无代理环境**复测再决定。

### 4.2 需求池

#### P0 · Must have（阻塞生产可用性）

| ID | 需求 | 验收标准 |
|---|---|---|
| **P0-1** | **修复 `SinaSource`：切换到新浪新接口 `InnerFuturesNewService.getDailyKLine`** | ① `fetch_bars(['rb0'],'2026-08-01','2026-08-28')` 返回 **≥15 行且最后日期 = 交易日当日**；② 解析 8 字段含 `open_interest`/`settle`；③ 18/18 品种覆盖 |
| **P0-2** | **消除静默空返回**：空数据必须抛 `HexDataError`，不得返回 0 行 `BarFrame` | ① 对已停更接口取近期区间 → 抛异常而非返回空；② `BarFrame.validate()` 拒绝 0 行或在调用侧显式断言 `len>0` |
| **P0-3** | **修复 `AkshareSource`**：中文列名 rename 映射正确 | `fetch_bars(['rb0'],...)` 不再 `KeyError`，返回可 `validate()` 的 BarFrame，最后日期为当日 |
| **P0-4** | **备源 raw 拉取器**：AkShare 18 品种 raw 主力连续 | ① 18/18 成功；② 输出列 `date,open,high,low,close,volume,open_interest`；③ 单次全量 < 60s |
| **P0-5** | **后复权续接器**：实现 §3.4 算法，接 `ContractStitcher` | ① 无换月窗口：`adj` 序列与 pandadata 历史**收益率误差 < 1e-6**；② 段内 `adj/raw` 为常数（相对标准差 < 1e-9） |
| **P0-6** | **换月检测与告警**：比对 `adj[t]/raw[t]` 常数性 | ① 段内比值相对标准差 > 1e-6 → 判定换月并告警；② 提供 `--dry-run` 输出疑似换月日 |
| **P0-7** | **失败可区分**：区分「休市」「源故障」「配额超限」 | ① 各失败类型有独立退出码/异常类型；② 复用 `p6_4_apply_persisted_dir.py --trading-day` 的 exit 3 语义，不再静默 exit 0 |
| **P0-8** | **真实联网冒烟测试**（替换/补充 mock） | ① 新增 `tests/integration/test_sources_live.py`，标记 `@pytest.mark.live`；② CI 默认 skip，人工/定时任务执行；③ 断言"最后日期 ≥ 上一个交易日" |

#### P1 · Should have（提升韧性）

| ID | 需求 | 验收标准 |
|---|---|---|
| **P1-1** | **交易所官方日行情适配器**（SHFE / INE / CZCE） | ① 三所各取 1 个交易日成功解析；② 合约代码 = `PRODUCTGROUPID`+`DELIVERYMONTH`；③ 覆盖该所全部 18 品种内标的 |
| **P1-2** | **DCE 大商所适配**（需换网络环境复测） | ① 在**无代理环境**验证 POST 表单可用；② 若仍 412，明确标注"本环境不可达"并给出 WAF 绕过评估 |
| **P1-3** | **多源 failover 编排器**：主→备→兜底自动降级 | ① 主源失败自动切备源，全程记录 `source` 溯源字段；② 每次切换写 audit 日志 |
| **P1-4** | **dominant 日历落盘**：从 pandadata payload 抽取 `dominant_id` 存为权威换月日历 | ① 落盘 `data/raw/dominant_calendar.json`；② 备源续接强制读取该日历 |
| **P1-5** | **pytdx 在无代理环境复测** | ① 10 服务器连通性复测报告；② `_select_main_contract` 耗时实测（预期性能问题需量化） |
| **P1-6** | **TQSDK 评估**（需账号） | ① 安装并验证 `KQ.m@SHFE.rb` 主连可取；② 免费版额度/频控实测报告 |
| **P1-7** | **修复 D3 symbol 命名错位** | `PytdxSource._fetch_continuous` 输出 `sym0` 格式（`rb0` 而非 `rb`），落盘路径与生产一致 |

#### P2 · Nice to have

| ID | 需求 | 验收标准 |
|---|---|---|
| **P2-1** | 成交额 `amount` 补齐（交易所官方有 `TURNOVER`，pandadata 恒为 0） | 回填 18 品种 amount 列，覆盖率 > 95% |
| **P2-2** | 多源交叉校验看板：主/备/兜底三源收盘价偏差日报 | 偏差 > 0.5% 告警，> 2% 视为异常（沿用 `free.yaml` 阈值） |
| **P2-3** | CTP 实时行情接入（`live/ctp_skeleton.py` 已有骨架） | 实时价格与主源日线末值偏差 < 0.5% |
| **P2-4** | 结算价 vs 收盘价口径统一 | 明确特征工程用哪个；涨跌幅统一用**昨结算价**（非昨收盘） |
| **P2-5** | 数据源健康度 SLA 报表 | 各源近 30 日成功率/延迟/P95 |

### 4.3 待确认问题（需主理人/用户拍板）

| # | 问题 | 为什么必须拍板 | 建议 |
|---|---|---|---|
| **Q1** | **是否接受"备源只续接 10 日窗口、不重建全历史"？** | 这是本方案把成本从"全量重建"降到"增量续接"的关键假设。若要求全历史也用备源重建，需重训+重校准，工作量差一个数量级 | 建议接受；**全量重建仅在 pandadata 彻底不可用 > 5 个交易日时触发** |
| **Q2** | **pandadata 配额 500009 的具体规则是什么？（日/月？按品种还是按次？）** | 决定主源能否长期稳定。若配额可预测，可设计错峰拉取 | 需查 pandadata 文档或询问连接器方 |
| **Q3** | **能否拿到 pandadata 的历史 `dominant_id` 全量日历？** | §3.4 算法情形 2（换月）强依赖它。只有最近 10 天的 payload 不够建历史换月链 | 建议先在重叠窗口验证算法，再补历史 |
| **Q4** | **是否接受新增 TQSDK 依赖与其账号注册（可能需期货公司账户）？** | TQSDK 有原生主连，是最省事的主力连续来源；但免费版疑似绑定期货公司实盘账户 | 建议先做 P1-6 评估，再决定 |
| **Q5** | **DCE 6 个品种（i j jm m p y）在用户实际部署环境是否可达？** | 本环境 412 是 WAF 拦截，不代表用户环境不可达。这 6 个品种占 18 品种的 1/3 | 请在**本地无代理环境**跑一次 `scripts/dev_probe_36_sources.py` 确认 |
| **Q6** | **特征工程用的是收盘价还是结算价？** | 有夜盘品种昨收≠昨结，涨跌幅口径会错。pandadata 同时给了 `close`/`settlement`/`pre_settlement` | 建议统一：涨跌幅用**昨结算价** |
| **Q7** | **主力合约选取规则是否要与 pandadata 对齐到"1.1 倍持仓量切换"？** | AkShare 文档记载该规则。若我们的规则不同，换月日会错开（已实测 cu0 分歧） | 建议直接沿用 pandadata 的 `dominant_id`，不自创规则 |

---

## 5. 交付物与复现方式

| 文件 | 说明 |
|---|---|
| `scripts/dev_probe_36_sources.py` | 18 品种 × {sina, eastmoney, akshare} 覆盖探针（只读，无副作用） |
| `scripts/dev_probe_36_sina_new.py` | 新浪**新旧接口对比**探针（证明旧接口冻结在 2024-07-17） |
| `scripts/dev_probe_36_alignment.py` | pandadata 后复权 vs 新浪不复权**口径差异**实测 |
| `artifacts/p36_probe_sina.json` | sina 18 品种探测结果 |
| `artifacts/p36_probe_em.json` | eastmoney 18 品种探测结果（本环境全失败） |

复现：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd E:/Workspace/HexBroker
"$PY" scripts/dev_probe_36_sources.py --source sina      --json artifacts/p36_probe_sina.json
"$PY" scripts/dev_probe_36_sina_new.py
"$PY" scripts/dev_probe_36_alignment.py
"$PY" -m pytest hexbroker/data/sources/ -v     # 当前 47 passed（全 mock）
```

---

## 6. 未实测项清单（诚实声明）

| 项 | 状态 | 原因 |
|---|---|---|
| pytdx 真实取数 | ⛔ **未实测** | 本环境 TCP:7727 全超时（10/10）。**不等于源已死**，需无代理环境复测 |
| pytdx 主力连续拼接正确性 | ⛔ **未实测** | 同上；且 `_select_main_contract` 性能未量化 |
| TQSDK 全部功能 | ⛔ **未实测** | 未安装；定价/额度信息来自 WebSearch 官方文档 |
| DCE 大商所 | ⛔ **本环境不可达** | 全站 412（WAF）。12 品种中 6 个属 DCE |
| CTP / simnow | ⛔ **未实测** | 需账号与期货公司环境；仅代码骨架评估 |
| 东财 push2his 稳定性 | 🟡 **部分实测** | 曾成功 1 次（返回 2026-08-25 数据），后持续 ProxyError → 判为**本环境不稳定** |
| 中金所 CFFEX | ⛔ **未实测** | SSLError（证书问题）；18 品种不含金融期货，影响有限 |
| 备源续接算法（P0-5/6） | ⛔ **未实现、未验证** | 算法为**设计方案**，§3.2 的 35/36 收益率一致是其理论依据，但代码尚未落地 |

> 本报告中凡标注「〔推断〕」处均为逻辑推断而非实测事实，不得作为验收依据。
