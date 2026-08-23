"""信号引擎（§3.1 SignalEngine / D2 / §3.3）。

- 主源：v8 信号缓存（``artifacts/signals_cache18_grouped_v8.parquet``，含 ag0/rb0，**不含 c0**）。
- 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
- 技术指标兜底（c0 或信号缺失时）：双均线 + ATR 通道（§4.3 决策建议）。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from .types import SignalFrame

log = get_logger("PAPER")


def _to_date(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


def _business_days(a: date, b: date) -> int:
    """两个日期之间的工作日（交易日近似）数量。"""
    if a is None or b is None:
        return 10 ** 9
    d0, d1 = min(a, b), max(a, b)
    return int(np.busday_count(d0, d1))


class SignalEngine:
    """v8 信号读取 + 新鲜度检测 + 技术指标兜底。"""

    def __init__(
        self,
        cache_path: str | Path,
        freshness_threshold_days: int = 5,
        fast_ma: int = 5,
        slow_ma: int = 20,
        atr_window: int = 14,
        atr_mult: float = 1.5,
    ) -> None:
        self._cache_path = Path(cache_path)
        self._freshness_threshold = int(freshness_threshold_days)
        self._fast_ma = int(fast_ma)
        self._slow_ma = int(slow_ma)
        self._atr_window = int(atr_window)
        self._atr_mult = float(atr_mult)
        self._cache: pd.DataFrame = self._load_cache()

    def _load_cache(self) -> pd.DataFrame:
        """读取 v8 信号缓存并规范化列/类型；缺失时抛 FileNotFoundError（启动期致命）。"""
        if not self._cache_path.exists():
            raise FileNotFoundError(f"信号缓存缺失：{self._cache_path}")
        df = pd.read_parquet(self._cache_path)
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 主信号
    # ------------------------------------------------------------------
    def latest_signal(self, symbol: str, asof: Any = None) -> Optional[SignalFrame]:
        """取 ``symbol`` 在 ``asof`` 之前（含）最新的 v8 信号。

        - 无信号 → None（调用方走技术兜底 / 禁开新仓）。
        - 有信号但过期（freshness_days > 阈值）→ ``is_effective=False`` 且 source 标注。
        """
        asof_dt = pd.Timestamp(asof).tz_localize(None) if asof is not None else pd.Timestamp.now()
        sub = self._cache[self._cache["symbol"] == symbol]
        sub = sub[sub["ts"] <= asof_dt]
        if sub.empty:
            return None
        row = sub.iloc[-1]
        fd = self.freshness_days(symbol, asof_dt, row["ts"])
        effective = bool(row["is_effective"]) and fd <= self._freshness_threshold
        return SignalFrame(
            symbol=symbol,
            ts=pd.Timestamp(row["ts"]).to_pydatetime(),
            p_up=float(row["p_up"]),
            exp_ret=float(row["exp_ret"]),
            is_effective=effective,
            source="engine_a",
            freshness_days=fd,
        )

    def freshness_days(self, symbol: str, asof: Any, sig_ts: Any = None) -> int:
        """信号新鲜度（交易日数）。无信号返回超大值。"""
        if sig_ts is None:
            sig = self.latest_signal(symbol, asof)
            if sig is None:
                return 10 ** 9
            sig_ts = sig.ts
        a, b = _to_date(sig_ts), _to_date(asof)
        return _business_days(a, b)

    def has_symbol(self, symbol: str) -> bool:
        return symbol in set(self._cache["symbol"].unique())

    def cache_latest_ts(self, symbol: str) -> Optional[datetime]:
        sub = self._cache[self._cache["symbol"] == symbol]
        if sub.empty:
            return None
        return pd.Timestamp(sub["ts"].max()).to_pydatetime()

    # ------------------------------------------------------------------
    # 技术指标兜底（双均线 + ATR 通道）
    # ------------------------------------------------------------------
    def technical_fallback(self, symbol: str, bars: pd.DataFrame) -> Optional[SignalFrame]:
        """基于日线计算双均线/ATR 通道信号；数据不足返回 None。"""
        if bars is None or bars.empty:
            return None
        close = pd.to_numeric(bars["close"], errors="coerce").dropna()
        need = max(self._slow_ma + 1, self._atr_window + 2)
        if len(close) < need:
            return None
        high = pd.to_numeric(bars["high"], errors="coerce")
        low = pd.to_numeric(bars["low"], errors="coerce")
        fast = close.rolling(self._fast_ma).mean().iloc[-1]
        slow = close.rolling(self._slow_ma).mean().iloc[-1]
        tr = pd.concat(
            [
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(self._atr_window).mean().iloc[-1]
        if slow <= 0 or atr <= 0 or not np.isfinite(slow) or not np.isfinite(atr):
            return None
        last = close.iloc[-1]
        upper, lower = slow + self._atr_mult * atr, slow - self._atr_mult * atr
        if last > upper:
            p_up = 0.65
        elif last < lower:
            p_up = 0.35
        elif fast > slow:
            p_up = 0.55
        elif fast < slow:
            p_up = 0.45
        else:
            return None
        prev = close.iloc[-2]
        exp_ret = float((last / prev - 1.0) * 100.0) if prev > 0 else 0.0
        ts = bars.index[-1]
        if not isinstance(ts, datetime):
            ts = pd.Timestamp(ts).to_pydatetime()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=float(p_up),
            exp_ret=exp_ret,
            is_effective=True,
            source="technical",
            freshness_days=0,
        )

    # ------------------------------------------------------------------
    # 风控专用中性信号（有持仓但信号缺失时：仅风控管理）
    # ------------------------------------------------------------------
    @staticmethod
    def neutral_signal(symbol: str, asof: Any = None) -> SignalFrame:
        """中性信号：p_up=0.5、is_effective=False（不驱动新开仓，仅触发风控评估）。"""
        ts = pd.Timestamp(asof).to_pydatetime() if asof is not None else datetime.now()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=0.5,
            exp_ret=0.0,
            is_effective=False,
            source="risk_only",
            freshness_days=10 ** 9,
        )
