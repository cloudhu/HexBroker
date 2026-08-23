"""通达信公共行情服务器（pytdx）免费期货数据源 —— 主力免费源（P0）。

连接公共行情服务器（无需注册），可取**全合约**日线/60min，适合拼接主力连续/指数连续。
设计要点（与现有 schema 严格对齐，禁止自创命名）：

- 复用 ``DataSource`` 基类与 ``BarFrame`` 契约（``(symbol, datetime)`` 两级索引）。
- 多服务器 failover：内置 ≥3 个公共服务器 IP，连接超时 ≤5s，逐个试连。
- 周期映射：本项目 ``freq`` -> pytdx exhq ``category(period)``（4=日线, 3=60min）。
- 市场代码：``market=30``（上期所/INE 原油，期货均属扩展行情，无需 MCP 的 ``target`` 参数）。
- ``Volume``/``Amount`` 已是最终可用值，**禁止二次换算**；**禁止前复权**（仅不复权或后复权/
  换月拼接，复用现有 ``ContractStitcher``）。
- ``fetch_bars`` 签名对齐 ``base.DataSource``；取数后通过 ``DataLake`` 落 Parquet。
- 提供主力连续拼接辅助：按持仓量选主力，或接受 ``cu0/rb0/sc0`` 风格代码。

依赖缺失时 ``health_check`` 返回 False，``fetch_bars`` 抛明确异常（沿用 akshare 源风格）。
"""

from __future__ import annotations

import time
from typing import Optional

import pandas as pd

from ... import HexConfigError, HexDataError
from ..base import DataSource
from ..schema import BarFrame
from ..store import DataLake

# ---------------------------------------------------------------------------
# 常量（集中声明，禁止硬编码魔法数字在业务代码；见 config.py 约定）
# ---------------------------------------------------------------------------
# 通达信公共行情服务器（任选其一，可能需试连多个）。连接超时 ≤5s。
DEFAULT_SERVERS: list[tuple[str, int]] = [
    ("119.147.212.81", 7727),
    ("180.153.125.194", 7727),
    ("218.83.166.161", 7727),
]

# 本项目 freq -> pytdx exhq ``category``（即 K 线周期）。
# 4=日线, 3=60min, 2=30min, 1=15min, 0=5min, 7=1min。
FREQ_TO_PERIOD: dict[str, int] = {
    "1d": 4,
    "60m": 3,
    "30m": 2,
    "15m": 1,
    "5m": 0,
    "1m": 7,
}

# 上期所/INE 市场代码（期货均属扩展行情）。
MARKET_SHFE_INE: int = 30
# pytdx exhq 期货 category 固定为 3（与 hq 不同，exhq 用 category 区分品种类型）。
CATEGORY_FUTURE: int = 3

# 单次 ``get_instrument_bars`` 最大条数（pytdx 限制）。
MAX_BARS_PER_CALL: int = 800

# 连续主力代码后缀（新浪风格与项目风格统一识别）。
CONTINUOUS_SUFFIX = "0"

# 已知期货交易所 pytdx exhq market 代码（用于跨所自动发现主力/合约）。
# 30=SHFE(上期所) 47=DCE(大连) 28=CZCE(郑商所) 29=CFFEX(中金所) 60=INE(上海国际能源)。
_KNOWN_MARKETS = (MARKET_SHFE_INE, 47, 28, 29, 60)


def _product_of_code(code: str) -> str:
    """取合约代码前导字母部分（品种标识），如 'M2509'->'m'、'MA2509'->'ma'。

    精确匹配品种，避免 'm'(豆粕) 误匹配 'MA'(甲醇) 等同字母前缀合约。
    """
    return "".join(ch for ch in str(code) if ch.isalpha()).lower()


