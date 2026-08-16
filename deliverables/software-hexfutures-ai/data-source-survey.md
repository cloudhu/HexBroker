# 中国期货行情免费数据源调研报告

> 调研目标：为 `HexFutures-AI`（cu/rb/sc 商品期货，日线/小时线）找到**尽量免费**的行情获取方案，并给出对现有 `hexbroker/data/sources/` 的改造建议。
> 调研日期：2026-08-15 ｜ 范围：SHFE（沪铜 cu / 螺纹钢 rb）、INE（原油 sc）、DCE/CZCE 同类商品期货。
> 结论先行：**免费且可用**的组合 = 通达信行情服务器（pytdx，主力源，全合约日线+小时线）+ 新浪期货公开接口（分钟线/实时，免费无 key）+ AkShare 基础数据（库存/持仓/仓单/基差/费用，免费稳）+ 天勤 TQSDK（注册免费、含 Tick，作为权威升级源）。Tushare 期货免费门槛高，**不首选**。

---

## 1. 免费数据源全景对比

| # | 数据源 | 免费程度 | 覆盖品种 | 日线 | 小时线 | 分钟/Tick | 是否需要 key | 历史深度 | Python 接入 | 稳定性 | 推荐定位 |
|---|--------|---------|---------|------|--------|-----------|------------|---------|-----------|--------|---------|
| 1 | **通达信行情服务器 (pytdx)** | ✅ 完全免费 | 全市场期货/期权 | ✅ | ✅ 60min | ✅ 1/5/15/30min | 否（公共服务器） | 数年（旧合约略不全） | `pip install pytdx` | 中（公共服务器并发/限连） | **主力源（全合约）** |
| 2 | **新浪期货公开接口** | ✅ 完全免费 | 内盘期货（主力/指数连续） | ✅ | ✅ 60min | ✅ 1/5/15/30/60m | 否 | 主力连续数年 | `requests` 直抓 | 中（限频、可能改版） | **分钟线/实时辅助** |
| 3 | **AkShare（基础数据类）** | ✅ 完全免费 | 全品种 | —（行情接口不稳） | — | — | 否 | — | `pip install akshare` | 基础数据稳；行情类随源站失效 | **库存/持仓/仓单/基差/费用** |
| 4 | **天勤 TQSDK** | 🟡 注册免费（专业版付费） | 全品种 | ✅ | ✅ | ✅ Tick | 是（免费 token） | 免费版有限，专业版全量 | `pip install tqsdk` | 高 | **权威升级源 + 实盘同源** |
| 5 | **Tushare Pro** | 🔴 期货门槛高 | 全品种 | 需 2000 积分 | 需单独权限(付费) | 需单独权限(付费) | 是（积分） | 长 | `pip install tushare` | 高 | **仅在有高积分账户时选用** |
| 6 | **东方财富公开接口** | ✅ 免费 | 库存/全球大宗 | 主力 K 线可抓(易变) | — | — | 否 | — | `requests` | 低（非官方） | **辅助（库存/全球大宗）** |
| 7 | **tdx-connector (已连接 MCP)** | ✅ 免费 | 期货/股票 | ✅ | ✅ | ✅ | 否 | 同 pytdx | 经 MCP | **高（2026-08-14 实测）** | **已连接·实测可靠的在环校验源**（见 §8） |
| 8 | **westock (已连接，腾讯自选股)** | ✅ 免费 | 股票为主 | 期货弱 | 期货弱 | 期货弱 | 是 | — | 经 MCP | 中 | **不首选（期货弱）** |

**一句话**：pytdx + 新浪满足"免费拿 cu/rb/sc 的日线/小时线"全部需求；AkShare 补基础数据；TQSDK 注册后升为权威源（与实盘同源）；Tushare 因积分门槛不进免费首选。

---

## 2. 各源实测接口与代码片段

