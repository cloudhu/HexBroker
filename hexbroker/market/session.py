"""交易时段 / 交易日标签共享模块（§3 / P1-9）。

复用 ``hexbroker.data.calendar.Session``（已支持 crosses_midnight），并提供纯函数
``day_label``（原 paper ``TradingSession.day_label`` 的共享实现）。paper ``TradingSession``
委托本模块，消除 ``data.calendar`` 与 ``paper/sessions`` 的重复实现。

时间口径：全系统 tz-naive；夜盘（>= night_boundary，默认 21:00）归属下一交易日。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional, cast

from ..data.calendar import Session

# 共享模块导出：Session 复用 data.calendar；day_label 为共享交易日标签实现
__all__ = [
    "day_label",
    "Session",
    "CACHE_REWRITE_BLOCK_SESSIONS",
    "is_cache_rewrite_blocked",
    "format_cache_rewrite_block_banner",
]


def _to_date(ts: Any) -> Optional[date]:
    """把时间戳归一为 date（date / datetime / pd.Timestamp / ISO 字符串）。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return cast(date, ts.date())
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


def _is_trading_day(d: date, holidays: Optional[set[date]]) -> bool:
    """是否为交易日：周一至周五且不在节假日表内。"""
    return d.weekday() < 5 and d not in (holidays or set())


def _pd_time(ts: Any) -> time:
    """时间戳 → time（兼容 pd.Timestamp）。"""
    if isinstance(ts, datetime):
        return ts.time()
    try:
        import pandas as pd

        return cast(time, pd.Timestamp(ts).time())
    except Exception:
        return datetime.fromisoformat(str(ts)).time()


def day_label(
    ts: Any,
    night_boundary: time,
    holidays: Optional[set[date]],
) -> Optional[date]:
    """交易日标签：夜盘（>= night_boundary）归属下一交易日，跳过周末/节假日。

    与 paper ``TradingSession.day_label`` 行为一致（委托此处实现，消除重复）。
    """
    d = _to_date(ts)
    if d is None:
        return None
    t = _pd_time(ts)
    if t >= night_boundary:
        d = d + timedelta(days=1)
    while not _is_trading_day(d, holidays):
        d = d + timedelta(days=1)
    return d


# ===========================================================================
# P1-2（2026-09-01）生产信号缓存重写禁区
# ===========================================================================
# 事故背景
# --------
# 2026-09-01 **09:10:12**（日盘交易时段内）生产信号缓存
# ``artifacts/signals_cache18_grouped_v8_tail_ext.parquet`` 被重写；而模拟盘进程
# **08:58** 启动时加载的是旧值（rb0 p_up 0.8667 / exp_ret 0.4480），重写后盘上值为
# p_up 0.999999 / exp_ret 1.639979（**3.7 倍跳变**）。后果：同一交易日存在两套信号，
# 13:25 窗口重启后全部开仓/成本门禁结论被静默翻转。
#
# 取证补充（同等危险，尚未发生）：20:30 夜盘刷新自动化 = 拉取（tqsdk 18 品种）
# + ``p22_tail_ext --force`` 全量重训约 8 分钟；而 **rb0 夜盘 21:00 开盘**、
# 模拟盘 **20:55** 已启动。只要该链路稍有超时，落盘就落在夜盘开始之后 —— 与
# 09-01 事故同构。**故禁区必须由写入时刻裁定，而不是由任务启动时刻裁定。**
#
# 禁区定义（按模拟盘进程实际启动时刻 08:55 / 13:25 / 20:55 各前移 5 分钟）
# ---------------------------------------------------------------------
#   ⛔ 禁止重写：08:50–11:30、13:20–15:00、20:50–02:30（跨午夜，覆盖 ag0 21:00–02:30）
#   ✅ 允许刷新：02:30–08:50、11:30–13:20（午休补刷窗口）、15:00–20:50（收盘后）、
#               以及全部非交易日（周末/节假日）
#
# ⛔ 误拦 vs 漏放不对称：误拦代价 = 一次补刷需人工加 ``--force-in-session``；
#    漏放代价 = 盘中信号被静默重写、当日全部交易结论翻转。故一律取保守取向。
# ===========================================================================
CACHE_REWRITE_BLOCK_SESSIONS = (
    # 日盘上午：09:00–10:15 / 10:30–11:30，含中间 10:15–10:30 小节休市（进程仍在运行）
    Session(time(8, 50), time(11, 30)),
    # 日盘下午：13:30–15:00（13:20 起算，覆盖 13:25 二次启动窗口）
    Session(time(13, 20), time(15, 0)),
    # 夜盘：20:50 起算（覆盖 20:55 启动），至 02:30 覆盖贵金属全时段
    Session(time(20, 50), time(2, 30), crosses_midnight=True),
)