class PytdxSource(DataSource):
    """通达信公共行情服务器免费期货源（主力源）。"""

    name = "pytdx"

    def __init__(
        self,
        servers: Optional[list[tuple[str, int]]] = None,
        connect_timeout: float = 5.0,
        rate_limit_sleep: float = 0.2,
        root: Optional[str] = None,
        save: bool = True,
    ) -> None:
        """初始化 pytdx 源。

        参数
        ----
        servers : 公共行情服务器列表 ``[(host, port), ...]``，默认内置 3 个。
        connect_timeout : 单服务器连接超时（秒），默认 5s。
        rate_limit_sleep : 多次请求之间的保护间隔（秒）。
        root : ``DataLake`` 根目录（落 Parquet），默认 ``data/raw``。
        save : 取数后是否落 Parquet，默认 True。
        """
        self.servers = servers if servers is not None else list(DEFAULT_SERVERS)
        self.connect_timeout = connect_timeout
        self.rate_limit_sleep = rate_limit_sleep
        self.save = save
        self.lake = DataLake(root if root is not None else "data/raw")
        self._api = None

    # ---- 连接与 failover -------------------------------------------------
    def _connect(self):
        """连接任一可用公共服务器，全部失败则抛 ``HexDataError``。"""
        try:
            from pytdx.exhq import TdxExHq_API  # 懒加载：缺依赖不阻断 import
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise HexConfigError(
                "未安装 pytdx，无法使用 pytdx 数据源。请 pip install pytdx，"
                "或使用 --source csv。"
            ) from exc

        api = TdxExHq_API(
            multithread=False, heartbeat=False, auto_retry=True, raise_exception=False
        )
        last_err: Optional[Exception] = None
        for host, port in self.servers:
            try:
                ok = api.connect(host, port, time_out=self.connect_timeout)
                if ok:
                    self._api = api
                    return api
            except Exception as exc:  # 连接异常（超时/拒绝）继续试下一个
                last_err = exc
                continue
        # 全部失败：清晰抛错，不静默降级
        raise HexDataError(
            f"pytdx 无法连接任何公共行情服务器（已试 {len(self.servers)} 个）：{last_err}"
        )

    def _disconnect(self) -> None:
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._api = None

    # ---- 符号解析 --------------------------------------------------------
    @staticmethod
    def _resolve_symbol(symbol: str) -> tuple[str, str]:
        """将项目符号解析为 ``(kind, value)``。

        - ``SHFE.cu`` / ``INE.sc`` -> ``("continuous", "cu")``
        - ``cu0`` / ``rb0`` / ``sc0`` -> ``("continuous", "cu")``
        - ``CU2609`` / ``cu2401`` -> ``("contract", "CU2609")``（tdx 合约代码，大写）
        """
        s = symbol.strip()
        if "." in s:
            product = s.split(".", 1)[1].lower()
            return "continuous", product
        if s and s[-1].isdigit() and s[:-1].isalpha() and s.endswith(CONTINUOUS_SUFFIX):
            # 形如 cu0 / rb0 / sc0
            return "continuous", s[:-1].lower()
        # 单合约代码（如 CU2609）：转大写作为 tdx 合约代码
        return "contract", s.upper()

    # ---- 单合约取数 ------------------------------------------------------
    def _fetch_bars_for_market(
        self, api, market: int, code: str, period: int, count: int
    ) -> list[dict]:
        """在指定 ``market`` 拉取单合约 K 线（分页拉满 ``count`` 根）。

        无数据或失败返回空 list（不抛异常），由调用方决定跨市场回退，
        避免单市场失败即中断（旧实现 market=30 硬编码导致 DCE 等永失败）。
        """
        rows: list[dict] = []
        fetched = 0
        for start in range(0, max(count, 1), MAX_BARS_PER_CALL):
            take = min(MAX_BARS_PER_CALL, count - fetched)
            if take <= 0:
                break
            try:
                chunk = api.get_instrument_bars(
                    CATEGORY_FUTURE, market, code, start, take
                )
            except Exception as exc:
                raise HexDataError(f"pytdx 取 {code}(market={market}) 失败：{exc}") from exc
            if not chunk:
                break
            rows.extend(chunk)
            fetched += len(chunk)
            if len(chunk) < take:
                break
            if self.rate_limit_sleep:
                time.sleep(self.rate_limit_sleep)
        return rows

    def _fetch_contract_bars(
        self, code: str, period: int, count: int, market: Optional[int] = None
    ) -> pd.DataFrame:
        """取单合约 K 线（分页拉满 ``count`` 根），返回单 symbol 的 DataFrame。

        ``market`` 指定时只在该交易所取数；为 ``None`` 时跨已知交易所自动发现
        （DCE/CZCE/CFFEX/INE 此前因 ``market=30`` 硬编码而无法获取）。
        """
        api = self._connect()
        markets = [market] if market is not None else list(_KNOWN_MARKETS)
        last_err: Optional[Exception] = None
        for m in markets:
            try:
                rows = self._fetch_bars_for_market(api, m, code, period, count)
            except HexDataError as exc:
                last_err = exc
                rows = []
            if rows:
                return self._bars_to_frame(rows, code)
        raise HexDataError(
            f"pytdx 未取得 {code} 任何 K 线（合约可能已退市/代码错误/市场不匹配）；"
            f"最后错误：{last_err}"
        )
    # ---- 主力连续拼接辅助 ------------------------------------------------
    def _select_main_contract(self, product: str) -> tuple[str, int]:
        """按实时持仓量(open interest)选主力合约，返回 ``(code, market)``。

        - 跨**全部**交易所枚举合约（不再硬编码 ``market=30``），按品种字母精确匹配
          （``m`` 不会误匹配 ``MA`` 甲醇等）；
        - 主力判定用实时报价 ``chicang``(持仓量) —— ``get_instrument_info`` 静态元数据
          **不含** open_interest，旧实现恒为 0 → 退化成「取首个候选」；
        - 返回所属 ``market`` 供取数使用，使 DCE/CZCE/CFFEX/INE 主力可正确拉取。
        """
        api = self._connect()
        try:
            total = api.get_instrument_count()
        except Exception as exc:
            raise HexDataError(f"pytdx 枚举合约失败（get_instrument_count）：{exc}") from exc
        prod = product.lower()
        candidates: list[tuple[str, int]] = []
        step = 80
        page = 0
        while page < total:
            try:
                infos = api.get_instrument_info(page, step)
            except Exception as exc:
                raise HexDataError(f"pytdx 枚举合约失败（get_instrument_info）：{exc}") from exc
            if not infos:
                break
            for info in infos:
                if int(info.get("category", -1)) != CATEGORY_FUTURE:
                    continue
                code = (info.get("code") or "").upper()
                # 品种精确匹配（前导字母），避免 m<->MA 等误匹配
                if code and _product_of_code(code) == prod:
                    candidates.append((code, int(info.get("market", MARKET_SHFE_INE))))
            page += step
        if not candidates:
            raise HexDataError(
                f"pytdx 未找到品种 '{product}' 的任何活跃合约（可能已休市/代码错误）"
            )
        # 按实时持仓量选主力（持仓最大者）
        best_code, best_market, best_oi = None, None, -1.0
        for code, market in candidates:
            try:
                q = api.get_instrument_quote(market, code)
            except Exception:
                q = None
            if not q:
                continue
            q0 = q[0] if isinstance(q, (list, tuple)) else q
            # 持仓量字段在实时报价中为 chicang（部分版本别名 open_interest）
            oi = float(q0.get("chicang") or q0.get("open_interest") or 0.0)
            if oi > best_oi:
                best_oi, best_code, best_market = oi, code, market
        if best_code is None:
            # 全部无实时报价时退回首个候选（避免卡死，行为接近旧逻辑但不保证主力）
            best_code, best_market = candidates[0]
        return best_code, best_market
    def _fetch_continuous(self, product: str, period: int, count: int) -> pd.DataFrame:
        """主力连续：选主力合约后取其近期 K 线。

        注：仅取「当前主力」近期历史；跨月连续需多合约拼接，由现有
        ``ContractStitcher``（``hexbroker/data/contract.py``）负责后向复权对齐。
        """
        main_code, main_market = self._select_main_contract(product)
        df = self._fetch_contract_bars(main_code, period, count, market=main_market)
        # 主力连续以品种字母作 symbol，便于与 ContractStitcher 衔接
        df = df.reset_index()
        df["symbol"] = product.lower()
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return df

    # ---- 映射为 BarFrame 契约 --------------------------------------------
    @staticmethod
    def _repair_ohlc(df: pd.DataFrame) -> pd.DataFrame:
        """免费源偶有脏数据：修复 OHLC 包络（low<=open,close<=high）并丢弃废 bar。

        - 包络破坏（open>high 等）：以四价极值重定 low/high，保证契约成立；
        - close<=0 的废 bar（无成交）：直接丢弃。
        """
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].astype(float)
        lo = df[["open", "high", "low", "close"]].min(axis=1)
        hi = df[["open", "high", "low", "close"]].max(axis=1)
        df["low"] = lo
        df["high"] = hi
        df = df[df["close"] > 0].copy()
        return df

    @staticmethod
    def _bars_to_frame(bars: list[dict], symbol: str) -> pd.DataFrame:
        """将 pytdx ``get_instrument_bars`` 返回的行映射为 BarFrame 单 symbol DataFrame。"""
        if not bars:
            raise HexDataError(f"pytdx 合约 {symbol} 返回空数据")
        records: list[dict] = []
        for b in bars:
            records.append(
                {
                    "symbol": symbol,
                    "datetime": pd.to_datetime(str(b["datetime"])),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    # Volume/Amount 已是最终值，禁止二次换算
                    "volume": float(b.get("volume", 0.0) or 0.0),
                    "amount": float(b.get("amount", 0.0) or 0.0),
                    # pytdx 日/频 K 线持仓量字段名为 position（非 open_interest），
                    # 旧实现读 open_interest 恒为 0 → 持仓量列全 0；兼容两种键名。
                    "open_interest": float(b.get("open_interest") or b.get("position") or 0.0),
                }
            )
        df = pd.DataFrame.from_records(records)
        # 脏数据修复（免费源偶有包络破坏/废 bar）
        df = PytdxSource._repair_ohlc(df)
        if df.empty:
            raise HexDataError(f"pytdx 合约 {symbol} 经清洗后无有效 bar")
        # tz-naive 本地时间，与 schema 约定一致
        df["datetime"] = df["datetime"].dt.tz_localize(None)
        # 不复权：raw_close == close；adj_close 初值等同 close（换月由 ContractStitcher 处理）
        df["raw_close"] = df["close"]
        df["adj_close"] = df["close"]
        df["limit_up"] = False
        df["limit_down"] = False
        df["is_rollover"] = False
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return df

    # ---- 公共取数接口 ----------------------------------------------------
    def fetch_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
        freq: str = "1d",
        count: Optional[int] = None,
        save: Optional[bool] = None,
    ) -> BarFrame:
        """拉取指定品种/区间/频率的 BarFrame（pytdx 公共行情服务器）。

        参数
        ----
        symbols : 符号列表，支持 ``SHFE.cu`` / ``cu0`` / ``CU2609`` 三种风格。
        start, end : 日期范围（左闭右闭），用于裁剪。
        freq : ``1d`` / ``60m`` 等，映射 pytdx period。
        count : 回拉根数（默认按 freq 给 1200/2000）。
        save : 是否落 Parquet（覆盖构造参数）。
        """
        if freq not in FREQ_TO_PERIOD:
            raise HexConfigError(f"pytdx 不支持 freq={freq!r}，可选 {list(FREQ_TO_PERIOD)}")
        if count is None:
            count = 1200 if freq == "1d" else 2000
        period = FREQ_TO_PERIOD[freq]

        frames: list[pd.DataFrame] = []
        try:
            for sym in symbols:
                kind, value = self._resolve_symbol(sym)
                if kind == "contract":
                    df = self._fetch_contract_bars(value, period, count)
                else:
                    df = self._fetch_continuous(value, period, count)
                frames.append(df)
                if self.rate_limit_sleep:
                    time.sleep(self.rate_limit_sleep)
        finally:
            self._disconnect()

        out = pd.concat(frames)
        out = self._clip_range(out, start, end)

        bf = BarFrame(df=out, freq=freq, source="pytdx")
        do_save = self.save if save is None else save
        if do_save:
            try:
                self.lake.save_processed(bf)
            except Exception as exc:  # 落盘失败不阻断取数，明确提示
                raise HexDataError(f"pytdx 落 Parquet 失败：{exc}") from exc
        return bf.validate()

    def health_check(self) -> bool:
        try:
            import pytdx  # noqa: F401

            return True
        except ImportError:
            return False
