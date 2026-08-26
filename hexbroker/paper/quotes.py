"""实时行情客户端（§3.1 RealTimeQuoteClient / D3）。

- 主源：新浪 ``hq.sinajs.cn`` 实时报价（标准库 urllib，零新增依赖；实测可拉三品种）。
- K 线兜底：复用 ``SinaSource``（requests 懒加载；缺失/失败返回空 DataFrame，不阻塞主链路）。
- tdx 备选：接口占位（pytdx 网络不可达已实证，保留扩展点）。

新浪 nf_ 字段映射（实测 2026-08-22）：
  0 名称 | 1 时间(HHMMSS) | 2 最新价 | 3 卖价 | 4 买价 | 6 今开 | 7 最高 | 8 昨结 | 10 最低
  17 日期(YYYY-MM-DD)。字段解析失败时回退 price，保证主链路健壮。
"""

from __future__ import annotations

import re
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
    ) -> None:
        self._symbol_map = symbols or dict(SINA_REALTIME_CODE)
        self._reverse_map = {v: k for k, v in self._symbol_map.items()}
        self._url = url
        self._timeout = float(timeout)
        self._offline = offline
        self._offline_prices = offline_prices or {}

    # ------------------------------------------------------------------
    # 实时报价
    # ------------------------------------------------------------------
    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """批量拉取实时报价，返回 {symbol: Quote}。"""
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
                out[quote.symbol] = quote
        return out

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
        price = _f(fields, 2)
        if price <= 0:
            return None
        ts = self._parse_ts(fields)
        return Quote(
            symbol=symbol,
            ts=ts,
            price=price,
            open=_f(fields, 6) or price,
            high=_f(fields, 7) or price,
            low=_f(fields, 10) or price,
            pre_settle=_f(fields, 8) or price,
            timestamp=datetime.now(),
        )

    @staticmethod
    def _parse_ts(fields: list[str]) -> datetime:
        """日期(field17) + 时间(field1, HHMMSS) → datetime。"""
        try:
            d = datetime.strptime(fields[17], "%Y-%m-%d")
            t = fields[1]
            hh, mm, ss = int(t[0:2]), int(t[2:4]), int(t[4:6])
            return d.replace(hour=hh, minute=mm, second=ss)
        except Exception:
            return datetime.now()

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