"""Dominant（主力合约）日历本地快照（§3 架构决策，P1）。

背景：主源 pandadata 每次拉取都返回 ``dominant_id``（+ 不复权
``open_interest``），但此前从不落盘 —— 换月日历随主源"用完即弃"，
形成"主源挂了，换月日历也得问主源要"的死结。本模块把日历固化为
本地快照：``data/interim/dominant/{sym0}.parquet``。

⚠️ 已知陷阱（graft.py §4.8 实测，2026-08-28）：pandadata 的
``dominant_id`` 切换日 **≠** 后复权因子切换日（cu0 相差 3 个交易日）。
因此本日历只用于：换月日候选（graft ``rollover_dates``）、事后对账、
切换历史分析 —— **不得**直接当作后复权调整依据。

语义：
- ``extract_calendar``：pandadata 结果 DataFrame（``load_persisted_rows``
  输出）→ 规范化日历（datetime 索引、升序去重）；
- ``save_calendar``：merge-upsert（同日期覆盖 + 新日期追加），幂等；
- ``detect_switches``：dominant_id 变更明细（date/from_id/to_id）；
- ``rollover_dates(root, sym0, start, end)``：窗口内切换日列表。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DEFAULT_DIRNAME = "dominant"

REQUIRED_COLS = ("date", "dominant_id")


def _cal_dir(root: str | Path) -> Path:
    return Path(root) / "interim" / DEFAULT_DIRNAME


def _cal_path(root: str | Path, sym0: str) -> Path:
    return _cal_dir(root) / f"{sym0}.parquet"


def extract_calendar(df: pd.DataFrame) -> pd.DataFrame:
    """pandadata 结果 DataFrame → 规范化日历。

    列要求：``date``（YYYYMMDD 或可解析日期）、``dominant_id``；
    ``open_interest`` 存在则保留（不复权，可用于主力复核）。
    输出：``datetime`` 索引升序无重复，列 ``dominant_id``（+ 可选
    ``open_interest``）。
    """
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"输入缺少必需列: {missing}（现有: {list(df.columns)}）")
    out = df[["date", "dominant_id"] + (
        ["open_interest"] if "open_interest" in df.columns else []
    )].copy()
    out["date"] = pd.to_datetime(out["date"].astype(str), format="%Y%m%d")
    out = out.drop_duplicates(subset="date").sort_values("date")
    out = out.set_index("date").rename_axis("datetime")
    for col in out.columns:
        if col == "open_interest":
            out[col] = out[col].astype(float)
        else:
            out[col] = out[col].astype(str)
    return out


def save_calendar(root: str | Path, sym0: str, cal: pd.DataFrame) -> dict:
    """merge-upsert 快照：新日期追加 + 同日期以新值覆盖，幂等。

    返回写入统计：``{"sym0", "path", "total", "overwritten", "appended"}``。
    """
    path = _cal_path(root, sym0)
    cal = cal.sort_index()
    if path.exists():
        old = pd.read_parquet(path)
        common = cal.index.intersection(old.index)
        appended_dates = cal.index.difference(old.index)
        overwritten, appended = len(common), len(appended_dates)
        if not overwritten and not appended:
            return {"sym0": sym0, "path": str(path), "total": len(old),
                    "overwritten": 0, "appended": 0}
        merged = pd.concat([old.loc[old.index.difference(common)], cal]).sort_index()
    else:
        overwritten, appended = 0, len(cal)
        merged = cal
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(path)
    return {"sym0": sym0, "path": str(path), "total": len(merged),
            "overwritten": overwritten, "appended": appended}


def load_calendar(root: str | Path, sym0: str) -> pd.DataFrame | None:
    """读快照；不存在返回 None。"""
    path = _cal_path(root, sym0)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def detect_switches(cal: pd.DataFrame) -> pd.DataFrame:
    """dominant_id 变更明细：``date / from_id / to_id``（首个可用合约无
    from —— 入 DataFrame 后为 NaN，判定用 ``pd.isna``）。"""
    if cal.empty:
        return pd.DataFrame(columns=["date", "from_id", "to_id"])
    s = cal["dominant_id"].astype(str)
    switched = s != s.shift(1)
    idx = cal.index[switched]
    rows = []
    for i, d in enumerate(idx):
        loc = cal.index.get_loc(d)
        from_id = str(cal["dominant_id"].iloc[loc - 1]) if loc > 0 else None
        rows.append({"date": d, "from_id": from_id, "to_id": str(cal["dominant_id"].iloc[loc])})
    return pd.DataFrame(rows)


def rollover_dates(root: str | Path, sym0: str, start: str, end: str) -> list[pd.Timestamp]:
    """窗口内 (start, end] 的切换日 —— graft ``rollover_dates`` 直接可用。

    快照缺失 → ``HexDataError`` 语义由调用方处理（此处返回空列表并让
    调用方用 ``load_calendar`` 判断）→ 简化：缺失时抛 ``FileNotFoundError``。
    """
    cal = load_calendar(root, sym0)
    if cal is None:
        raise FileNotFoundError(f"无 {sym0} 的 dominant 快照：{_cal_path(root, sym0)}")
    sw = detect_switches(cal)
    if sw.empty:
        return []
    m = (sw["date"] > pd.Timestamp(start)) & (sw["date"] <= pd.Timestamp(end))
    return list(sw.loc[m, "date"])