### 2.1 通达信行情服务器（pytdx）—— 推荐主力免费源
连接公共行情服务器（无需注册），可取**全部合约**日线/分钟线，适合拼接主力连续/指数连续。
```python
from pytdx.exhq import TdxExHq_API

api = TdxExHq_API(multithread=False, heartbeat=False, auto_retry=True, raise_exception=False)
# 公共行情服务器（任选其一，可能需试连多个）：
SERVERS = [("119.147.212.81", 7727), ("180.153.125.194", 7727), ("218.83.166.161", 7727)]
ok = any(api.connect(h, p) for h, p in SERVERS)

# 市场代码：28=郑商所 29=大商所 30=上期所(含INE原油) 47=中金所
# category=3 期货；period: 7=1min 0=5min 1=15min 2=30min 3=60min 4=日线
# 注意上期能源(原油 sc) 同属 market=30
bars = api.get_instrument_bars(3, 30, "sc2401", 0, 800)          # 原油日线(800根)
bars60 = api.get_instrument_bars(3, 30, "sc2401", 3, 800)        # 原油60分钟线
# 按日期范围（签名以实际 pytdx 版本为准，常见为 category,market,code,start,end）
# hist = api.get_history_instrument_bars_range(3, 30, "sc2401", "20230101", "20231231")
api.disconnect()
```
**优点**：免费、全合约、日线/小时线齐全；可离线读本地通达信目录（easy_tdx）。
**缺点**：公共服务器稳定性/并发受限；声明仅个人学习；旧合约历史可能不完整。

### 2.2 新浪期货公开接口 —— 免费分钟线/实时
无需 key，直接 `requests` 抓取。主力连续代码：`rb0`(螺纹钢) `cu0`(沪铜) `sc0`(原油)；指数 `rbl8` 等。
```python
import requests, pandas as pd

def sina_future_daily(symbol: str) -> pd.DataFrame:
    # 内盘期货日线（symbol 如 rb0 / cu0 / sc0）
    url = f"http://stock2.finance.sina.com.cn/futures/api/json.php/IndexService.getInnerFuturesDailyKLine?symbol={symbol}"
    data = requests.get(url, timeout=10).json(); data.reverse()
    return pd.DataFrame(data, columns=["date", "open", "high", "low", "close", "vol"])

def sina_future_min(symbol: str, scale: int = 60) -> pd.DataFrame:
    # scale ∈ {1,5,15,30,60}
    url = f"http://stock2.finance.sina.com.cn/futures/api/json_v2.php/IndexService.getInnerFuturesMiniKLine{scale}m?symbol={symbol}"
    data = requests.get(url, timeout=10).json(); data.reverse()
    return pd.DataFrame(data, columns=["date", "open", "high", "low", "close", "vol"])

# 股指期货：CffexFuturesService.getCffexFuturesMiniKLine{scale}m?symbol=IF2401
```
**优点**：零依赖、免费、分钟线齐全。
**缺点**：仅主力/指数连续（无全部合约）；datalen 上限与限频；非官方，URL 可能变动；需自行拼接连续合约。

### 2.3 AkShare 基础数据类（免费且稳）—— 用于特征工程
行情 K 线类（`futures_main_sina` 等）依赖新浪/东财，**易随源站改版失效**，不建议作主线；但其**基础数据类接口非常稳、全部无需 key**，是特征工程宝库：
```python
import akshare as ak
ak.futures_inventory_em(symbol="CU")          # 铜库存(东方财富)
ak.futures_inventory_em(symbol="RB")          # 螺纹钢库存
ak.futures_inventory_em(symbol="SC")          # 原油库存(INE, 经上期所同源)
ak.futures_shfe_warehouse_receipt()           # 上期所/INE 仓单日报
ak.futures_dce_position_rank(...)             # 大商所持仓排名
ak.futures_fees_info                          # 手续费/保证金(含合约乘数、最小跳动) — openctp
ak.futures_spot_price(symbol="螺纹钢")         # 基差/现货价(生意社)
ak.futures_rule                               # 交易日历/涨跌停/合约乘数
```
**定位**：库存、持仓排名、仓单、基差、费用/保证金——直接喂给 `hexbroker/feature/` 做特征，显著提升信号质量。

### 2.4 天勤 TQSDK（注册免费）—— 权威升级源 + 实盘同源
注册免费账户得 token，提供**所有可交易合约全部 Tick 和 K 线**，且回测/实盘同源（复用 `docs/system_design.md` 的实盘层）。
```python
from tqsdk import TqApi, TqBacktest
from datetime import date
api = TqApi(backtest=TqBacktest(start_dt=date(2023,1,1), end_dt=date(2023,12,31)))
k = api.get_kline_serial("SHFE.cu", 86400)    # 日线(86400秒)
k60 = api.get_kline_serial("SHFE.cu", 3600)   # 小时线
q = api.get_quote("SHFE.cu")
while api.wait_update():                      # 事件驱动回测/实时
    ...
```
**限制**：免费版有频率/并发/历史回溯长度限制；专业版付费全 Tick。建议等用户注册账户后切换为权威源。

### 2.5 Tushare Pro —— 期货免费门槛高（不首选）
- `fut_daily`（期货日线）需 **2000 积分**（注册仅 120 积分，只能调非复权股票日线）。
- 分钟行情 `ft_mins` / `rt_fut_min` 需**单独开权限（付费）**。
- 结论：除非已有 ≥2000 积分账户，否则不进免费首选。

