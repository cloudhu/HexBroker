"""信号引擎（§3.1 SignalEngine / D2 / §3.3）。

- 主源：v8 信号缓存（``artifacts/signals_cache18_grouped_v8.parquet``，含 ag0/rb0，**不含 c0**）。
- 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
  P0-3：阈值默认 **0 = 隔夜过期**，仅同一交易日（fd=0）的信号可驱动开仓。
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
    """多信号源级联读取 + 新鲜度检测 + 技术指标兜底。

    - 信号源（按优先序）：主源（如 tail_ext，覆盖至最新）→ 兜底源（如 v8 生产基线）→ 技术指标。
    - 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
    - 技术指标兜底（c0 或全部缓存信号缺失时）：双均线 + ATR 通道（§4.3 决策建议）。

    Args:
        cache_path: 单信号缓存路径（向后兼容，等价 ``cache_paths=[cache_path]``）。
        cache_paths: 多源级联信号缓存路径（优先于 ``cache_path``）。
        freshness_threshold_days: 信号新鲜度阈值（工作日/交易日近似差）。
            **默认 0 = 隔夜过期**（P0-3）：仅 ``fd == 0``（同一交易日）的信号视为新鲜，
            ``fd >= 1``（隔夜、含周五信号周一用）即过期 → ``is_effective=False``
            → 有持仓仅风控 / 无持仓禁开（§8.2），由技术兜底接手。
        fast_ma: 技术兜底快均线窗口。
        slow_ma: 技术兜底慢均线窗口。
        atr_window: 技术兜底 ATR 窗口。
        atr_mult: 技术兜底 ATR 通道倍数。
    """

    def __init__(
        self,
        cache_path: str | Path | None = None,
        cache_paths: list[str | Path] | None = None,
        freshness_threshold_days: int = 0,
        fast_ma: int = 5,
        slow_ma: int = 20,
        atr_window: int = 14,
        atr_mult: float = 1.5,
    ) -> None:
        # 多源级联：cache_path 单参数向后兼容（等价 cache_paths=[cache_path]）；显式 cache_paths 优先
        if cache_paths:
            self._paths = [Path(p) for p in cache_paths]
        elif cache_path:
            self._paths = [Path(cache_path)]
        else:
            raise ValueError("SignalEngine 至少需要一个信号缓存路径")
        self._freshness_threshold = int(freshness_threshold_days)
        self._fast_ma = int(fast_ma)
        self._slow_ma = int(slow_ma)
        self._atr_window = int(atr_window)
        self._atr_mult = float(atr_mult)
        self._caches: list[pd.DataFrame] = [self._load_cache(p) for p in self._paths]

    @property
    def freshness_threshold(self) -> int:
        """信号新鲜度阈值（交易日；0=隔夜过期，仅当天信号有效）。

        供调用方（调度器运行时告警 / 健康自检）判定信号是否陈旧，避免各处重复读配置。
        """
        return self._freshness_threshold

    def _load_cache(self, cache_path: Path) -> pd.DataFrame:
        """读取信号缓存并规范化列/类型；缺失时抛 FileNotFoundError（启动期致命）。"""
        if not cache_path.exists():
            raise FileNotFoundError(f"信号缓存缺失：{cache_path}")
        df = pd.read_parquet(cache_path)
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 主信号（多源级联）
    # ------------------------------------------------------------------
    def latest_signal(self, symbol: str, asof: Any = None) -> Optional[SignalFrame]:
        """按优先序取 ``symbol`` 在 ``asof`` 之前（含）最新的信号。

        - 全部源无信号 → None（调用方走技术兜底 / 禁开新仓）。
        - 主源新鲜（freshness_days <= 阈值）→ 返回主源。
        - 主源过期但兜底源有**更新**信号 → 返回兜底源信号（source 标注 engine_a_fbN）。
        - 兜底源也过期/无更新 → 返回主源（维持原语义，is_effective 按新鲜度判定）。
        - ``source`` 标注信号源（engine_a=主源 / engine_a_fbN=第 N 兜底源），供审计。
        """
        asof_dt = pd.Timestamp(asof).tz_localize(None) if asof is not None else pd.Timestamp.now()
        # 收集每个源在 asof 前最新的信号
        rows: list[tuple[int, pd.Series, int]] = []
        for i, df in enumerate(self._caches):
            sub = df[df["symbol"] == symbol]
            sub = sub[sub["ts"] <= asof_dt]
            if sub.empty:
                continue
            row = sub.iloc[-1]
            fd = self.freshness_days(symbol, asof_dt, row["ts"])
            rows.append((i, row, fd))
        if not rows:
            return None

        # 主源
        i0, row0, fd0 = rows[0]
        if fd0 <= self._freshness_threshold:
            return self._build_frame(symbol, row0, i0, fd0)

        # 主源过期：找兜底源中「比主源更新」的信号（取最新者）
        best: Optional[tuple[int, pd.Series, int]] = None
        ts0 = pd.Timestamp(row0["ts"])
        for i, row, fd in rows[1:]:
            if pd.Timestamp(row["ts"]) > ts0:
                if best is None or pd.Timestamp(row["ts"]) > pd.Timestamp(best[1]["ts"]):
                    best = (i, row, fd)
        if best is not None:
            i, row, fd = best
            return self._build_frame(symbol, row, i, fd)

        # 兜底源也过期/无更新 → 返回主源（原语义）
        return self._build_frame(symbol, row0, i0, fd0)

    def _build_frame(self, symbol: str, row: pd.Series, source_idx: int, fd: int) -> SignalFrame:
        """由缓存行构造 SignalFrame（freshness 过期 → is_effective=False）。"""
        effective = bool(row["is_effective"]) and fd <= self._freshness_threshold
        return SignalFrame(
            symbol=symbol,
            ts=pd.Timestamp(row["ts"]).to_pydatetime(),
            p_up=float(row["p_up"]),
            exp_ret=float(row["exp_ret"]),
            is_effective=effective,
            source="engine_a" if source_idx == 0 else f"engine_a_fb{source_idx}",
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
        return any(symbol in set(df["symbol"].unique()) for df in self._caches)

    def cache_latest_ts(self, symbol: str) -> Optional[datetime]:
        """跨全部源取最新信号时间戳（取所有源中的最大值）。"""
        latest: Optional[pd.Timestamp] = None
        for df in self._caches:
            sub = df[df["symbol"] == symbol]
            if sub.empty:
                continue
            ts = pd.Timestamp(sub["ts"].max())
            if latest is None or ts > latest:
                latest = ts
        return latest.to_pydatetime() if latest is not None else None

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
