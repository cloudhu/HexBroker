#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P6-4 数据缺口补齐：计划 → 解析合并 → 校验。

背景
----
18 品种主力连续日线（data/raw/processed/{sym0}/1d/{year}.parquet）存在数据缺口：
  1. 2026 尾部：15/18 品种止于 2026-02-24（缺到 2026-08-17）；仅 au0/ag0/m0 到 2026-08-14
  2. 2024 缺口：au0/ag0/m0 缺下半年；cu0/rb0 缺上半年
  3. 2022 缺口：cu0/rb0/i0 缺 2022-01-04 ~ 2022-02-16
  4. sc0 2018：189/243 为上市前（2018-01-02~2018-03-23），非数据缺口
数据源为 PandaData get_future_daily_post（method=close_pcr 后复权主力连续），
与现有 18 品种数据同口径（data/interim/pd_*.csv 的 method=close_pcr）。

⚠️ 授权恢复前不执行真实拉取；本脚本只负责：
  --stage plan    扫描缺口，生成拉取计划 artifacts/p6_4_pull_plan.json（只读）
  --stage parse   解析单次持久化拉取结果，合并写回分片 parquet（写前备份）
  --stage verify  重扫缺口，输出闭合率 + 拼接连续性校验（只读）

用法
----
  python scripts/p6_4_fill_gaps.py --stage plan
  python scripts/p6_4_fill_gaps.py --stage parse <持久化文件> --sym AU --seg 20240718_20241231 [--force] [--dry-run]
  python scripts/p6_4_fill_gaps.py --stage verify [--plan artifacts/p6_4_pull_plan.json]

设计要点
--------
- 理论交易日历 = 全品种并集日期，经“覆盖比”过滤（present/covering >= 0.5），
  剔除个别品种偶发的假日脏行（如 2020-10-02、2022-04-04、2024-06-10），
  同时保留 2026 尾部（该区间仅 3 个品种覆盖，覆盖比 100%）。
- 参考截止日 TARGET_END_DATE = 2026-08-17；拉取窗口 = 最近 5 年。
- 缺口段 = 交易日历上连续的缺失日 run（跨周末/节假日不断开）。
- 拉取段按 MAX_SEGMENT_YEARS=4 拆分（网关实测 ~1000 行截断，约 4 年日线，
  且在网关 5 年限制内留余量）。
- parse 幂等：artifacts/p6_4_applied.json 记录已应用段，同 (sym,seg) 跳过（--force 覆盖）。
- 写回前备份原分片到 artifacts/backup_p64/。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from dateutil.relativedelta import relativedelta

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "raw" / "processed"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
PLAN_PATH = ARTIFACTS_DIR / "p6_4_pull_plan.json"
APPLIED_PATH = ARTIFACTS_DIR / "p6_4_applied.json"
BACKUP_DIR = ARTIFACTS_DIR / "backup_p64"

TARGET_END_DATE = date(2026, 8, 17)   # 参考截止日（主理人核实；周一交易日）
PULL_WINDOW_YEARS = 5                 # 网关 start~end 上限（最近 5 年）
MAX_SEGMENT_YEARS = 4.0               # 实测 1000 行截断 ≈ 4 年日线，留余量
MIN_COVER_RATIO = 0.5                 # 交易日判定阈值：present/covering

SCHEMA_COLUMNS = [
    "symbol", "datetime", "open", "high", "low", "close",
    "volume", "amount", "open_interest", "raw_close", "adj_close",
    "limit_up", "limit_down", "is_rollover",
]
FLOAT_COLUMNS = ["open", "high", "low", "close", "volume", "amount",
                 "open_interest", "raw_close", "adj_close"]
BOOL_COLUMNS = ["limit_up", "limit_down", "is_rollover"]

# sym0 → underlying_symbol（大写、无 0）
SYMBOL_MAP = {
    "au0": "AU", "ag0": "AG", "m0": "M", "cu0": "CU", "rb0": "RB",
    "i0": "I", "al0": "AL", "zn0": "ZN", "ni0": "NI", "hc0": "HC",
    "y0": "Y", "p0": "P", "j0": "J", "jm0": "JM", "sr0": "SR",
    "cf0": "CF", "ta0": "TA", "sc0": "SC",
}
UNDERLYING_TO_SYM0 = {v: k for k, v in SYMBOL_MAP.items()}

OVERLAP_TOLERANCE = 0.01  # 重叠日 close 相对偏差阈值（1%）