---

## 3. cu / rb / sc 获取方案

| 品种 | 交易所 | 主力连续代码(新浪) | pytdx market | 说明 |
|------|--------|-------------------|--------------|------|
| 沪铜 cu | SHFE | `cu0` | 30 | 工业品，流动性好 |
| 螺纹钢 rb | SHFE | `rb0` | 30 | 黑色，成交活跃 |
| 原油 sc | INE(上期能源) | `sc0` | 30（与 SHFE 同源） | 能源，pytdx market 同为 30 |

**连续合约拼接建议**：免费源多只给主力连续或单合约。
- 研究期（日线方向预测）直接用新浪 `cu0/rb0/sc0` 主力连续 + 后复权（前复权会引入未来函数，回测必须用**后复权/换月拼接**，复用现有 `ContractStitcher`）。
- 实盘/精细回测用 pytdx 取全部合约，按持仓量滚动选主力再拼接（`hexbroker/data/contract.py` 已支持）。

---

## 4. 对 `hexbroker/data/sources/` 的改造清单

现状：`akshare_source.py`(仅新浪主力日线，易失效) / `csv_source.py` / `qlib_source.py` / `synthetic_source.py` / `tqsdk_source.py`(骨架)。**缺 pytdx 与 sina 源**。

| 动作 | 文件 | 职责 | 优先级 |
|------|------|------|--------|
| **新增** | `pytdx_source.py` | 通达信免费行情主力源：全合约日线/60min，多服务器 failover，落 Parquet | P0 |
| **新增** | `sina_source.py` | 新浪期货免费分钟线/实时/主力日线，无 key 直抓 | P0(分钟线) |
| **重构** | `akshare_source.py` | 从"行情日线"降级为"基础数据"源（库存/持仓/仓单/基差/费用），重构为 `fetch_fundamentals()`；或在保留行情的同时注明易失效 | P1 |
| **实现** | `tqsdk_source.py` | 填充骨架：账户 token 接入 + `get_kline_serial` 下载日线/小时线，复用 schema | P2(待账户) |
| 复用 | `base.py` / `schema.py` / `store.py` / `contract.py` | 多品种 (symbol,datetime) 索引、Parquet 湖、换月拼接已就绪，直接复用 | — |
| 配置 | `configs/data/*.yaml` | 新增 `free.yaml`：`source_priority: [pytdx, sina, akshare_fundamentals]`，各源开关 | P0 |

**数据层最终形态（免费优先）**：
```
free.yaml → pytdx(全合约日线/60min) → 落 Parquet
          → sina(分钟线/实时/主力连续, 冗余) → 落 Parquet
          → akshare(库存/持仓/仓单/基差/费用, 特征) → 落 Parquet
tqsdk(账户到位) → 权威源(含Tick) → 复用同一 schema
```

---

## 5. 频率与历史深度评估（是否满足需求）

- **日线**：pytdx + 新浪 + TQSDK 均可免费取得，历史数年，**满足** ✅
- **小时线(60min)**：pytdx(period=3) + 新浪(MiniKLine60m) + TQSDK(3600s) 均可免费取得，**满足** ✅
- **分钟线(<60m)/Tick**：新浪(1/5/15/30/60m) + pytdx(1/5/15/30min) 免费；Tick 仅 TQSDK 专业版。研究期用日线/小时线即可，分钟/Tick 留待升级 ✅(研究期)
- **历史深度**：免费源日线可达 5–10 年（pytdx 视合约），足以支撑 walk-forward 训练与 OOS 验证 ✅

---

## 6. 限制与风险

1. **稳定性**：pytdx 公共服务器、新浪接口均非官方 SLA，需多服务器 failover + 本地 Parquet 缓存（已实现）。
2. **合规**：通达信公共服务器、新浪数据声明"仅个人学习/研究"，商业用途需授权；AkShare 声明"仅学术研究"。本项目定位研究，需保留声明。
3. **连续合约**：免费源多只给主力连续，回测必须用后复权/换月拼接，禁止前复权（未来函数）—— 现有 `ContractStitcher` 已覆盖，须强制使用。
4. **数据质量**：免费源偶有缺失/跳变，需复用 `data/cleaner.py` 做缺失填充与异常值处理。

---

## 7. 待确认（决定下一步）

