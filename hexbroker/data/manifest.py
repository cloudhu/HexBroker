"""P0-3 数据层 manifest（sidecar JSON，不改 Parquet schema）。

约定：``data/{layer}/{symbol}/{freq}/manifest.json``，与数据本体解耦；
写入用原子写；读路径零改动（仅新增读取侧校验能力）。

manifest 记录：数据版本、拉取时间、来源、口径常量（``adjust_method`` /
``main_rule`` / ``RAW_SCALE_FIX`` 等）、内容指纹、行数、日期范围。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..utils.io import read_parquet, write_json


@dataclass
class DataManifest:
    """数据层 manifest（与 Parquet 本体解耦的 sidecar）。"""

    layer: str                      # raw | interim | processed
    symbol: str
    freq: str
    data_version: str               # 语义版本（v1/v2...，回填脚本赋 "backfill-<ts>"）
    fetched_at: str                 # ISO 8601 UTC
    source: str
    constants: dict = field(default_factory=dict)   # adjust_method / main_rule / RAW_SCALE_FIX ...
    content_fingerprint: str = ""   # sha1(规范化内容) 前 12 位
    n_rows: int = 0
    date_range: tuple[str, str] = ("", "")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date_range"] = [self.date_range[0], self.date_range[1]]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DataManifest":
        dr = d.get("date_range") or ("", "")
        if isinstance(dr, list):
            dr = tuple(dr)
        return cls(
            layer=str(d.get("layer", "")),
            symbol=str(d.get("symbol", "")),
            freq=str(d.get("freq", "")),
            data_version=str(d.get("data_version", "")),
            fetched_at=str(d.get("fetched_at", "")),
            source=str(d.get("source", "")),
            constants=dict(d.get("constants") or {}),
            content_fingerprint=str(d.get("content_fingerprint", "")),
            n_rows=int(d.get("n_rows", 0)),
            date_range=tuple(dr),
        )


def content_fingerprint(df: pd.DataFrame) -> str:
    """sha1(规范化内容) 前 12 位。

    规范化：索引纳入内容、列排序、NaN→``__nan__``、浮点 round 12 位、
    时间戳 ISO；列顺序变化不影响指纹，内容变化必然改变指纹。
    """
    norm = df.copy()
    if isinstance(norm.index, pd.MultiIndex) or norm.index.name is not None:
        norm = norm.reset_index()
    norm = norm.reindex(sorted(norm.columns), axis=1)

    def _cell(v: Any) -> str:
        try:
            if pd.isna(v):
                return "__nan__"
        except (TypeError, ValueError):
            pass
        if isinstance(v, (np.datetime64, pd.Timestamp)):
            return pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M:%S")
        if isinstance(v, (np.floating, float)):
            return f"{float(v):.12f}"
        if isinstance(v, (np.integer, int)):
            return str(int(v))
        if isinstance(v, (np.bool_, bool)):
            return str(bool(v))
        return str(v)

    payload: list[str] = []
    for col in norm.columns:
        payload.append(str(col))
        for v in norm[col].to_numpy():
            payload.append(_cell(v))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def manifest_path(root: Path, layer: str, symbol: str, freq: str) -> Path:
    """sidecar 路径：data/{layer}/{symbol}/{freq}/manifest.json。"""
    return Path(root) / layer / symbol / freq / "manifest.json"


def write_manifest(m: DataManifest, root: Path) -> Path:
    """写 data/{layer}/{symbol}/{freq}/manifest.json（原子写）。"""
    p = manifest_path(root, m.layer, m.symbol, m.freq)
    write_json(m.to_dict(), p)
    return p


def read_manifest(root: Path, layer: str, symbol: str, freq: str) -> Optional[DataManifest]:
    """读取 manifest；不存在或解析失败返回 None（读路径零侵入）。"""
    p = manifest_path(root, layer, symbol, freq)
    if not p.exists():
        return None
    try:
        return DataManifest.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def build_manifest(layer: str, symbol: str, freq: str, df: pd.DataFrame, *,
                   source: str = "unknown", data_version: str = "v1",
                   constants: Optional[dict] = None) -> DataManifest:
    """由 DataFrame 构造 manifest（自动计算内容指纹/行数/日期范围）。"""
    dts: Optional[pd.Series] = None
    if isinstance(df.index, pd.MultiIndex):
        for level in ("datetime", "ts", "date"):
            if level in df.index.names:
                dts = pd.to_datetime(df.index.get_level_values(level))
                break
    else:
        for c in ("datetime", "ts", "date"):
            if c in df.columns:
                dts = pd.to_datetime(df[c])
                break
    date_range = ("", "")
    if dts is not None and len(dts):
        date_range = (dts.min().strftime("%Y-%m-%d"), dts.max().strftime("%Y-%m-%d"))
    return DataManifest(
        layer=layer,
        symbol=symbol,
        freq=freq,
        data_version=data_version,
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=source,
        constants=dict(constants or {}),
        content_fingerprint=content_fingerprint(df),
        n_rows=int(len(df)),
        date_range=date_range,
    )


def backfill_manifests(root: Path, cfg: Any, *, skip_existing: bool = False) -> int:
    """对存量 parquet 分区回填 manifest；返回回填数（PRD A3.1 迁移脚本）。

    覆盖两种布局：
    - ``{layer}/{symbol}/{freq}/{year}.parquet``（按年分区）；
    - ``{layer}/{symbol}/{freq}.parquet``（无年份分区）。

    ``skip_existing=True`` 时跳过已有 manifest 的分区（增量补全，不触碰
    生产自动化在维护的 manifest，如盘中流水线按 ``v1`` 重写的品种）。
    """
    root = Path(root)
    constants = _constants_from_cfg(cfg)
    count = 0
    for layer in ("raw", "interim", "processed"):
        layer_dir = root / layer
        if not layer_dir.exists():
            continue
        for symbol_dir in sorted(p for p in layer_dir.iterdir() if p.is_dir()):
            # 按年分区目录
            for freq_dir in sorted(p for p in symbol_dir.iterdir() if p.is_dir()):
                files = sorted(freq_dir.glob("*.parquet"))
                if not files:
                    continue
                if skip_existing and (freq_dir / "manifest.json").exists():
                    continue
                parts = [read_parquet(f) for f in files]
                df = pd.concat(parts, ignore_index=True)
                write_manifest(
                    build_manifest(
                        layer, symbol_dir.name, freq_dir.name, df,
                        source="lake",
                        data_version=_backfill_version(),
                        constants=constants,
                    ),
                    root,
                )
                count += 1
            # 无年份分区文件
            for freq_file in sorted(symbol_dir.glob("*.parquet")):
                if skip_existing and (
                    freq_file.with_name("manifest.json").exists()
                    or (freq_file.parent / freq_file.stem / "manifest.json").exists()
                ):
                    continue
                df = read_parquet(freq_file)
                write_manifest(
                    build_manifest(
                        layer, symbol_dir.name, freq_file.stem, df,
                        source="lake",
                        data_version=_backfill_version(),
                        constants=constants,
                    ),
                    root,
                )
                count += 1
    return count


def _backfill_version() -> str:
    return f"backfill-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


def _constants_from_cfg(cfg: Any) -> dict:
    """从配置抽取口径常量（adjust_method / main_rule / RAW_SCALE_FIX）。"""
    out: dict[str, Any] = {}
    dc = getattr(cfg, "data", None)
    if dc is not None:
        out["adjust_method"] = str(getattr(dc, "adjust_method", ""))
        out["main_rule"] = str(getattr(dc, "main_rule", ""))
    try:
        from ..constants import RAW_SCALE_FIX  # 若常量表存在则记录
        out["RAW_SCALE_FIX"] = RAW_SCALE_FIX
    except (ImportError, AttributeError):
        pass
    return out