# --------------------------------------------------------------------------- #
# 价格口径校准（P6-4 实测发现，2026-08-19）
#
# 背景：data/raw/processed 中 15 个品种（al0/cf0/cu0/hc0/i0/j0/jm0/ni0/p0/
# rb0/sc0/sr0/ta0/y0/zn0）既有序列为 close_pcr 后复权口径，与 PandaData 新拉取
# 数据一致，拼接连续（重叠日 close 偏差 <1%）。
#
# 但 au0/ag0/m0 三个品种的既有序列为【未复权原始价格】口径：
#   - 既有 au0 2026-08-14 close=911.27，而 close_pcr csv 同日=723.44（比率 1.2596）
#   - 既有 ag0 2026-08-14 close=15596.94，而 close_pcr csv 同日=10752.43（比率 1.4505）
#   - 既有 m0  2026-08-14 close=3156.31，而 close_pcr csv 同日=9962.17（比率 0.3168）
# 经 data/interim/pd_*_daily.csv（close_pcr 口径）与既有 parquet 全量重叠日核对，
# 该比率在 2024-08 ~ 2026-08 全窗口内【逐日恒定】（m0 精确到 1e-15），仅 2024-07
# 上旬主力换月当日有 ±0.4% 抖动。
#
# 因此 parse 阶段对 au0/ag0/m0 的新数据（close_pcr）乘以下述常数，换算到既有
# 序列的原始价格口径，保证拼接连续；否则直接合并会产生 20%~216% 的伪跳变。
# 换算系数 = 既有原始价 / close_pcr（取 2024-08 后稳定段）。
RAW_SCALE_FIX = {
    "m0": 0.31682966433418885,
    "au0": 1.2596349591656173,
    "ag0": 1.4505499241026856,
}


