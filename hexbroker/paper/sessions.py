"""交易时段判定（§3.1 TradingSession / §8.5）。

复用 ``hexbroker.data.calendar.FuturesCalendar.Session``（已支持 crosses_midnight），
按品种配置日盘/夜盘时段 + 内置 2026 节假日表（可配置）+ 开盘延迟（Q6 批复）。

时间口径（§7.2）：全系统 Asia/Shanghai tz-naive；**夜盘（>= night_boundary，默认
21:00）归属下一交易日**（``day_label`` 统一处理）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from ..data.calendar import FuturesCalendar, Session


def _to_date(ts: Any) -> Optional[date]:
    """把时间戳归一为 date（date / datetime / pd.Timestamp / ISO 字符串）。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):  # pd.Timestamp 等
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


def parse_sessions(day: list[list[str]], night: list[list[str]]) -> list[Session]:
    """把 yaml 的 [[HH:MM,HH:MM], ...] 解析为 Session 列表。

    夜盘是否跨日由起止时间推断（start > end → crosses_midnight=True，
    如 ag 21:00-02:30；rb 21:00-23:00 不跨日）。
    """
    sessions: list[Session] = []
    for start_s, end_s in day or []:
        sessions.append(Session(_parse_time(start_s), _parse_time(end_s), crosses_midnight=False))
    for start_s, end_s in night or []:
        start_t, end_t = _parse_time(start_s), _parse_time(end_s)
        sessions.append(Session(start_t, end_t, crosses_midnight=start_t > end_t))
    return sessions


def _parse_time(s: Any) -> time:
    """HH:MM 字符串 → time；兼容 OmegaConf 将 10:15 解析为 sexagesimal 整数 615。"""
    if isinstance(s, (int, float)):
        total = int(s)
        hh, mm = divmod(total, 60)
        return time(hh % 24, mm % 60)
    return datetime.strptime(str(s), "%H:%M").time()


