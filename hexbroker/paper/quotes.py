"""实时行情客户端（§3.1 RealTimeQuoteClient / D3）。

- 主源：新浪 ``hq.sinajs.cn`` 实时报价（标准库 urllib，零新增依赖；实测可拉三品种）。
- K 线兜底：复用 ``SinaSource``（requests 懒加载；缺失/失败返回空 DataFrame，不阻塞主链路）。
- tdx 备选：接口占位（pytdx 网络不可达已实证，保留扩展点）。

新浪 nf_ 字段映射（标准内盘期货格式，2026-09-04 用官方日 K 三重验证）：
  0 名称 | 1 时间(HHMMSS) | 2 开盘价 | 3 最高价 | 4 最低价 | 5 昨收盘
  6 买价 | 7 卖价 | 8 **最新价** | 9 结算价 | 10 昨结算 | 11 买量 | 12 卖量
  13 持仓量 | 14 成交量 | 15 交易所 | 16 品种名 | 17 日期(YYYY-MM-DD)

⛔ P0-1（2026-09-04 修复）：原实现把 ``price`` 取成 field[2]（**开盘价**）。开盘价
日内恒定 → 盯市浮盈全天冻结（trades.log 实证：09-03 全天 equity 恒为 94483.56、
09-04 恒为 94573.56）。**最新价在 field[8]**；同理 open/high/low/pre_settle 原先
分别误取 field[6]买价 / field[7]卖价 / field[10]昨结算 / field[8]最新价，一并修正。
字段解析失败时回退 price，保证主链路健壮。
"""

from __future__ import annotations

import re
import time
import urllib.request
from datetime import datetime
from typing import Optional

import pandas as pd

from ..utils.logging import get_logger
from .types import Quote, SINA_BAR_CODE, SINA_REALTIME_CODE

log = get_logger("PAPER")

# 新浪内盘期货实时行情正则：var hq_str_nf_AG0="...";
_LINE_RE = re.compile(r'var hq_str_(\w+)="(.*)";')

_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# --------------------------------------------------------------------------- #
# P1-1 报价时效护栏常量（2026-09-04）
#
# 背景：P0-1 事故中「行情冻结」在日志上完全不可见——报价看着正常、解析成功、无
# 任何告警，只是价格从不变。``Quote.ts``（交易所行情时间）早就被解析出来，但全仓
# 从未与本机时间比较过。故补一层时效护栏：过期即判陈旧，调用方 fail-closed。
# --------------------------------------------------------------------------- #
#: 行情时间落后本机超过此秒数 → 判陈旧，本轮不用于撮合与盯市（退回兜底价）。
#: 取 120s：覆盖新浪推送间隔与网络抖动，又远小于 60s 轮询周期的整数倍，能抓住冻结。
QUOTE_MAX_STALENESS_SEC = 120.0
#: 同一品种时效告警的最小间隔（秒）。非交易时段与夜盘休市时报价时间天然会旧，
#: 无节流会每 60s 刷屏一次。
QUOTE_STALE_ALERT_THROTTLE_SEC = 600.0


def _f(fields: list[str], idx: int, default: float = 0.0) -> float:
    """安全取浮点字段。"""
    try:
        val = float(fields[idx])
        return val if val == val else default  # NaN 防御
    except (IndexError, ValueError, TypeError):
        return default