# --------------------------------------------------------------------------- #
# 数据读取 / 日历
# --------------------------------------------------------------------------- #
def load_symbol_frames() -> dict[str, pd.DataFrame]:
    """读取全部品种分片 parquet → {sym0: DataFrame(datetime 归一化、去重、排序)}。"""
    frames: dict[str, pd.DataFrame] = {}
    for sym_dir in sorted(PROCESSED_DIR.iterdir()):
        if not sym_dir.is_dir():
            continue
        day_dir = sym_dir / "1d"
        if not day_dir.is_dir():
            continue
        parts: list[pd.DataFrame] = []
        for pf in sorted(day_dir.glob("*.parquet")):
            try:
                df = pd.read_parquet(pf)
            except Exception as exc:  # noqa: BLE001 - 单文件损坏不阻塞整体
                print(f"[WARN] 读取失败 {pf}: {exc}")
                continue
            if df is None or df.empty or "datetime" not in df.columns:
                continue
            df = df.copy()
            df["datetime"] = pd.to_datetime(df["datetime"]).dt.normalize()
            parts.append(df)
        if parts:
            frames[sym_dir.name] = (
                pd.concat(parts, ignore_index=True)
                .drop_duplicates(subset="datetime", keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
    return frames


def build_calendar(frames: dict[str, pd.DataFrame]) -> tuple[list[pd.Timestamp], dict[str, object]]:
    """构造理论交易日历。

    规则：某日期为交易日，当且仅当 present/covering >= MIN_COVER_RATIO，
    其中 covering = 数据范围覆盖该日期的品种数，present = 实际有该日数据的品种数。
    该规则剔除个别品种偶发的假日脏行（2020-10-02 等仅 1~3 品种有行），
    同时保留 2026 尾部（仅 au0/ag0/m0 覆盖该区间，覆盖比 100%）。
    """
    dates_by_sym: dict[str, set[pd.Timestamp]] = {}
    all_dates: set[pd.Timestamp] = set()
    ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for sym, df in frames.items():
        s = set(df["datetime"])
        dates_by_sym[sym] = s
        all_dates.update(s)
        ranges[sym] = (min(s), max(s))

    # 每日期在各品种的出现次数（必须逐品种累加，不能 Counter(并集)）
    counter: Counter = Counter()
    for s in dates_by_sym.values():
        counter.update(s)

    def is_trading_day(d: pd.Timestamp) -> bool:
        cover = sum(1 for lo, hi in ranges.values() if lo <= d <= hi)
        if cover == 0:
            return False
        present = counter.get(d, 0)
        return present / cover >= MIN_COVER_RATIO

    cal = sorted(d for d in all_dates if is_trading_day(d))
    target_ts = pd.Timestamp(TARGET_END_DATE)
    if target_ts not in set(cal) and target_ts.dayofweek < 5:
        cal = sorted(cal + [target_ts])

    meta: dict[str, object] = {
        "days": len(cal),
        "start": cal[0].date().isoformat() if cal else None,
        "end": cal[-1].date().isoformat() if cal else None,
        "removed_non_trading": [d.date().isoformat() for d in sorted(all_dates - set(cal))],
    }
    return cal, meta


def find_gap_regions(dates_set: set[pd.Timestamp],
                     calendar: list[pd.Timestamp],
                     earliest: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """返回 [earliest, TARGET_END_DATE] 内缺失交易日的连续 run 列表。

    每个 run 是交易日历上连续的缺失日（跨周末/节假日不断开），
    返回 (start_ts, end_ts, 缺失天数)。
    """
    missing_set = {d for d in calendar if d >= earliest and d not in dates_set}
    if not missing_set:
        return []
    regions: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    run: list[pd.Timestamp] = []
    for d in calendar:
        if d in missing_set:
            run.append(d)
        elif run:
            regions.append((run[0], run[-1], len(run)))
            run = []
    if run:
        regions.append((run[0], run[-1], len(run)))
    return regions


def split_span(start: date, end: date, max_years: float) -> list[tuple[date, date]]:
    """把 [start, end] 拆成不超过 max_years 的子段（网关 start~end 限制）。"""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = cur + relativedelta(years=max_years)
        if nxt > end:
            nxt = end
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return chunks


# --------------------------------------------------------------------------- #
# plan 阶段
# --------------------------------------------------------------------------- #
def stage_plan() -> int:
    """扫描缺口并写出 artifacts/p6_4_pull_plan.json（只读数据文件）。"""
    frames = load_symbol_frames()
    if not frames:
        print("[FAIL] 未发现任何品种数据")
        return 1
    calendar, cal_meta = build_calendar(frames)
    window_start = pd.Timestamp(TARGET_END_DATE - relativedelta(years=PULL_WINDOW_YEARS)
                                + timedelta(days=1))

    print(f"[PLAN] 交易日历: {cal_meta['days']} 天 "
          f"({cal_meta['start']} ~ {cal_meta['end']})")
    removed = cal_meta["removed_non_trading"]
    if removed:
        print(f"[PLAN] 剔除非交易日(假日脏行): {', '.join(removed)}")
    print(f"[PLAN] 拉取窗口: {window_start.date()} ~ {TARGET_END_DATE} "
          f"(最近 {PULL_WINDOW_YEARS} 年，单段上限 {MAX_SEGMENT_YEARS} 年)")

    segments: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    notes: list[str] = []
    per_symbol: dict[str, dict[str, object]] = {}

    for sym in sorted(frames):
        underlying = SYMBOL_MAP.get(sym)
        if underlying is None:
            notes.append(f"{sym}: 未知品种映射，跳过")
            continue
        ds = set(frames[sym]["datetime"])
        earliest = min(ds)
        if earliest > pd.Timestamp(cal_meta["start"]):
            notes.append(
                f"{sym}: 最早数据 {earliest.date()} 晚于全局 {cal_meta['start']}，"
                f"上市前日期不视为缺口"
            )
        regions = find_gap_regions(ds, calendar, earliest)
        sym_segs: list[dict[str, object]] = []
        sym_skips: list[dict[str, object]] = []
        missing_total = 0
        missing_in_window = 0
        for start_ts, end_ts, days in regions:
            missing_total += days
            if end_ts.date() < window_start.date():
                sym_skips.append({
                    "sym0": sym, "underlying_symbol": underlying,
                    "start_date": start_ts.strftime("%Y%m%d"),
                    "end_date": end_ts.strftime("%Y%m%d"),
                    "seg_label": f"{start_ts:%Y%m%d}_{end_ts:%Y%m%d}",
                    "expected_days": days,
                    "reason": "outside_5y_window",
                })
                skipped.append(sym_skips[-1])
                continue
            clip_start = max(start_ts, window_start)
            for c0, c1 in split_span(clip_start.date(), end_ts.date(), MAX_SEGMENT_YEARS):
                c0_ts, c1_ts = pd.Timestamp(c0), pd.Timestamp(c1)
                expected = sum(1 for d in calendar
                               if c0_ts <= d <= c1_ts and d not in ds)
                seg = {
                    "underlying_symbol": underlying,
                    "sym0": sym,
                    "start_date": c0.strftime("%Y%m%d"),
                    "end_date": c1.strftime("%Y%m%d"),
                    "seg_label": f"{c0:%Y%m%d}_{c1:%Y%m%d}",
                    "expected_days": expected,
                    "method": "close_pcr",
                    "pull_params": {
                        "underlying_symbol": underlying,
                        "start_date": c0.strftime("%Y%m%d"),
                        "end_date": c1.strftime("%Y%m%d"),
                        "method": "close_pcr",
                    },
                }
                segments.append(seg)
                sym_segs.append(seg)
                missing_in_window += expected

        per_symbol[sym] = {
            "underlying_symbol": underlying,
            "earliest": earliest.date().isoformat(),
            "latest": max(ds).date().isoformat(),
            "rows": int(len(frames[sym])),
            "missing_total": missing_total,
            "missing_in_window": missing_in_window,
            "segments": sym_segs,
            "skipped": sym_skips,
        }

        desc = "; ".join(
            f"{s['seg_label']}({s['expected_days']}d)" for s in sym_segs
        ) or "-"
        sk = "; ".join(
            f"{s['seg_label']}({s['expected_days']}d)" for s in sym_skips
        ) or "-"
        print(f"[PLAN] {sym} ({underlying}): 缺 {missing_total}d / 窗口内 {missing_in_window}d "
              f"| 拉取 [{desc}] | 跳过 [{sk}]")

    plan: dict[str, object] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_end_date": TARGET_END_DATE.isoformat(),
        "pull_window_start": window_start.date().isoformat(),
        "pull_window_years": PULL_WINDOW_YEARS,
        "max_segment_years": MAX_SEGMENT_YEARS,
        "calendar": cal_meta,
        "segments": segments,
        "skipped": skipped,
        "per_symbol": per_symbol,
        "notes": notes,
    }
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total_expected = sum(int(s["expected_days"]) for s in segments)
    print(f"[PLAN] 共 {len(segments)} 个拉取段，预计 {total_expected} 个交易日 → {PLAN_PATH}")
    print(f"[PLAN] 跳过 {len(skipped)} 段（窗口外）")
    if notes:
        for n in notes:
            print(f"[PLAN] NOTE: {n}")
    return 0


# --------------------------------------------------------------------------- #
# parse 阶段
# --------------------------------------------------------------------------- #
def normalize_sym_arg(sym_arg: str) -> tuple[str, str]:
    """把用户传入的品种名归一化为 (underlying, sym0)。支持 AU / AU0 / au0 / m0 / M。"""
    raw = sym_arg.strip().upper()
    if raw.endswith("0"):
        underlying = raw[:-1]
    else:
        underlying = raw
    sym0 = (underlying.lower() + "0") if underlying != "M" else "m0"
    if underlying not in UNDERLYING_TO_SYM0:
        raise ValueError(f"未知品种: {sym_arg}（合法: {sorted(UNDERLYING_TO_SYM0)}）")
    return underlying, sym0


def parse_seg_label(seg: str) -> tuple[date, date]:
    """解析 seg_label YYYYMMDD_YYYYMMDD → (start, end)。"""
    try:
        a, b = seg.strip().split("_")
        return datetime.strptime(a, "%Y%m%d").date(), datetime.strptime(b, "%Y%m%d").date()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"seg 格式应为 YYYYMMDD_YYYYMMDD，实际: {seg!r}") from exc


def load_persisted_rows(path: Path) -> pd.DataFrame:
    """解析持久化 JSON → DataFrame。

    兼容三种形态：
      1. connector-proxy 包装: {ok, method, params, result:{type,columns,rows}}
      2. 任务描述形态: {result: [ {symbol, date, ...}, ... ]}
      3. 裸数组 / {data: [...]}
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法解析 JSON: {path} ({exc})") from exc

    res = raw
    if isinstance(raw, dict):
        if "result" in raw:
            res = raw["result"]
        elif "data" in raw:
            res = raw["data"]
        else:
            # 兜底：直接尝试 dict-of-arrays / 单行 dict
            res = raw

    if isinstance(res, dict):
        if res.get("type") == "dataframe" and "rows" in res:
            cols = res.get("columns") or []
            rows = res.get("rows") or []
            return pd.DataFrame(rows, columns=cols)
        if "columns" in res and "rows" in res:
            cols = res.get("columns") or []
            rows = res.get("rows") or []
            return pd.DataFrame(rows, columns=cols)
        # dict-of-arrays（各列等长）
        try:
            return pd.DataFrame(res)
        except ValueError as exc:
            raise ValueError(f"result 结构无法识别: {list(res.keys())}") from exc

    if isinstance(res, list):
        if not res:
            return pd.DataFrame()
        if all(isinstance(r, dict) for r in res):
            return pd.DataFrame(res)
        raise ValueError("result 为数组但非 dict 列表")

    raise ValueError(f"result 类型不支持: {type(res)}")


def normalize_new_df(df: pd.DataFrame,
                     underlying: str,
                     sym0: str,
                     seg_start: date,
                     seg_end: date) -> pd.DataFrame:
    """把源 DataFrame 映射为目标 schema，过滤品种与日期区间。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    df = df.copy()

    # 1) 定位日期列并解析
    date_col = next((c for c in ("date", "datetime", "trade_date", "trading_date")
                     if c in df.columns), None)
    if date_col is None:
        raise ValueError(f"缺少日期列 (date/datetime)，实际列: {list(df.columns)}")
    parsed = pd.to_datetime(df[date_col].astype(str), format="%Y%m%d", errors="coerce")
    if parsed.isna().any():
        parsed = parsed.fillna(pd.to_datetime(df[date_col], errors="coerce"))
    df["datetime"] = pd.to_datetime(parsed).dt.normalize()

    # 2) 过滤品种
    if "underlying_symbol" in df.columns:
        df = df[df["underlying_symbol"].astype(str).str.upper() == underlying]
    elif "symbol" in df.columns:
        df = df[df["symbol"].astype(str).str.upper().str.startswith(underlying)]
    else:
        print(f"[WARN] 无 underlying_symbol/symbol 列，假定全部属于 {underlying}")

    # 3) 过滤日期区间（含端点）
    lo, hi = pd.Timestamp(seg_start), pd.Timestamp(seg_end)
    df = df[(df["datetime"] >= lo) & (df["datetime"] <= hi)]
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    # 4) 数值列
    def num(col: str, default: float = 0.0) -> pd.Series:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
        return pd.Series(default, index=df.index)

    close = num("close")
    scale = RAW_SCALE_FIX.get(sym0, 1.0)
    if scale != 1.0:
        print(f"[SCALE] {sym0} close_pcr → 既有原始口径 系数 {scale:.8f}")
    out = pd.DataFrame({
        "symbol": sym0,
        "datetime": df["datetime"],
        "open": num("open") * scale,
        "high": num("high") * scale,
        "low": num("low") * scale,
        "close": close * scale,
        "volume": num("volume"),
        "amount": num("amount"),
        "open_interest": num("open_interest"),
        "raw_close": close * scale,
        "adj_close": close * scale,
        "limit_up": False,
        "limit_down": False,
        "is_rollover": False,
    })
    out = out.dropna(subset=["close"])
    return coerce_schema(out)


def coerce_schema(df: pd.DataFrame) -> pd.DataFrame:
    """统一 schema 列序与 dtype。"""
    for col in SCHEMA_COLUMNS:
        if col not in df.columns:
            if col in BOOL_COLUMNS:
                df[col] = False
            elif col in ("raw_close", "adj_close"):
                df[col] = df["close"] if "close" in df.columns else 0.0
            elif col in ("amount", "open_interest"):
                df[col] = 0.0
            elif col == "symbol":
                df[col] = ""
    df = df[SCHEMA_COLUMNS].copy()
    df["symbol"] = df["symbol"].astype(str)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.normalize()
    for col in FLOAT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in BOOL_COLUMNS:
        df[col] = df[col].fillna(False).astype(bool)
    return df


def enrich_raw_close(df: pd.DataFrame, sym0: str, *,
                     fetcher: Any = None,
                     min_coverage: float = 0.9) -> tuple[pd.DataFrame, list[str]]:
    """P1-c：用外部备源名义价回填 ``raw_close``。

    背景（37 号 §4.12）：pandadata ``close_pcr`` 只供复权价，管线原映射
    ``raw_close = adj_close = close``，湖内 nominal 校准信息完全丢失，
    "名义价冒充"无法湖内自检（P0-10 只能靠外部探针定罪）。

    契约（静默即事故）：
    - 对齐成功（覆盖率 ≥ min_coverage）→ ``raw_close`` 写真实名义价，
      notes 记录来源与覆盖行数；
    - 失败（备源全败 / 覆盖率不足 / 空帧）→ **原样返回**（``raw_close``
      仍为 adj 复制品），notes 含归因 —— 调用方必须逐条打印，
      禁止静默吞掉。

    all-or-nothing：不做部分对齐，避免同一列内语义混杂。

    ``fetcher`` 供测试注入（鸭子类型：只需 ``fetch_raw``）；默认惰性构造
    ``BackupRawFetcher(save=False)``（备源绝不落数据湖）。
    """
    notes: list[str] = []
    if df.empty:
        return df, ["空帧，跳过名义价回填"]
    if fetcher is None:
        from hexbroker.data.backup import BackupRawFetcher
        fetcher = BackupRawFetcher(root=None, save=False)

    start = df["datetime"].min().strftime("%Y-%m-%d")
    end = df["datetime"].max().strftime("%Y-%m-%d")
    try:
        pulls = fetcher.fetch_raw([sym0], start, end)
    except Exception as exc:  # noqa: BLE001 — 备源失败必须降级，不炸管线
        notes.append(f"回填失败（raw_close 仍为 adj 复制品）："
                     f"{type(exc).__name__}: {exc}")
        return df, notes

    pull = pulls.get(sym0)
    if pull is None or pull.close is None or len(pull.close) == 0:
        notes.append(f"回填失败：备源返回空（source={pull.source if pull else 'unknown'}）")
        return df, notes

    nom = pd.to_numeric(pull.close, errors="coerce").dropna()
    nom.index = pd.to_datetime(nom.index).normalize()
    nom = nom[~nom.index.duplicated(keep="last")]
    aligned = df["datetime"].map(nom)
    coverage = float(aligned.notna().mean()) if len(df) else 0.0
    if coverage < min_coverage:
        notes.append(f"回填失败：备源 {pull.source} 覆盖率 {coverage:.1%} < "
                     f"{min_coverage:.0%}（raw_close 仍为 adj 复制品）")
        return df, notes

    out = df.copy()
    matched = int(aligned.notna().sum())
    # 未对齐行保留原值（adj 复制品），避免 NaN 经 coerce_schema fillna(0.0) 污染
    out["raw_close"] = aligned.astype(float).fillna(df["raw_close"]).to_numpy()
    extra = f"；备源提示 {' | '.join(pull.warnings)}" if pull.warnings else ""
    if matched < len(out):
        notes.append(f"回填成功：来源 {pull.source}，对齐 {matched}/{len(out)} 行"
                     f"（{coverage:.1%}，未对齐行保留 adj 复制品）{extra}")
    else:
        notes.append(f"回填成功：来源 {pull.source}，"
                     f"覆盖 {matched}/{len(out)} 行（{coverage:.1%}）{extra}")
    return out, notes


def load_applied() -> list[dict[str, object]]:
    if APPLIED_PATH.exists():
        try:
            data = json.loads(APPLIED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
    return []


def save_applied(records: list[dict[str, object]]) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    APPLIED_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def backup_year_file(year_path: Path) -> Optional[Path]:
    """写回前备份原分片 → artifacts/backup_p64/{sym0}_{year}_prefill_{ts}.parquet。"""
    if not year_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    sym0 = year_path.parent.parent.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"{sym0}_{year_path.stem}_prefill_{ts}.parquet"
    shutil.copy2(year_path, dest)
    return dest


def compute_overlap_ratio(old: pd.DataFrame, new: pd.DataFrame) -> float:
    """重叠日期 close 最大相对偏差（旧 vs 新，新为准）。"""
    if old is None or old.empty or new is None or new.empty:
        return 0.0
    old_dt = pd.to_datetime(old["datetime"]).dt.normalize()
    new_dt = pd.to_datetime(new["datetime"]).dt.normalize()
    old_map = dict(zip(old_dt, pd.to_numeric(old["close"], errors="coerce")))
    ratios = []
    for dt_, nc in zip(new_dt, pd.to_numeric(new["close"], errors="coerce")):
        if dt_ in old_map:
            oc = old_map[dt_]
            if oc and pd.notna(oc) and pd.notna(nc):
                ratios.append(abs(nc - oc) / abs(oc))
    return max(ratios) if ratios else 0.0


def stage_parse(persisted: str, sym_arg: str, seg: str,
                force: bool = False, dry_run: bool = False,
                skip_nominal: bool = False) -> int:
    """解析单次拉取结果并合并写回（写前备份；幂等；--dry-run 不写盘）。"""
    underlying, sym0 = normalize_sym_arg(sym_arg)
    seg_start, seg_end = parse_seg_label(seg)
    path = Path(persisted)
    if not path.exists():
        print(f"[FAIL] 持久化文件不存在: {path}")
        return 1

    # 幂等：已应用段跳过（--force 覆盖）
    applied = load_applied()
    if any(a.get("sym0") == sym0 and a.get("seg") == seg for a in applied) and not force:
        print(f"[SKIP] {sym0} {seg} 已应用（artifacts/p6_4_applied.json）；"
              f"如需重跑请加 --force")
        return 0

    print(f"[PARSE] {sym0} ({underlying}) seg={seg} file={path.name} "
          f"{'[DRY-RUN]' if dry_run else ''}")
    df = load_persisted_rows(path)
    print(f"[PARSE] 持久化文件共 {len(df)} 行，列: {list(df.columns)[:12]}...")

    new_df = normalize_new_df(df, underlying, sym0, seg_start, seg_end)
    if new_df.empty:
        print(f"[WARN] 无 {underlying} 在 {seg_start}~{seg_end} 的数据，不写盘、不记录")
        return 0

    # P1-c：名义价回填 raw_close（失败大声降级，不阻断管线；静默即事故）
    if skip_nominal:
        print(f"[NOMINAL] {sym0}: 已跳过（--skip-nominal），raw_close 仍为 adj 复制品")
    else:
        new_df, nominal_notes = enrich_raw_close(new_df, sym0)
        for note in nominal_notes:
            tag = "[NOMINAL]" if "回填成功" in note else "[WARN][NOMINAL]"
            print(f"{tag} {sym0}: {note}")

    # 与计划核对（可选）
    if PLAN_PATH.exists():
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        planned = [s for s in plan.get("segments", [])
                   if s.get("sym0") == sym0 and s.get("seg_label") == seg]
        if planned:
            exp = planned[0].get("expected_days")
            print(f"[PARSE] 计划预期 {exp} 天，本次解析 {len(new_df)} 行")
            if len(new_df) < int(exp or 0):
                print("[WARN] 行数 < 预期，可能被网关截断；可重扫 plan 得到剩余子段")
        else:
            print(f"[WARN] 计划中未找到 {sym0} {seg}（仍按传入区间处理）")

    years = sorted(new_df["datetime"].dt.year.unique())
    print(f"[PARSE] 覆盖年份: {years}，日期 {new_df['datetime'].min().date()} "
          f"~ {new_df['datetime'].max().date()}，行数 {len(new_df)}")

    if dry_run:
        print("[DRY-RUN] 不写盘。预览:")
        print(new_df.head(3).to_string(index=False))
        return 0

    # 写回（按年分片）
    records: list[dict[str, object]] = []
    for year in years:
        year_path = PROCESSED_DIR / sym0 / "1d" / f"{year}.parquet"
        backup = backup_year_file(year_path)
        if year_path.exists():
            old = pd.read_parquet(year_path)
        else:
            old = pd.DataFrame(columns=SCHEMA_COLUMNS)
        old = coerce_schema(old)
        new_year = coerce_schema(
            new_df[new_df["datetime"].dt.year == year].copy()
        )
        overlap_ratio = compute_overlap_ratio(old, new_year)
        merged = pd.concat([old, new_year], ignore_index=True)
        merged = coerce_schema(merged)
        merged = (
            merged.drop_duplicates(subset="datetime", keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        # 防御：写临时文件后原子替换，避免写一半损坏
        year_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = year_path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(year_path)
        rows_before = len(old)
        records.append({
            "sym0": sym0,
            "underlying_symbol": underlying,
            "seg": seg,
            "persisted_file": str(path),
            "year": int(year),
            "year_file": str(year_path),
            "backup": str(backup) if backup else None,
            "rows_before": rows_before,
            "rows_after": int(len(merged)),
            "rows_added": int(len(merged) - len(old.drop_duplicates(subset="datetime", keep="last"))),
            "overlap_max_close_ratio": round(overlap_ratio, 6),
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        })
        try:
            rel = year_path.relative_to(PROJECT_ROOT)
        except ValueError:
            rel = year_path
        print(f"[MERGE] {rel}: "
              f"{rows_before} → {len(merged)} 行 "
              f"(备份 {backup.name if backup else '无'})")

    applied.extend(records)
    save_applied(applied)
    print(f"[OK] {sym0} {seg} 已合并 {len(years)} 个年度分片")
    return 0


# --------------------------------------------------------------------------- #
# verify 阶段
# --------------------------------------------------------------------------- #
def stage_verify(plan_path: Optional[Path]) -> int:
    """重扫缺口、输出闭合率、校验重叠日 close 比 <1%（只读）。"""
    frames = load_symbol_frames()
    if not frames:
        print("[FAIL] 未发现任何品种数据")
        return 1
    calendar, cal_meta = build_calendar(frames)
    window_start = pd.Timestamp(TARGET_END_DATE - relativedelta(years=PULL_WINDOW_YEARS)
                                + timedelta(days=1))

    plan: Optional[dict] = None
    if plan_path is not None and plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    elif PLAN_PATH.exists():
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))

    print(f"[VERIFY] 交易日历: {cal_meta['days']} 天 "
          f"({cal_meta['start']} ~ {cal_meta['end']})")

    # 1) 每品种剩余缺口（窗口内）
    print("\n[VERIFY] 剩余缺口（窗口内）：")
    remaining_total = 0
    for sym in sorted(frames):
        underlying = SYMBOL_MAP.get(sym, "?")
        ds = set(frames[sym]["datetime"])
        earliest = min(ds)
        regions = find_gap_regions(ds, calendar, earliest)
        in_win = [(s, e, n) for s, e, n in regions
                  if e.date() >= window_start.date()]
        n_win = sum(n for _, _, n in in_win)
        remaining_total += n_win
        if in_win:
            desc = "; ".join(f"{s.date()}~{e.date()}({n})" for s, e, n in in_win)
            print(f"  {sym} ({underlying}): {n_win}d  [{desc}]")
    print(f"  -- 窗口内剩余合计: {remaining_total}d")

    # 2) 闭合率（对照计划）
    if plan:
        segs = plan.get("segments", [])
        total_planned = sum(int(s.get("expected_days", 0)) for s in segs)
        filled = 0
        print(f"\n[VERIFY] 闭合率（对照 {Path(plan_path).name if plan_path else 'p6_4_pull_plan.json'}）:")
        for s in segs:
            sym0 = s["sym0"]
            if sym0 not in frames:
                print(f"  [WARN] {sym0}: 数据目录缺失，跳过该段")
                continue
            c0, c1 = pd.Timestamp(s["start_date"]), pd.Timestamp(s["end_date"])
            ds = set(frames[sym0]["datetime"])
            missing_in_seg = sum(1 for d in calendar if c0 <= d <= c1 and d not in ds)
            seg_filled = int(s.get("expected_days", 0)) - missing_in_seg
            filled += max(seg_filled, 0)
            if missing_in_seg:
                print(f"  {sym0} {s['seg_label']}: 已补 {seg_filled}/{s.get('expected_days')} "
                      f"(剩 {missing_in_seg})")
        closure = (filled / total_planned * 100.0) if total_planned else 100.0
        print(f"  -- 闭合率: {filled}/{total_planned} = {closure:.2f}%")
    else:
        closure = 0.0
        print("[VERIFY] 无计划文件，跳过闭合率计算")

    # 3) 拼接连续性：备份 vs 当前，重叠日 close 最大偏差
    print("\n[VERIFY] 拼接连续性（备份 vs 当前，重叠日 close 偏差 <1%）：")
    backups = sorted(BACKUP_DIR.glob("*.parquet"))
    max_ratio_all = 0.0
    issues = 0
    if not backups:
        print("  -- 无备份（尚未执行 parse 合并）")
    for bf in backups:
        # 文件名 {sym0}_{year}_prefill_{ts}.parquet
        try:
            stem = bf.stem
            sym0 = stem.split("_")[0]
            year = stem.split("_")[1]
        except IndexError:
            continue
        cur_path = PROCESSED_DIR / sym0 / "1d" / f"{year}.parquet"
        if not cur_path.exists():
            print(f"  [WARN] {bf.name}: 当前分片缺失，跳过")
            continue
        try:
            old = coerce_schema(pd.read_parquet(bf))
            cur = coerce_schema(pd.read_parquet(cur_path))
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] {bf.name}: 读取失败 {exc}")
            continue
        ratio = compute_overlap_ratio(old, cur)
        max_ratio_all = max(max_ratio_all, ratio)
        flag = "OK" if ratio < OVERLAP_TOLERANCE else "!!"
        if ratio >= OVERLAP_TOLERANCE:
            issues += 1
        print(f"  {bf.name}: max_close_ratio={ratio:.4%} [{flag}]")
    if issues:
        print(f"  -- {issues} 个备份存在 >=1% 偏差，需人工核对")
    else:
        print("  -- 全部重叠日 close 偏差 <1%")

    ok = remaining_total == 0 and issues == 0
    print(f"\n[VERIFY] 结论: {'PASS' if ok else 'INCOMPLETE'}"
          f"（剩余窗口内缺口 {remaining_total}d，重叠偏差 {issues} 处）")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="P6-4 数据缺口补齐：plan / parse / verify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stage", required=True,
                    choices=["plan", "parse", "verify"],
                    help="plan=扫描缺口生成拉取计划; parse=解析拉取结果合并写回; verify=校验")
    ap.add_argument("persisted", nargs="?", type=str, default=None,
                    help="parse 阶段：持久化 JSON 文件路径")
    ap.add_argument("--sym", type=str, default=None,
                    help="parse 阶段：品种（AU / AU0 / au0 / m0 / M）")
    ap.add_argument("--seg", type=str, default=None,
                    help="parse 阶段：段标签 YYYYMMDD_YYYYMMDD（与 plan 一致）")
    ap.add_argument("--force", action="store_true",
                    help="parse 阶段：忽略 applied 记录强制重跑")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse 阶段：只解析预览，不写盘")
    ap.add_argument("--skip-nominal", action="store_true",
                    help="parse 阶段：跳过 P1-c 名义价回填（离线场景）,"
                         "raw_close 将保持 adj 复制品并打印 WARN")
    ap.add_argument("--plan", type=str, default=None,
                    help="verify 阶段：指定计划 JSON 路径（默认 artifacts/p6_4_pull_plan.json）")
    args = ap.parse_args()

    if args.stage == "plan":
        return stage_plan()
    if args.stage == "parse":
        if not args.persisted or not args.sym or not args.seg:
            print("[FAIL] parse 需要: <持久化文件> --sym XX --seg YYYYMMDD_YYYYMMDD")
            return 2
        try:
            return stage_parse(args.persisted, args.sym, args.seg,
                               force=args.force, dry_run=args.dry_run,
                               skip_nominal=args.skip_nominal)
        except (ValueError, KeyError) as exc:
            print(f"[FAIL] {exc}")
            return 2
    if args.stage == "verify":
        plan_path = Path(args.plan) if args.plan else None
        return stage_verify(plan_path)
    return 2


if __name__ == "__main__":
    sys.exit(main())