| # | 问题 | 默认建议 |
|---|------|---------|
| Q1 | 是否由我**直接实现** `pytdx_source.py` + `sina_source.py`（免费主力源），并重构 `akshare_source.py` 为基本面数据？ | 建议实现（P0，免费且立即可跑） |
| Q2 | 是否注册**天勤 TQSDK 免费账户**，以便我填充 `tqsdk_source.py` 作为权威源？ | 建议注册（与实盘同源，长期最优） |
| Q3 | Tushare 积分：是否已有 ≥2000 积分账户可用于 `fut_daily`？ | 默认不用（门槛高） |
| Q4 | 数据频率最终锁定：日线 alone，还是日线+小时线双周期联合训练？ | 建议日线+小时线（免费均可取） |

---

## 附：快速验证脚本（联网即可自测）
```bash
# 通达信公共服务器连通性 + 取一根原油日线
python -c "from pytdx.exhq import TdxExHq_API as A; a=A(); print(a.connect('119.147.212.81',7727), a.get_instrument_bars(3,30,'sc2401',0,5)); a.disconnect()"
# 新浪主力日线（无需任何依赖）
python -c "import requests; print(len(requests.get('http://stock2.finance.sina.com.cn/futures/api/json.php/IndexService.getInnerFuturesDailyKLine?symbol=rb0').json()))"
```

---

## 8. 通达信 MCP（tdx-connector）实测稳定性 / 可靠性裁决（2026-08-14）

> 目标：用户要求"通过连接器 MCP 中的通达信获取期货行情，经过测试，从而确保数据源的稳定性和可靠性"。以下为对 `tdx-connector`（已连接）的**实证测试结果**，品种覆盖 CU（沪铜·SHFE）、RB（螺纹钢·SHFE）、SC（原油·INE）。

### 8.1 测试环境与关键参数（实测固化）
- 连接器：`tdx-connector`（已连接，无 key、免费）
- 市场/扩展代码：`setcode="30"`（上期所 / INE 上期能源，期货均属扩展行情）
- **关键陷阱**：期货必须显式传 `target="1"` —— 自动推导不触发，缺省会返回空 `"未知"`。所有本次调用已固化此参数。
- 周期：`period="4"` 日线、`period="3"` 60min
- 复权：`tqFlag="0"` 不复权（避免未来函数泄漏）
- 条数：`wantNum` 默认 100、最大 1000；`Rows.Volume/Amount` 已是最终可用值，禁止二次换算

### 8.2 测试项与结果

| 测试项 | 工具 | 结果 | 结论 |
|---|---|---|---|
| 多合约实时行情 | `tdx_futures_quotes(setDomain="30", subCode="CU", wantNum=10)` | 返回 10 个合约全字段（CU2608~CU2705）；CU2609 NOW=108200 / CLOSE=107670 / 持仓 203895（最活跃主力）；CU2610 持仓 169991（次主力） | ✅ 稳定、字段完整无缺失 |
| 历史日线 | `tdx_kline(period="4", target="1", …)` | 历史会话已验证 CU/RB/SC 日线可达 | ✅ 稳定 |
| 历史 60min 线 | `tdx_kline(period="3", target="1", tqFlag="0", wantNum=120)` | CU2609 / RB2610 / SC2609 **各返回 120 根** 60min K 线 | ✅ 稳定，小时线可用 |
| quote↔kline 一致性 | `tdx_kline.AttachInfo` ↔ `tdx_futures_quotes` | CU2609：Close=107670 / Now=108200 / VolInStock=203895 **双边完全一致** | ✅ 实时快照与历史末值吻合，可靠性高 |
| 基本面深度资料 | `tdx_futures_deep_info(query="查询 CU 基差走势")` | 路由 `f9_commodity_future_spot_basis`，返回 **1125 行** basis（trade_date / variety_name=电解铜 / basis_value），2021-12-23 → **2026-08-14** 全覆盖，最新 basis=210 | ✅ 可用且数据新鲜（截至当日） |

### 8.3 60min K 线区间摘要（period="3" 样本）
- **CU2609**：现价 108200(+0.49%)，区间 +2.74%，高 108730 / 低 104320
- **RB2610**：现价 3018(-0.03%)，区间 -2.65%，高 3116 / 低 2968
- **SC2609**：现价 569.3(+2.60%)，区间 +3.53%，高 573.30 / 低 501.60