class RealTimeQuoteClient:
    """实时行情客户端（新浪主源 + SinaSource K 线兜底 + tdx 备选占位）。"""

    def __init__(
        self,
        symbols: Optional[dict[str, str]] = None,
        url: str = "https://hq.sinajs.cn/list=",
        timeout: float = 10.0,
        offline: bool = False,
        offline_prices: Optional[dict[str, float]] = None,
        max_staleness_sec: float = QUOTE_MAX_STALENESS_SEC,
        alert_throttle_sec: float = QUOTE_STALE_ALERT_THROTTLE_SEC,
    ) -> None:
        self._symbol_map = symbols or dict(SINA_REALTIME_CODE)
        self._reverse_map = {v: k for k, v in self._symbol_map.items()}
        self._url = url
        self._timeout = float(timeout)
        self._offline = offline
        self._offline_prices = offline_prices or {}
        # P1-1 时效护栏
        self._max_staleness_sec = float(max_staleness_sec)
        self._alert_throttle_sec = float(alert_throttle_sec)
        self._stale_alerted_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 实时报价
    # ------------------------------------------------------------------
    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """批量拉取实时报价，返回 {symbol: Quote}。

        P0-1：``price`` 取 field[8]（最新价）。原取 field[2]（开盘价）导致盯市
        浮盈日内冻结。
        P1-1：逐条过时效护栏（:meth:`_apply_staleness_guard`），陈旧的报价仍返回
        但置 ``stale=True``，由调用方决定是否可用于撮合/盯市。
        """
        if self._offline:
            return self._offline_quotes(symbols)
        codes = [self._symbol_map[s] for s in symbols if s in self._symbol_map]
        if not codes:
            return {}
        url = f"{self._url}{','.join(codes)}"
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("gbk", errors="replace")
        except Exception as exc:  # 网络/超时/解析失败：记日志，不抛出（单步失败不阻断整轮）
            log.error("实时行情拉取失败 url={} err={}", url, exc)
            return {}
        out: dict[str, Quote] = {}
        for line in raw.splitlines():
            quote = self._parse_line(line)
            if quote is not None:
                self._apply_staleness_guard(quote)
                out[quote.symbol] = quote
        return out

    def _apply_staleness_guard(self, quote: Quote) -> None:
        """P1-1 时效护栏：就地为报价打 ``stale`` 标记并节流告警。

        ``Quote.ts`` 是**交易所行情时间**（非接收时刻），故可直接与本机时间比。
        """
        lag = (datetime.now() - quote.ts).total_seconds()
        if lag <= self._max_staleness_sec:
            return
        quote.stale = True
        self._warn_stale_throttled(
            quote.symbol,
            f"行情陈旧 lag={lag:.0f}s > 阈值 {self._max_staleness_sec:.0f}s"
            f"（行情时间 {quote.ts:%Y-%m-%d %H:%M:%S}）→ 本轮不用于撮合与盯市，"
            f"改用成本价兜底",
        )

    def _warn_stale_throttled(self, symbol: str, detail: str) -> None:
        """同一品种按 ``_alert_throttle_sec`` 节流告警，避免非交易时段刷屏。"""
        now_mono = time.monotonic()
        last = self._stale_alerted_at.get(symbol)
        if last is not None and (now_mono - last) < self._alert_throttle_sec:
            return
        self._stale_alerted_at[symbol] = now_mono
        log.warning("行情时效 symbol={} {}", symbol, detail)

    def _parse_line(self, line: str) -> Optional[Quote]:
        """解析一行 ``var hq_str_nf_AG0="...";``。"""
        m = _LINE_RE.match(line.strip())
        if not m:
            return None
        code, payload = m.group(1), m.group(2)
        symbol = self._reverse_map.get(code)
        if symbol is None:
            return None
        fields = payload.split(",")
        # P0-1：最新价在 field[8]；field[2] 是开盘价（日内恒定，曾致盯市冻结）
        price = _f(fields, 8)
        if price <= 0:
            return None
        ts = self._parse_ts(fields)
        if ts is None:
            # R22：时间字段格式变更时不得让全品种退回成本价（那会是新的整体停摆
            # 模式），故按当前本地时间放行并节流告警，保证可用性优先。
            self._warn_stale_throttled(
                symbol, "行情时间字段不可解析，无法校验时效（按本地时间放行）"
            )
            ts = datetime.now()
        return Quote(
            symbol=symbol,
            ts=ts,
            price=price,
            open=_f(fields, 2) or price,      # 开盘价
            high=_f(fields, 3) or price,      # 最高价
            low=_f(fields, 4) or price,       # 最低价
            pre_settle=_f(fields, 10) or price,  # 昨结算
            timestamp=datetime.now(),
        )

    @staticmethod
    def _parse_ts(fields: list[str]) -> Optional[datetime]:
        """日期(field17) + 时间(field1, HHMMSS) → datetime；不可解析返回 ``None``。

        P1-1：原实现失败时返回 ``datetime.now()``，会让「时间不可解析」伪装成
        「行情刚刚更新」，时效护栏被彻底绕过。改为返回 ``None`` 由调用方显式处置。
        """
        try:
            d = datetime.strptime(fields[17], "%Y-%m-%d")
            t = fields[1]
            hh, mm, ss = int(t[0:2]), int(t[2:4]), int(t[4:6])
            return d.replace(hour=hh, minute=mm, second=ss)
        except Exception:
            return None

    def _offline_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """离线模式（冒烟/测试）：返回确定性固定行情。"""
        now = datetime.now()
        out: dict[str, Quote] = {}
        base = {"ag0": 8000.0, "rb0": 3000.0, "c0": 2265.0}
        for i, s in enumerate(symbols):
            price = self._offline_prices.get(s, base.get(s, 100.0 + i))
            out[s] = Quote(
                symbol=s,
                ts=now,
                price=price,
                open=price * 0.999,
                high=price * 1.01,
                low=price * 0.99,
                pre_settle=price,
                timestamp=datetime.now(),
            )
        return out

    # ------------------------------------------------------------------
    # K 线（技术兜底用）
    # ------------------------------------------------------------------
    def fetch_bars(self, symbol: str, freq: str = "1d", days: int = 120) -> pd.DataFrame:
        """拉取主力连续 K 线（复用 SinaSource；失败返回空 DataFrame）。

        返回列约定：open / high / low / close / volume（datetime 列或 index）。
        """
        try:
            from ..data.sources.sina_source import SinaSource

            source = SinaSource(save=False)
            code = SINA_BAR_CODE.get(symbol, symbol)
            end = datetime.now().strftime("%Y-%m-%d")
            bf = source.fetch_bars([code], start="2000-01-01", end=end, freq=freq, save=False)
            df = bf.df
            if isinstance(df.index, pd.MultiIndex):
                try:
                    df = df.xs(code, level="symbol")
                except KeyError:
                    df = df.droplevel("symbol")
            df = df.reset_index()
            if "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"])
                df = df.set_index("datetime")
            elif "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            for col in ("open", "high", "low", "close"):
                if col not in df.columns:
                    return pd.DataFrame()
            return df.tail(int(days))
        except Exception as exc:  # requests 缺失 / 网络失败 / 解析异常
            log.warning("K 线兜底拉取失败 symbol={} freq={} err={}", symbol, freq, exc)
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # tdx 备选（占位；pytdx 网络不可达已实证，D3）
    # ------------------------------------------------------------------
    def fetch_quotes_tdx(self, symbols: list[str]) -> dict[str, Quote]:
        """tdx 备选通道。当前返回空（不可达），保留扩展点。"""
        log.warning("tdx 备选通道未启用（pytdx 网络不可达），symbols={}", symbols)
        return {}