"""provisional 标记与真值重建流水线 —— P0-9（37 号文档 §6 硬约束 6 的落地）。

背景
----
备源续接（:func:`hexbroker.data.graft.graft_adjusted`）产生的数据是**临时值**
（``GraftResult.provisional``）：主备主力切换日不一致会引入恒定乘性偏移
（cu0 2026-08-21 实测 21.35 bp，且零告警、原理上不可前向检出），只能事后用
主源真值重建消除。同理，被隔离的缺失分区（``_MISSING_*.json``，见
2026-08-29 污染事故）也必须等主源恢复后重建。

本模块职责：
1. **标记持久化**：``_provisional.json`` sidecar 记录哪些年度分区的哪些日期
   是续接临时值。独立于 manifest —— manifest 会被 :meth:`DataLake.save_processed`
   在每次常规写入时重建，挂在那里会被冲掉。
2. **扫描**：找出所有"待重建"对象（provisional 临时段 + missing 缺失分区）。
3. **重建**：用注入的真值逐行替换/追加，**替换了的日期才清标**——真值没有
   覆盖到的日期保留标记，绝不静默把临时值转正。

设计红线：
- 真值来源由调用方注入（``truth_fetcher``），本模块不依赖任何具体数据源。
- 只动目标年度分区，绝不整湖覆盖。
- schema 不一致直接拒绝（宁可 skipped，不可部分写入）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import HexDataError
from ..utils.io import read_parquet, write_json, write_parquet
from .manifest import build_manifest, write_manifest
from .store import MISSING_GLOB, DataLake

#: provisional sidecar 文件名（与分区 manifest.json 同目录）
PROVISIONAL_SIDECAR = "_provisional.json"

#: ``MISSING_GLOB``（"_MISSING_*.json"）自 P0-12 起定义于 :mod:`.store`
#: 并在此转出（保持既有公共名兼容；store 是本模块的被依赖方）。

#: 必须存在的最小列集合（missing 分区重建时校验真值用）
_MIN_TRUTH_COLS = ("datetime", "symbol")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 标记持久化
# --------------------------------------------------------------------------
def _sidecar_path(root: Path, layer: str, symbol: str, freq: str) -> Path:
    return Path(root) / layer / symbol / freq / PROVISIONAL_SIDECAR


def mark_provisional(
    root: Path,
    layer: str,
    symbol: str,
    freq: str,
    year: int,
    dates,
    *,
    method: str = "graft",
    anchored_at: str = "",
    reason: str = "",
) -> Path:
    """把若干日期登记为续接临时值（幂等合并，已登记日期不重复）。

    ``dates`` 为 ``YYYY-MM-DD`` 字符串或可 ``strftime`` 的时间戳序列。
    """
    p = _sidecar_path(root, layer, symbol, freq)
    existing: dict = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}  # 损坏即重建（标记是辅助元数据，丢失方向是保守的）
    years = dict(existing.get("years") or {})
    norm = sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates})
    old = set(years.get(str(year), {}).get("dates") or [])
    entry = {
        "dates": sorted(old | set(norm)),
        "method": method,
        "anchored_at": anchored_at,
    }
    years[str(year)] = entry
    payload = {
        "symbol": symbol,
        "freq": freq,
        "layer": layer,
        "years": years,
        "reason": reason or existing.get("reason", ""),
        "created_at": existing.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    }
    write_json(payload, p)
    return p


def clear_provisional(
    root: Path, layer: str, symbol: str, freq: str, year: int | None = None,
    dates=None,
) -> bool:
    """清除 provisional 标记。

    - ``year=None``：清全部；``dates=None``：清该年全部。
    - ``dates`` 给定时只移除这些日期；该年清空后移除年份条目；
      全部年份清空后删除 sidecar 文件。
    返回是否有变更。
    """
    p = _sidecar_path(root, layer, symbol, freq)
    if not p.exists():
        return False
    payload = json.loads(p.read_text(encoding="utf-8"))
    years: dict = dict(payload.get("years") or {})
    if year is None:
        years = {}
    else:
        key = str(year)
        if dates is None:
            years.pop(key, None)
        else:
            drop = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates}
            entry = years.get(key)
            if entry:
                remain = sorted(set(entry.get("dates") or []) - drop)
                if remain:
                    entry["dates"] = remain
                else:
                    years.pop(key, None)
    if years:
        payload["years"] = years
        payload["updated_at"] = _now_iso()
        write_json(payload, p)
        return True
    p.unlink()
    return True


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
@dataclass
class RebuildNeeded:
    """一个待重建对象。"""

    symbol: str
    freq: str
    year: int
    #: provisional = 续接临时段；missing = 显式缺失分区
    kind: str
    #: 需要真值的日期（missing 时为空列表 -> 需要全年）
    dates: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def scan_rebuild_needed(root: Path, layer: str = "processed") -> list[RebuildNeeded]:
    """扫描 ``root/{layer}/*/*`` 下的 provisional sidecar 与 _MISSING 标记。

    返回按 (symbol, year) 排序的列表；同一 (symbol, freq, year) 同时存在
    两种标记时合并为一条（kind 记 provisional，missing 信息进 detail）。
    """
    root = Path(root)
    out: dict[tuple[str, str, int], RebuildNeeded] = {}
    for sym_dir in sorted(p for p in (root / layer).glob("*") if p.is_dir()):
        for freq_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
            # provisional
            sc = freq_dir / PROVISIONAL_SIDECAR
            if sc.exists():
                try:
                    payload = json.loads(sc.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = {}
                for y, entry in (payload.get("years") or {}).items():
                    out[(sym_dir.name, freq_dir.name, int(y))] = RebuildNeeded(
                        symbol=sym_dir.name, freq=freq_dir.name, year=int(y),
                        kind="provisional",
                        dates=list(entry.get("dates") or []),
                        detail={"method": entry.get("method", "")},
                    )
            # missing
            for mp in sorted(freq_dir.glob(MISSING_GLOB)):
                try:
                    mj = json.loads(mp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    mj = {}
                y = int(mj.get("year") or mp.stem.split("_")[-1])
                key = (sym_dir.name, freq_dir.name, y)
                item = out.get(key) or RebuildNeeded(
                    symbol=sym_dir.name, freq=freq_dir.name, year=y,
                    kind="missing",
                )
                if item.kind != "missing":
                    item.detail["also_missing"] = mj
                else:
                    item.detail = {
                        "quarantined_to": mj.get("quarantined_to", ""),
                        "reason": mj.get("reason", ""),
                    }
                out[key] = item
    return [out[k] for k in sorted(out)]


# --------------------------------------------------------------------------
# 重建
# --------------------------------------------------------------------------
@dataclass
class RebuildResult:
    symbol: str
    freq: str
    year: int
    kind: str
    #: 用真值替换的原有行日期
    replaced: list[str] = field(default_factory=list)
    #: 真值新追加的行日期
    appended: list[str] = field(default_factory=list)
    #: 真值未覆盖、仍挂 provisional 标记的日期（missing 场景下真值不足年同样记此）
    uncovered: list[str] = field(default_factory=list)
    rows_before: int = 0
    rows_after: int = 0
    sidecar_cleared: bool = False
    status: str = "OK"          # OK | SKIPPED
    reason: str = ""


def _year_partition_path(root: Path, layer: str, symbol: str, freq: str,
                         year: int) -> Path:
    return Path(root) / layer / symbol / freq / f"{year}.parquet"


def _norm_dates(values) -> list[pd.Timestamp]:
    return sorted({pd.Timestamp(v).normalize() for v in values})


def rebuild_partition(
    root: Path, item: RebuildNeeded, truth: pd.DataFrame,
) -> RebuildResult:
    """用一个年度分区的真值执行重建。

    语义
    ----
    - provisional：同日期行**逐行替换**为真值行；真值有而库中无的日期**追加**。
      真值未覆盖到的临时日期**保留标记**（不静默转正）。
    - missing：真值必须含该年的行，整分区新建；真值不含该年任何行则 SKIPPED。
    - 写回后重算 manifest（``source="truth-rebuild"``）。
    """
    root = Path(root)
    res = RebuildResult(symbol=item.symbol, freq=item.freq, year=item.year,
                        kind=item.kind)
    target = _year_partition_path(root, "processed", item.symbol,
                                  item.freq, item.year)

    truth = truth.copy()
    if "datetime" not in truth.columns or "symbol" not in truth.columns:
        res.status = "SKIPPED"
        res.reason = f"真值缺少必要列 {_MIN_TRUTH_COLS}（现有 {list(truth.columns)[:8]}）"
        return res
    truth["datetime"] = pd.to_datetime(truth["datetime"]).dt.tz_localize(None)
    truth = truth.sort_values("datetime").drop_duplicates(
        subset=["datetime"], keep="last")

    if target.exists():
        cur = read_parquet(target)
        cur["datetime"] = pd.to_datetime(cur["datetime"]).dt.tz_localize(None)
        res.rows_before = int(len(cur))
        if set(truth.columns) != set(cur.columns):
            res.status = "SKIPPED"
            res.reason = (
                f"真值列集合与分区不一致：仅真值有 {sorted(set(truth.columns)-set(cur.columns))[:6]}，"
                f"仅分区有 {sorted(set(cur.columns)-set(truth.columns))[:6]}"
            )
            return res
        merged = cur.set_index("datetime")
        t = truth.set_index("datetime")
        common = merged.index.intersection(t.index)
        res.replaced = [f"{d:%Y-%m-%d}" for d in common]
        new_dates = t.index.difference(merged.index)
        res.appended = [f"{d:%Y-%m-%d}" for d in new_dates]
        merged.loc[common] = t.loc[common]
        out = pd.concat([merged, t.loc[new_dates]]).sort_index()
        out = out.reset_index()
        # 列序对齐分区原序
        out = out[cur.columns]
    else:
        # missing / 分区不存在：以真值自建
        year_rows = truth[truth["datetime"].dt.year == item.year]
        if year_rows.empty:
            res.status = "SKIPPED"
            res.reason = f"真值不含 {item.year} 年任何行，拒绝凭空建分区"
            return res
        res.rows_before = 0
        res.appended = [f"{d:%Y-%m-%d}" for d in _norm_dates(year_rows["datetime"])]
        out = year_rows.reset_index(drop=True)

    res.rows_after = int(len(out))

    if res.appended or not target.exists():
        y0 = pd.to_datetime(res.appended[0] if res.appended
                            else out["datetime"].min()).year
        y1 = pd.to_datetime(res.appended[-1] if res.appended
                            else out["datetime"].max()).year
        if y0 != item.year or y1 != item.year:
            res.status = "SKIPPED"
            res.reason = f"写入范围 {y0}~{y1} 超出目标年度 {item.year}，拒绝跨界分区"
            return res

    merged_idx = out.set_index(["symbol", "datetime"]).sort_index()
    DataLake(root).save_processed(
        _Frame(df=merged_idx, freq=item.freq), symbol=item.symbol,
    )

    # ---- 清标：只清被真值覆盖的日期；真值一行没对上时保留全部标记 ----
    covered = set(res.replaced) | (set(res.appended) if res.kind == "provisional"
                                   else set())
    if item.kind == "provisional":
        res.uncovered = sorted(set(item.dates) - covered)
        if covered:
            clear_provisional(root, "processed", item.symbol, item.freq,
                              item.year, dates=covered)
        #: 语义 = sidecar 文件已被整体删除（全部日期清完）；
        #: 部分覆盖时文件仍在（残留日期继续挂标），此处应为 False。
        res.sidecar_cleared = not _sidecar_path(
            root, "processed", item.symbol, item.freq).exists()
    else:
        # missing：分区已按真值重建即可清 _MISSING 标记
        missing_marker = (
            Path(root) / "processed" / item.symbol / item.freq
            / f"_MISSING_{item.year}.json"
        )
        if missing_marker.exists():
            missing_marker.unlink()
        res.sidecar_cleared = True
    return res


class _Frame:
    """最小 BarFrame 协议适配（df + freq，供 DataLake.save_processed 使用）。"""

    def __init__(self, df: pd.DataFrame, freq: str):
        self.df = df
        self.freq = freq
        self.symbols = list(df.index.get_level_values("symbol").unique())

    def by_symbol(self, sym: str) -> pd.DataFrame:
        return self.df.loc[[sym]]


@dataclass
class RebuildReport:
    results: list[RebuildResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (对象, 原因)

    @property
    def ok(self) -> bool:
        return all(r.status == "OK" for r in self.results)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            tag = r.status
            lines.append(
                f"[{tag}] {r.symbol}/{r.year}({r.kind}) "
                f"替换 {len(r.replaced)} 追加 {len(r.appended)} "
                f"未覆盖 {len(r.uncovered)} {r.reason}"
            )
        for obj, why in self.skipped:
            lines.append(f"[FETCH-SKIP] {obj}: {why}")
        return "\n".join(lines)


def rebuild_pipeline(
    root: Path,
    truth_fetcher,
    *,
    layer: str = "processed",
    symbols: list[str] | None = None,
) -> RebuildReport:
    """扫描 → 逐对象拉真值 → 重建 → 报告。

    ``truth_fetcher(item: RebuildNeeded) -> pd.DataFrame``：返回该对象的真值
    （至少含 datetime/symbol 列；provisional 场景应覆盖 ``item.dates``）。
    抛任何异常都视为该对象拉取失败，记录后继续下一个（不中断整批）。
    """
    report = RebuildReport()
    items = scan_rebuild_needed(root, layer=layer)
    if symbols is not None:
        want = set(symbols)
        items = [it for it in items if it.symbol in want]
    for it in items:
        label = f"{it.symbol}/{it.year}({it.kind})"
        try:
            truth = truth_fetcher(it)
        except Exception as exc:  # noqa: BLE001 - 逐对象隔离，不让单点失败中断整批
            report.skipped.append((label, f"{type(exc).__name__}: {exc}"))
            continue
        if truth is None or len(truth) == 0:
            report.skipped.append((label, "truth_fetcher 返回空真值"))
            continue
        report.results.append(rebuild_partition(root, it, truth))
    return report
