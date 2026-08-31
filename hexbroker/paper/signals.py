"""信号引擎（§3.1 SignalEngine / D2 / §3.3）。

- 主源：v8 信号缓存（``artifacts/signals_cache18_grouped_v8.parquet``，含 ag0/rb0，**不含 c0**）。
- 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
  P0-3：阈值默认 **1 = 允许相邻交易日（自然日差 ≤1）**，跨周末/跨假期（自然日差 ≥2）即过期。

  ⚠️ **度量口径为「自然日差」而非「工作日差」**（P3-B，2026-08-31）：
  信号是**回溯性**的（对已有 bar 打分，不为未来外推），因此盘中最新信号日恒为
  **上一交易日**，`fd=0` 在盘中结构性不可达 → 阈值 `0` 等价于「禁止一切主源开仓」，
  属 P0-3 的过度矫正。改用自然日差后：正常隔夜=1、周五→周一=3、长假后≥2，
  可精确区分「信息衰减 1 天」与「跨周末/跨假期」，P0-3 的真实意图（拦 08-24 事故：
  周五信号周一用）完整保留（3 > 1 → 仍拦截）。
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


def _calendar_days(a: date, b: date) -> int:
    """两个日期之间的**自然日**差（P0-3 信息衰减度量口径）。

    ⛔ 口径说明（P3-B，2026-08-31）——**为什么是自然日而不是工作日**：

    新鲜度要度量的是「信号信息随时间衰减了多少」，而衰减按**自然时间**发生，
    不按交易所开关门计数。原实现用 ``np.busday_count``（工作日差）存在致命缺陷：

    ==========  ==================  ============  ============
    场景        自然日跨度          工作日差        自然日差
    ==========  ==================  ============  ============
    周一→周二   隔夜 1 天           **1**          **1**
    周五→周一   跨周末 3 天         **1**          **3**
    节前→节后   跨假期 4~10 天      **1**          **4~10**
    ==========  ==================  ============  ============

    工作日差把后两者都算成 1，**与前者不可区分** → 当年为拦「周五信号周一用」
    （2026-08-24 事故），只能把阈值压到 0，结果**连正常隔夜一起误杀**。
    而由于信号是回溯性的（盘中最新信号日恒为上一交易日），``fd=0`` 盘中不可达，
    阈值 0 实际等价于「日盘永不主源开仓」。

    改自然日差后，阈值取 1 即可精确命中：正常隔夜（=1）放行，
    跨周末（=3）与跨假期（≥2）拦截，P0-3 意图完整保留。
    """
    if a is None or b is None:
        return 10 ** 9
    return abs((b - a).days)


# 向后兼容别名：历史脚本/外部引用（health_check、QA 脚本）仍用旧名。
# ⚠️ 语义已变更为自然日差，勿按「工作日」字面理解。
_business_days = _calendar_days


class SignalEngine:
    """多信号源级联读取 + 新鲜度检测 + 技术指标兜底。

    - 信号源（按优先序）：主源（如 tail_ext，覆盖至最新）→ 兜底源（如 v8 生产基线）→ 技术指标。
    - 新鲜度检测：信号日距当前交易日的距离超过阈值 → 标记过期（有持仓仅风控 / 无持仓禁开+告警，§8.2）。
    - 技术指标兜底（c0 或全部缓存信号缺失时）：双均线 + ATR 通道（§4.3 决策建议）。

    Args:
        cache_path: 单信号缓存路径（向后兼容，等价 ``cache_paths=[cache_path]``）。
        cache_paths: 多源级联信号缓存路径（优先于 ``cache_path``）。
        freshness_threshold_days: 信号新鲜度阈值（**自然日差**，见 ``_calendar_days``）。
            **默认 1 = 允许相邻交易日**（P0-3 + P3-B 修正）：
            ``fd <= 1``（正常隔夜，如周二→周三）视为新鲜；
            ``fd >= 2``（跨周末如周五→周一 =3、跨假期 ≥2）即过期 → ``is_effective=False``
            → 有持仓仅风控 / 无持仓禁开（§8.2），由技术兜底接手。

            ⚠️ 阈值不再取 0：信号为回溯性（盘中最新信号日恒为上一交易日），
            ``fd=0`` 盘中不可达，阈值 0 等价于「日盘永不主源开仓」。
        fast_ma: 技术兜底快均线窗口。
        slow_ma: 技术兜底慢均线窗口。
        atr_window: 技术兜底 ATR 窗口。
        atr_mult: 技术兜底 ATR 通道倍数。
    """

    def __init__(
        self,
        cache_path: str | Path | None = None,
        cache_paths: list[str | Path] | None = None,
        freshness_threshold_days: int = 1,
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
        """信号新鲜度阈值（自然日差；1=允许相邻交易日，跨周末/假期过期）。

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
        """信号新鲜度（自然日数，见 ``_calendar_days``）。无信号返回超大值。"""
        if sig_ts is None:
            sig = self.latest_signal(symbol, asof)
            if sig is None:
                return 10 ** 9
            sig_ts = sig.ts
        a, b = _to_date(sig_ts), _to_date(asof)
        return _calendar_days(a, b)

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
        """技术指标兜底（双均线 + ATR 通道），仅作**降级方向提示**。

        返回 ``is_effective=False`` 的帧：技术指标只能给出方向（p_up），
        无法校准「预期日收益率」(exp_ret)，因此不构成模型验证过的 edge。
        配合 P0-3 隔夜过期「无持仓禁开」硬约束，兜底信号不会驱动新开仓
        （``RiskGate._intent`` 对 ``is_effective=False`` 返回 0），
        仅保留 p_up 供人工参考 / 有持仓时风控管理。数据不足返回 None。
        """
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
        # exp_ret 不提供真实期望收益估计：技术指标仅给出方向，无法校准「预期日收益率」。
        # 若把「上一日已实现涨跌幅」当 exp_ret 喂给成本门禁，会在「昨日跌+弱多」时误拦、
        # 「昨日涨+弱多」时误放，完全取决于历史噪音，与未来期望无关（审计 P1-1）。
        # 故 exp_ret 置中性 0.0，并令 is_effective=False（降级 substitute，不构成 edge）：
        # 配合 P0-3 隔夜过期「无持仓禁开」硬约束，不会驱动任何新开仓（审计 P1-2）。
        exp_ret = 0.0
        ts = bars.index[-1]
        if not isinstance(ts, datetime):
            ts = pd.Timestamp(ts).to_pydatetime()
        return SignalFrame(
            symbol=symbol,
            ts=ts,
            p_up=float(p_up),
            exp_ret=exp_ret,
            is_effective=False,
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