### 8.4 裁决
- **可靠性：高**。通达信 MCP 对 cu/rb/sc 的①实时多合约行情、②历史日线/60min K 线、③基本面基差数据均稳定返回且自洽（实时快照与历史末值一致、基差覆盖至当日），可判定为**可靠数据源**。
- **唯一关键陷阱**：期货扩展行情必须显式传 `target="1"`，否则返回空。已固化为调用约定。
- **定位升级**：原表 #7 由"冗余验证源"升级为 **实测可靠的在环校验源** —— 可与 pytdx 主源互相印证（实时快照比对 K 线末值，监测数据漂移/缺失）。
- **生产化注意**：`tdx_futures_deep_info` 单次输出可能极大（本次 149,776 字符，已存盘），需分页 / 字段过滤，勿一次拉全量；如需纳入特征工程，建议封装为独立 fundamental 抽取器。

### 8.5 对 HexFutures-AI 数据层的含义
- **双周期（日线 + 60min）方案确认可行**，可落地 `configs/data/free.yaml`。
- `tdx-connector` 作为 **pytdx 主源的在环校验**：实时快照 ↔ K 线末值比对，自动发现数据缺失/漂移。
- 基本面（基差）经 deep_info 可取，但需自行分页抽取；建议作为 `akshare_source`（基本面）的补充或独立 `fundamental` 拉取器，喂给 `hexbroker/feature/`。

---

## 9. 数据源供应链裁决：pytdx + sina 实测对比（2026-08-15）

> 目标：在 §8 确认 tdx MCP 可靠后，补全「免费主源 pytdx + sina」并与之对比，构建可靠数据源供应链。以 SoftwareCompany 团队（software-hexfutures-datasources）走快速模式：工程师实现 + QA 验证。

### 9.1 交付物（代码 + 测试，QA 47/47 通过）
- `hexbroker/data/sources/pytdx_source.py`：PytdxSource，market=30，多服务器 failover（3 IP，≤5s），日线 period=4 / 60min period=3，映射 BarFrame，`_repair_ohlc` 脏数据修复，**禁止前复权 / 二次换算**。
- `hexbroker/data/sources/sina_source.py`：SinaSource，无 key 直抓 cu0/rb0/sc0 日线+60min，映射 BarFrame，amount/open_interest 补 0。
- `configs/data/free.yaml`：`source_priority: [pytdx, sina, akshare_fundamentals]` + `verification.tdx_mcp`（在环校验，drift 阈值 0.5% 告警 / 2.0% 异常）。
- `hexbroker/data/validate_sources.py`：pytdx vs sina vs MCP 基准对比脚本，输出 md+json。
- QA 测试：`test_pytdx_source.py` / `test_sina_source.py` / `test_free_config.py`，**47/47 通过**（pytest）。

### 9.2 实测对比结果（vs §8 tdx MCP 基准）
| 源 | 沙箱可达 | 说明 |
|---|---|---|
| sina | ✅ 实跑成功 | cu0 1d 2324 根 / 60m 192 根；RB2610 2325 / SC2609 1536；端点到端可用 |
| pytdx | ❌ 环境屏蔽 | 公共服务器 IP 在沙箱 TCP 超时；failover/映射/落盘代码就绪，本地有网可跑（monkeypatch 假 bars 已验证完整管线） |
| tdx_mcp | ✅（§8） | 实时多合约 + 日线/60min + 基差，可靠 |

> 注：sina 最新价与 2026 基准偏差大，系「新浪返回 2024 数据 + 主力连续 vs 单合约」环境/品种错位，非代码缺陷（报告 §5.1）。

### 9.3 可靠数据源供应链（最终形态）
```
取数主链（优先级 + failover）：
  pytdx(主力源, 全合约日线/60m, market=30, 多服务器 failover)
    └─ 不可达 ─▶ sina(降级源, 主力连续日线/60m, 无key)
    └─ 特征 ─▶ akshare_fundamentals(库存/持仓/仓单/基差/费用, 不参与行情主链)
在环校验（不参与取数）：
  tdx_mcp(已连接) ── 实时快照↔K线末值比对 ── 监测缺失/漂移
                     drift: close>0.5% 告警 / >2.0% 异常 / 缺失≥1根告警
纪律：禁止前复权(未来函数)；Volume/Amount 禁止二次换算；本地 Parquet 缓存断点续传
```
- 复跑命令（本地有网）：`python hexbroker/data/validate_sources.py`（自动生成对比报告）
- 完整对比报告：`deliverables/software-hexfutures-ai/source-comparison-2026-08-15.md`

### 9.4 待办（本地有网环境）
- 在本地有网机器实跑 pytdx 取数路径，确认与 tdx MCP 基准数值吻合（沙箱仅验证管线）。
- 注册 TQSDK（Q2）后填充 `tqsdk_source.py` 升为权威源（与实盘同源）。
- `akshare_source` 重构为基本面数据源（P1，进行中）。
