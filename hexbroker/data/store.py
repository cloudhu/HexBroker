"""数据湖（§2.2 L1）：``data/raw|interim|processed`` 分层 Parquet，
按 ``symbol/freq/year`` 分区。底层 IO 见 ``hexbroker.utils.io``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from ..utils.io import read_parquet, write_parquet
from .schema import BarFrame

logger = logging.getLogger(__name__)

#: 缺失年度标记（与 ``rebuild.MISSING_GLOB`` 同值。store 是 rebuild 的被依赖
#: 方，不可反向导入 —— 由此处定义、rebuild 转出，避免循环导入）。
MISSING_GLOB = "_MISSING_*.json"


@dataclass(frozen=True)
class DataQualityNote:
    """读取时的数据质量提示（P0-12 缺失年度显式化）。

    kind:
    - ``hole`` —— present 年度范围内的洞（分区缺失）。有 ``_MISSING`` 标记
      时 detail 带标记内容；**无标记的洞最危险**（零留痕，静默拼接）。
    - ``missing_mark`` —— 范围外的孤儿标记（该年无分区也不在洞范围内）。
    - ``stale_mark`` —— 标记与分区并存（标记已过时应清除）。
    """

    kind: str
    year: int
    detail: str


class DataLake:
    """分层本地数据湖。"""

    def __init__(self, root: Optional[str | Path] = None,
                 constants: Optional[dict] = None) -> None:
        self.root = Path(root) if root else Path("data")
        # P0-3：写 manifest 时携带的口径常量（adjust_method/main_rule/RAW_SCALE_FIX 等）
        self.constants = dict(constants or {})
        for layer in ("raw", "interim", "processed"):
            (self.root / layer).mkdir(parents=True, exist_ok=True)

    def _path(self, layer: str, symbol: str, freq: str, year: Optional[int] = None) -> Path:
        if year is None:
            return self.root / layer / symbol / f"{freq}.parquet"
        return self.root / layer / symbol / freq / f"{year}.parquet"

    def save_processed(self, bars: BarFrame, symbol: Optional[str] = None) -> None:
        """保存已处理 BarFrame（按 symbol 拆分分区），并自动写/更新 manifest（P0-3）。

        manifest 为 sidecar JSON（``processed/{symbol}/{freq}/manifest.json``），
        不改 Parquet schema；仅当有新数据写入时更新。
        """
        from .manifest import build_manifest, write_manifest

        for sym in bars.symbols:
            df = bars.by_symbol(sym)
            years = df.index.get_level_values("datetime").year.unique()
            for y in years:
                sub = df[df.index.get_level_values("datetime").year == y]
                write_parquet(sub.reset_index(), self._path("processed", sym, bars.freq, int(y)))
            write_manifest(
                build_manifest("processed", sym, bars.freq, df,
                               source="lake", data_version="v1", constants=self.constants),
                self.root,
            )

    # ---- P0-12：缺失年度显式化 ----------------------------------------
    def quality_notes(self, symbol: str, freq: str) -> list[DataQualityNote]:
        """扫描 ``processed/{symbol}/{freq}`` 的缺失年度与标记状态。

        纯只读：只看分区文件名与 ``_MISSING_*.json`` sidecar，不读 Parquet
        内容。返回值可能为空（干净湖）。
        """
        base = self.root / "processed" / symbol / freq
        if not base.exists():
            return []
        present = sorted(
            int(f.stem) for f in base.glob("*.parquet") if f.stem.isdigit()
        )
        if not present:
            return []

        marks: dict[int, dict] = {}
        for mp in sorted(base.glob(MISSING_GLOB)):
            try:
                payload = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            y = payload.get("year") if isinstance(payload, dict) else None
            if not isinstance(y, int):
                try:
                    y = int(mp.stem.split("_")[-1])
                except ValueError:
                    continue  # 文件名与内容都解析不出年度 → 忽略
            marks[int(y)] = payload if isinstance(payload, dict) else {}

        notes: list[DataQualityNote] = []
        full_range = set(range(present[0], present[-1] + 1))
        for y in sorted(full_range - set(present)):
            m = marks.get(y)
            if m is not None:
                parts = [m.get("reason") or "存在 _MISSING 标记"]
                if m.get("quarantined_to"):
                    parts.append(f"隔离于 {m['quarantined_to']}")
                if m.get("rebuild_condition"):
                    parts.append(f"重建条件：{m['rebuild_condition']}")
                notes.append(DataQualityNote("hole", y, "；".join(parts)))
            else:
                notes.append(DataQualityNote(
                    "hole", y,
                    "年度分区缺失且无 _MISSING 标记（零留痕，静默拼接，最危险）",
                ))
        for y in sorted(set(marks) & set(present)):
            notes.append(DataQualityNote(
                "stale_mark", y, "_MISSING 标记与分区并存，标记已过时应清除",
            ))
        for y in sorted(set(marks) - set(present) - full_range):
            notes.append(DataQualityNote(
                "missing_mark", y, "范围外孤儿 _MISSING 标记（该年无分区）",
            ))
        return notes

    def load_processed(self, symbol: str, freq: str, *, warn: bool = True) -> BarFrame:
        """读取某品种已处理数据，拼回 MultiIndex。

        P0-12：存在年度洞 / ``_MISSING`` 标记时**逐条告警**（logging），
        数据行为不变（非破坏性）——洞仍会被静默拼接，但不再无声。
        程序化消费方可调用 :meth:`quality_notes` 拿结构化结果。
        ``warn=False`` 可关闭告警（仅建议在已显式处理 quality_notes 的
        调用方使用）。
        """
        base = self.root / "processed" / symbol / freq
        files = sorted(base.glob("*.parquet")) if base.exists() else []
        if not files:
            raise FileNotFoundError(f"数据湖中无 {symbol}/{freq} 的处理数据")
        if warn:
            for n in self.quality_notes(symbol, freq):
                logger.warning(
                    "load_processed %s/%s: [%s] %s 年度：%s",
                    symbol, freq, n.kind, n.year, n.detail,
                )
        parts = [read_parquet(f) for f in files]
        df = pd.concat(parts)
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        df = df.set_index(["symbol", "datetime"]).sort_index()
        return BarFrame(df=df, freq=freq, source="lake")

    def exists(self, symbol: str, freq: str) -> bool:
        base = self.root / "processed" / symbol / freq
        return base.exists() and any(base.glob("*.parquet"))