# 跨午夜归属阈值：凌晨 04:00 之前的时刻视为**前一自然日**夜盘的延续。
# 与 day_label（夜盘 >= 21:00 归属下一交易日）同源口径：周五 21:00–周六 02:30 这段
# 夜盘归周五这个交易日，若按周六判定（非交易日）就会被漏放。
_NIGHT_TAIL_BOUNDARY = time(4, 0)


def is_cache_rewrite_blocked(
    ts: Any,
    holidays: Optional[set[date]] = None,
    sessions: Optional[Iterable[Session]] = None,
) -> bool:
    """P1-2：``ts`` 是否落在「生产信号缓存禁止重写」窗口内。

    两步判定，任一步为否即**放行**：

    1. **归属交易日**是否为交易日（周末 / 节假日 → 直接放行，禁区只在交易日生效）；
    2. 时刻是否落入 :data:`CACHE_REWRITE_BLOCK_SESSIONS` 任一区间。

    ⚠️ **节假日表默认为空**（不引入外部依赖）→ 节假日的盘中时刻会被**误拦**。
    这是刻意的保守取向：误拦只需人工加 ``--force-in-session``，漏放则会静默
    重写盘中信号并翻转当日全部交易结论，两者代价严重不对称。调用方可从
    ``configs/paper.yaml → paper.holidays_2026`` 传入节假日表以消除该误拦。

    ``ts`` 无法解析时返回 ``False``（放行）——禁区不得因解析失败而扩大，
    解析失败应由上游显式报错而不是靠禁区兜底。
    """
    d = _to_date(ts)
    if d is None:
        return False
    t = _pd_time(ts)
    owning = d - timedelta(days=1) if t < _NIGHT_TAIL_BOUNDARY else d
    if not _is_trading_day(owning, holidays):
        return False
    return any(t in s for s in (sessions or CACHE_REWRITE_BLOCK_SESSIONS))


def format_cache_rewrite_block_banner(ts: Any, cache_path: str = "", extra: str = "") -> str:
    """P1-2 禁写横幅：说清「为什么拦 / 什么时候可以刷 / 如何强制放行」。"""
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
    lines = [
        "=" * 72,
        "⛔ 生产信号缓存重写已被拦截（P1-2 交易时段禁写）",
        "=" * 72,
        f"  当前时刻：{stamp}",
    ]
    if cache_path:
        lines.append(f"  目标缓存：{cache_path}")
    lines += [
        "",
        "  禁区窗口（模拟盘进程运行时段，各前移 5 分钟为保守边界）：",
        "    ⛔ 08:50–11:30   日盘上午（含 10:15–10:30 小节休市）",
        "    ⛔ 13:20–15:00   日盘下午",
        "    ⛔ 20:50–02:30   夜盘（跨午夜，覆盖 ag0 21:00–02:30）",
        "",
        "  允许的刷新窗口：",
        "    ✅ 02:30–08:50   盘前（08:00 自动化正常落在此段）",
        "    ✅ 11:30–13:20   午休补刷窗口",
        "    ✅ 15:00–20:50   收盘后（20:30 自动化正常落在此段）",
        "    ✅ 全部非交易日（周末 / 节假日）",
        "",
        "  为何拦截：盘中重写会让「进程已加载的旧信号」与「盘上重写后的新信号」",
        "  并存，下一次窗口重启（13:25 / 20:55）将静默翻转全部开仓与成本门禁结论。",
        "  2026-09-01 09:10:12 即因此造成 rb0 p_up 0.8667 → 0.999999 的 3.7 倍跳变。",
    ]
    if extra:
        lines += ["", f"  {extra}"]
    lines += [
        "",
        "  ⚠️ 确需盘中重建（如事故恢复）时，显式加 --force-in-session 人工放行，",
        "     并须同步重启模拟盘进程，使内存信号与盘上缓存一致。",
        "=" * 72,
    ]
    return "\n".join(lines)
