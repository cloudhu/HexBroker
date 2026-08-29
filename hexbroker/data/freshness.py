"""取数结果门禁：空结果检测 + 新鲜度检测（P0-0 / P0-7）。

背景（2026-08-28 停摆事故）
--------------------------
事故故障模式不是"取数报错"，而是"取数**成功**但结果不可用"：

1. 数据源停更（或连接器未接入），最新 bar 停留在数日/数年前；
2. 调用方请求 ``[start, end]`` 的新日期窗口；
3. ``_clip_range`` 把数据裁成 0 行，或裁出几行远早于 ``end`` 的旧 bar；
4. 空/陈旧的 BarFrame 通过 ``validate_bars`` 全部既有校验项后**静默返回成功**；
5. 下游把"刷新成功"写进状态，实际信号陈旧 → 系统误判可交易。

本模块把第 3-4 步变成**显式失败**，并与"网络失败 / 配额耗尽"用不同异常类型区分，
便于上层编排做差异化降级（换备源 / 等待配额重置 / 直接告警）。

设计原则
--------
* **只判不改**：本模块不拉取、不重试、不补数据，只做判定与抛错。
* **历史回填豁免**：当 ``end`` 明显早于"当前日期 - 容差"时，认定为历史回填，
  不做新鲜度判定（历史窗口本就不可能"新鲜"），仅做空结果判定。
* **可注入当前日期**：``today`` 参数便于单测确定性，禁止在实现内部直接取 ``now()``
  而不留注入口（否则测试不可复现）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from .. import HexEmptyDataError, HexStaleDataError

__all__ = [
    "DEFAULT_MAX_STALE_DAYS",
    "latest_bar_date",
    "assert_nonempty",
    "assert_fresh",
    "check_fetch_result",
]

#: 默认陈旧容忍天数（日历日）。覆盖周末 + 最长 3 天连休（元旦/春节邻近日）。
DEFAULT_MAX_STALE_DAYS = 5


def _to_date(value) -> date:
    """把 str / date / datetime / Timestamp / numpy.datetime64 统一成 ``date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError(f"无法解析为日期: {value!r}")
    return ts.date()


def latest_bar_date(df: pd.DataFrame) -> date | None:
    """返回 DataFrame 中最新 bar 日期；无数据返回 ``None``。

    兼容两级 MultiIndex(symbol, datetime) 与带 ``datetime`` 列的普通 DataFrame。
    """
    if df is None or len(df) == 0:
        return None
    if isinstance(df.index, pd.MultiIndex) and df.index.nlevels >= 2:
        dts = df.index.get_level_values("datetime")
    elif "datetime" in df.columns:
        dts = pd.to_datetime(df["datetime"])
    else:
        dts = pd.to_datetime(df.index)
    return _to_date(pd.Timestamp(max(dts)).date())


def assert_nonempty(df: pd.DataFrame, *, source: str = "", symbols: object = None) -> None:
    """结果为空 → 抛 ``HexEmptyDataError``。"""
    if df is None or len(df) == 0:
        sym_txt = ""
        if symbols:
            syms = list(symbols)[:5]
            sym_txt = f"，请求品种 {syms}{'...' if len(list(symbols)) > 5 else ''}"
        raise HexEmptyDataError(
            f"数据源 {source or 'unknown'} 取数结果为 0 行{sym_txt}。"
            "可能原因：请求区间无交易日、数据源已停更、或品种代码不匹配。",
            source=source,
        )


def assert_fresh(
    df: pd.DataFrame,
    end,
    *,
    source: str = "",
    symbols: object = None,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    today: date | None = None,
) -> date:
    """校验最新 bar 日期是否足够接近请求结束日 ``end``。

    返回实际最新日期。陈旧则抛 ``HexStaleDataError``。

    豁免规则：当 ``end < today - max_stale_days`` 时视为历史回填，直接返回不做判定。
    """
    latest = latest_bar_date(df)
    if latest is None:  # 空结果归 assert_nonempty 管，这里不重复抛
        return latest  # type: ignore[return-value]

    end_d = _to_date(end)
    ref = today or date.today()

    # 历史回填豁免：请求窗口整体落在容忍窗口之外
    if end_d < ref - timedelta(days=max_stale_days):
        return latest

    earliest_ok = end_d - timedelta(days=max_stale_days)
    if latest < earliest_ok:
        sym_txt = ""
        if symbols:
            syms = list(symbols)[:5]
            sym_txt = f"（品种 {syms}{'...' if len(list(symbols)) > 5 else ''}）"
        raise HexStaleDataError(
            f"数据源 {source or 'unknown'} 数据陈旧{sym_txt}："
            f"最新 bar 日期 {latest.isoformat()}，请求结束日 {end_d.isoformat()}，"
            f"容忍 {max_stale_days} 日历日（要求不早于 {earliest_ok.isoformat()}）。"
            "可能原因：数据源已停更或上游未发布新数据 —— 不得当作刷新成功。",
            source=source,
            latest=latest.isoformat(),
            expected=end_d.isoformat(),
        )
    return latest


def check_fetch_result(
    df: pd.DataFrame,
    end,
    *,
    source: str = "",
    symbols: object = None,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    today: date | None = None,
    check_freshness: bool = True,
) -> date | None:
    """门禁总入口：先查空、再查陈旧。返回最新 bar 日期（空则在抛错前为 None）。"""
    assert_nonempty(df, source=source, symbols=symbols)
    if not check_freshness:
        return latest_bar_date(df)
    return assert_fresh(
        df,
        end,
        source=source,
        symbols=symbols,
        max_stale_days=max_stale_days,
        today=today,
    )