@dataclass
class TradingSession:
    """按品种时段 + 节假日 + 开盘延迟 + 夜盘跨日归属 的交易日历封装。"""

    symbol_sessions: dict[str, list[Session]]
    holidays: set[date] = None  # type: ignore[assignment]
    night_boundary: time = time(21, 0)

    def __post_init__(self) -> None:
        if self.holidays is None:
            self.holidays = set()
        self._calendar = FuturesCalendar(self.symbol_sessions or {})

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, paper_cfg: Any) -> "TradingSession":
        """从 ``configs/paper.yaml`` 的 ``paper.symbols`` 构造。"""
        symbols_cfg = paper_cfg.symbols
        symbol_sessions: dict[str, list[Session]] = {}
        for sym, cfg in symbols_cfg.items():
            sessions = cfg.get("sessions", {})
            symbol_sessions[sym] = parse_sessions(
                day=list(sessions.get("day", []) or []),
                night=list(sessions.get("night", []) or []),
            )
        holidays = {datetime.strptime(s, "%Y-%m-%d").date() for s in paper_cfg.get("holidays_2026", [])}
        return cls(symbol_sessions=symbol_sessions, holidays=holidays)

    # ------------------------------------------------------------------
    # 核心判定
    # ------------------------------------------------------------------
    def is_trading_day(self, ts: Any) -> bool:
        """是否为交易日：周一至周五且不在节假日表内。"""
        d = _to_date(ts)
        if d is None:
            return False
        return d.weekday() < 5 and d not in self.holidays

    def is_tradable(self, symbol: str, ts: Any) -> bool:
        """该品种在 ``ts`` 是否处于交易时段（交易日 + 时段内）。"""
        if not self.is_trading_day(ts):
            return False
        t = pd_time(ts)
        return any(t in s for s in self.sessions(symbol))

    def is_open_delay_passed(self, symbol: str, ts: Any, delay_min: int) -> bool:
        """开盘延迟是否已过（Q6：跳过集合竞价，开盘后 N 分钟进入）。

        仅对「当日第一个开盘」（日盘首节 / 夜盘首节）施加延迟；
        盘中节间休息（10:30 / 13:30 复牌）不施加。
        """
        if delay_min is None or delay_min <= 0:
            return True
        t = pd_time(ts)
        sessions = self.sessions(symbol)
        for s in sessions:
            if t in s:
                if s.start in self._opening_starts(sessions):
                    base_day = _to_date(ts)
                    # P1-1：跨日夜盘（21:00-02:30）凌晨段 ts 的 date 是次日，基准日回退一天，
                    # 使 00:00-02:30 的延迟判断相对「当晚 21:00」计算（否则恒判为延迟中）。
                    if s.crosses_midnight and t <= s.end:
                        base_day = base_day - timedelta(days=1) if base_day is not None else None
                    start_dt = datetime.combine(base_day, s.start)
                    if ts < start_dt + timedelta(minutes=int(delay_min)):
                        return False
                return True
        return False

    def day_label(self, ts: Any) -> Optional[date]:
        """交易日标签：夜盘（>= night_boundary）归属下一交易日，跳过周末/节假日。"""
        d = _to_date(ts)
        if d is None:
            return None
        t = pd_time(ts)
        if t >= self.night_boundary:
            d = d + timedelta(days=1)
        while not self.is_trading_day(d):
            d = d + timedelta(days=1)
        return d

    def day_close_threshold(self, buffer_min: int = 10) -> time:
        """全部品种日盘收盘时刻的最大值 + 缓冲（P1-3 收盘复盘触发阈值）。

        只取日盘时段（start < night_boundary）：夜盘（含 21:00-23:00 非跨日）归属
        下一交易日，不计入当日收盘；无日盘时段时兜底 15:00（通用商品期货时段）。
        """
        close_times: list[time] = []
        for sessions in self.symbol_sessions.values():
            for s in sessions:
                if s.start < self.night_boundary:
                    close_times.append(s.end)
        latest = max(close_times) if close_times else time(15, 0)
        total = latest.hour * 60 + latest.minute + int(buffer_min)
        hh, mm = divmod(total, 60)
        return time(hh % 24, mm % 60)

    def day_closed(self, ts: Any, buffer_min: int = 10) -> Optional[date]:
        """当前时刻所属交易日的日盘是否已收盘（收盘时刻 + 缓冲已过）。

        返回该交易日（供收盘复盘触发，P1-3）；以下情况返回 None：
        - 未到收盘时刻（含盘中/夜盘凌晨段）；
        - 夜盘（>= night_boundary）day_label 已归属下一交易日，日盘尚未结束；
        - 周末/节假日 day_label 跳到下一交易日（不误触发）。
        """
        d = _to_date(ts)
        if d is None:
            return None
        trading_day = self.day_label(ts)
        if trading_day is None or trading_day != d:
            return None
        t = pd_time(ts)
        if t >= self.day_close_threshold(buffer_min):
            return trading_day
        return None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def sessions(self, symbol: str) -> list[Session]:
        return self._calendar.sessions(symbol)

    @staticmethod
    def _opening_starts(sessions: list[Session]) -> set[time]:
        """开盘时刻集合：日盘最早一节 + 夜盘最早一节。"""
        opens: set[time] = set()
        day_starts = [s.start for s in sessions if not s.crosses_midnight]
        night_starts = [s.start for s in sessions if s.crosses_midnight]
        if day_starts:
            opens.add(min(day_starts))
        if night_starts:
            opens.add(min(night_starts))
        return opens

    def next_trading_day(self, d: date) -> date:
        """返回 ``d`` 之后的第一个交易日。"""
        nd = d + timedelta(days=1)
        while not self.is_trading_day(nd):
            nd = nd + timedelta(days=1)
        return nd


def pd_time(ts: Any) -> time:
    """时间戳 → time（兼容 pd.Timestamp）。"""
    if isinstance(ts, datetime):
        return ts.time()
    try:
        import pandas as pd

        return pd.Timestamp(ts).time()
    except Exception:
        return datetime.fromisoformat(str(ts)).time()
