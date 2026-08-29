"""数据契约（§8.4）：``BarFrame`` 与 ``validate_bars``。

标准列名（小写）：``open, high, low, close, volume, amount, open_interest``；
复权列 ``adj_close``；原始列 ``raw_close``；标记列 ``limit_up, limit_down, is_rollover``。
索引：``MultiIndex(symbol, datetime)``，``datetime`` 为 bar **结束时刻**，tz-naive 本地时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .. import (
    HexDataError,
    HexEmptyDataError,
)
from ..constants import OHLCV_COLS

REQUIRED_COLS = OHLCV_COLS + ["adj_close", "raw_close"]
OPTIONAL_COLS = ["limit_up", "limit_down", "is_rollover", "adj_factor"]


@dataclass
class BarFrame:
    """K 线数据帧（数据层跨模块传递的核心契约）。"""

    df: pd.DataFrame
    freq: str = "1d"
    source: str = "unknown"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.df.index, pd.MultiIndex):
            raise HexDataError("BarFrame.df 必须为 MultiIndex(symbol, datetime)")
        if self.df.index.nlevels != 2:
            raise HexDataError("BarFrame.df 索引必须是两级 (symbol, datetime)")

    # ---- 便捷访问 ----
    @property
    def symbols(self) -> list[str]:
        return sorted(self.df.index.get_level_values("symbol").unique().tolist())

    @property
    def length(self) -> int:
        return len(self.df)

    def validate(self, allow_empty: bool = False) -> "BarFrame":
        """校验数据契约，失败抛 ``HexDataError``。

        ``allow_empty``：是否容忍 0 行。默认 **False** —— 0 行一律视为异常。

        背景（2026-08-28 停摆事故根因）：空 DataFrame 能通过 ``validate_bars``
        的全部既有校验项（无重复索引、列齐全、groupby 无分组故单调性检查空转、
        价格比较对空 Series 恒真），导致"数据源停更 → 请求新日期窗口 → 被裁剪成
        0 行 → 静默返回成功"整条链路被伪装成刷新成功。此处必须由默认行为拦截。
        """
        validate_bars(self.df, freq=self.freq, allow_empty=allow_empty)
        return self

    def by_symbol(self, symbol: str) -> pd.DataFrame:
        """返回某品种的 DataFrame，保留 MultiIndex(symbol, datetime) 两级索引。

        注意：使用列表选择 ``loc[[symbol]]``，标量 ``loc[symbol]`` 会丢弃该索引层级。
        """
        return self.df.loc[[symbol]].sort_index()

    def to_frame(self) -> pd.DataFrame:
        return self.df


def validate_bars(df: pd.DataFrame, freq: str = "1d", allow_empty: bool = False) -> bool:
    """校验 BarFrame 的 DataFrame 是否符合契约。

    校验项：
    0. 非空（``allow_empty=False`` 时，0 行抛 ``HexEmptyDataError``）
    1. 两级 MultiIndex (symbol, datetime)
    2. 必须列齐全；索引单调、无重复
    3. 价格非负、high>=low、high/low 包络 open/close
    4. 缺失值策略：前向填充 ≤1 根后不得残留 NaN（除可选标记列）
    """
    if len(df) == 0:
        if allow_empty:
            return True
        raise HexEmptyDataError(
            "BarFrame 为 0 行：取数链路返回空结果。"
            "若确需容忍空结果（如区间内无交易日），请显式传 allow_empty=True。"
        )
    if not isinstance(df.index, pd.MultiIndex) or df.index.nlevels != 2:
        raise HexDataError("索引必须为 MultiIndex(symbol, datetime)")
    syms = df.index.get_level_values(0)
    dts = df.index.get_level_values(1)
    # 重复判定必须基于完整 (symbol, datetime) 索引：
    # 多品种天然共享日期，datetime 层级本身重复是正常的。
    if df.index.has_duplicates:
        dup = df.index[df.index.duplicated()].unique()[:3].tolist()
        raise HexDataError(f"存在重复 (symbol, datetime) 索引: {dup}")

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise HexDataError(f"缺失必需列: {missing}")

    # 单调性：按 (symbol, datetime) 排序
    for sym, grp in df.groupby(level=0):
        if not grp.index.get_level_values(1).is_monotonic_increasing:
            raise HexDataError(f"品种 {sym} 的时间索引非单调")

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    if (h < l).any():
        raise HexDataError("存在 high < low 的异常 bar")
    if (h < o).any() or (h < c).any() or (l > o).any() or (l > c).any():
        raise HexDataError("存在 OHLC 包络关系被破坏的 bar")
    if (c <= 0).any() or (o <= 0).any():
        raise HexDataError("存在非正价格")

    # 缺失值：按品种分别前向填充 1 根后剩余 NaN 视为错误（避免跨品种互填）
    fillable = df[REQUIRED_COLS].copy()
    ffill = fillable.groupby(level=0, group_keys=False).ffill(limit=1)
    residual = ffill.isna().sum().sum()
    if residual > 0:
        raise HexDataError(f"前向填充≤1根后仍存在 {residual} 个 NaN，数据不连续")

    return True


def as_barframe(df: pd.DataFrame, freq: str = "1d", source: str = "unknown") -> BarFrame:
    """便捷构造并校验 BarFrame。"""
    return BarFrame(df=df, freq=freq, source=source).validate()


# ---------------------------------------------------------------------------
# P0-13：免费源 OHLC 包络修复（共享实现）
# ---------------------------------------------------------------------------
def repair_envelope(
    df: pd.DataFrame, *, drop_zero_ohl: bool = True
) -> tuple[pd.DataFrame, list[str]]:
    """修复免费源的 OHLC 包络脏数据并丢弃废 bar（**校验前**的源层清洗）。

    背景（P0-13，2026-08-29 取证）：``AkshareSource`` 缺修复步骤，hc0
    （2021-12-30，C=4394 < L=4395）与 ni0（2023-08-28，C=167030 < L=167230）
    各 1 根源端毛刺 bar 使整次拉取被 ``validate_bars`` 拒绝；而
    ``SinaSource``/``PytdxSource`` 早已各自实现同语义修复（三处重复，
    本函数收口，调用方保留原方法名委托兼容）。

    语义（与 sina 原实现逐位一致）：
    - 包络破坏（如 close<low）：以**四价极值**重定 low/high，保证契约成立；
    - ``close<=0`` 废 bar（无成交）：丢弃；
    - ``drop_zero_ohl=True``（sina 语义）：``open/high/low<=0``（如 m0
      2019-07-29 open=0）也丢弃；False（pytdx 语义）保留，交由上层契约裁决。

    返回
    ----
    ``(修复后 df, 告警列表)``。**调用方必须上报告警**（logging 等），
    修复是数据变更，静默即事故。
    """
    notes: list[str] = []
    df = df.copy()
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    lo = df[["open", "high", "low", "close"]].min(axis=1)
    hi = df[["open", "high", "low", "close"]].max(axis=1)
    viol = (df["low"] != lo) | (df["high"] != hi)
    if viol.any():
        sample = _sample_dates(df, viol)
        suffix = f"（如 {', '.join(sample)}）" if sample else ""
        notes.append(f"包络修复 {int(viol.sum())} 根（low/high 重定为四价极值）{suffix}")
    df["low"] = lo
    df["high"] = hi
    drop_close = df["close"] <= 0
    if drop_close.any():
        notes.append(f"丢弃 close<=0 废 bar {int(drop_close.sum())} 根")
    df = df[df["close"] > 0].copy()
    if drop_zero_ohl:
        drop_ohl = (df[["open", "high", "low"]] <= 0).any(axis=1)
        if drop_ohl.any():
            notes.append(f"丢弃 open/high/low<=0 废 bar {int(drop_ohl.sum())} 根")
        df = df[(df[["open", "high", "low"]] > 0).all(axis=1)].copy()
    return df, notes


def _sample_dates(df: pd.DataFrame, mask: pd.Series, k: int = 3) -> list[str]:
    """从 date/datetime 列或索引取样例日期（取不到就返回空）。"""
    for c in ("datetime", "date"):
        if c in df.columns:
            return [str(v) for v in df.loc[mask, c].head(k)]
    if isinstance(df.index, pd.DatetimeIndex):
        return [str(v.date()) for v in df.index[mask][:k]]
    return []
