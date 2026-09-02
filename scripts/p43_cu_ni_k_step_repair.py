"""P-NEW 处置驱动器：cu0/ni0 湖内「k 伪台阶」后复权价定点修复（2026-09-02）。

缺陷（定罪报告：``deliverables/cu_ni_k_step_forensics_20260902.md``）
------------------------------------------------------------------
换月领先窗口期内，tqsdk 主路经「``seam∈tail`` + ``ext`` 非空」路径排放了
**旧约 OHLC × 当时 k 锚** 的 bar，而 ``raw_close`` 由 sina enrich 回填为
**正确的新约名义价** → ``k = adj_close / raw_close`` 出现既非前段真值亦非
后段真值的**幽灵台阶**，破坏「k 段内恒定、仅换月日跳变」不变量。

定罪已由双独立源交叉完成（sina 名义价证 raw 侧洁净 / pandadata
``close_pcr`` 证 close 侧污染），**本脚本不做定罪，只执行修复**。

污染范围（报告 §二，双独立源交叉确认）
--------------------------------------
===================  =======  ==========================================
文件                 日期      列
===================  =======  ==========================================
cu0/1d/2026.parquet 0821/0824 open, high, low, close, adj_close
ni0/1d/2026.parquet 0820/0821/0824 open, high, low, close, adj_close
===================  =======  ==========================================

``raw_close`` / ``volume`` / ``amount`` / ``open_interest`` **无污染，绝不动**。

修复原理（权威值覆盖，非插值、非反推）
--------------------------------------
修复值 = pandadata ``get_future_daily_post(method="close_pcr")`` 返回的后复权
OHLC（与湖 close 的**生产权威同源**，见报告 §1.3），``round(x, 2)`` 对齐湖精度；
``adj_close`` 写为与 ``close`` 相同的值（口径见 ``.workbuddy/memory/MEMORY.md``
第一节：``adj_close = k × raw_close``，湖内既有行 ``adj_close == close`` 恒成立）。

修复后 k 不变量应恢复为两段式：
cu0 ``1.472692 → 1.475842`` @0821；ni0 ``1.228266 → 1.225704`` @0820。

护栏（G0~G5 全开，任一不过即 abort 且不写盘）
---------------------------------------------
- **G0 备份**：修复前把两个 parquet 复制到 ``artifacts/_p2_backup_<YYYYMMDD>/``，
  并校验「备份」与「原档」逐位一致（sha256 相等），sha256 记入台账；
- **G1 旧行精确断言**：改前断言目标行**全部列**当前值 == 报告记录的湖内污染值
  （浮点 ≤1e-6 相对/绝对容差，非浮点严格相等）。缺一即 assert 失败，绝不写入；
- **G2 其余行逐位不变**：除目标行外，其余所有行的**所有列**逐位不变
  （``np.array_equal`` 精确比较，非 ``allclose``，非抽样）；
- **G3 结构不变**：行数、列集合、列顺序、每列 dtype、索引全等；
- **G4 修复后 k 不变量复核**：重算 ``adj_close / raw_close``，按 1e-6 相对跳变
  切段，断言每品种恰好 2 段、段内恒定（≤1e-6）、且后段 k 回到报告期望值；
- **G5 原子写**：``tmp = src.with_suffix(".parquet.tmp")`` → ``os.replace(tmp, src)``。
  ⛔ 严禁 ``Path.unlink()`` / ``os.remove()`` / 直接覆盖原档（沙箱 safe-delete
  钩子会拦截 unlink 并把文件搬进回收站，直接覆盖会丢文件）。

  .. note:: 报告 §二与 MEMORY.md 第三节原文写的是 ``shutil.move``。此处用
     ``os.replace``：**Windows 上 ``os.rename`` 对已存在的目标必然失败**，
     ``shutil.move`` 会退化为 ``copy2`` + ``os.unlink(src)`` —— 退化为**非原子**
     覆盖，且 ``os.unlink`` 会触发本沙箱钩子。``os.replace`` 是同目录内的原子
     rename，语义与 G5 意图（先落 tmp、再原子替换、绝不 unlink 原档）一致，
     且与项目既有 ``hexbroker.utils.io._atomic_write`` 同实现。

用法
----
默认 **dry-run**（零写盘）：:

    python scripts/p43_cu_ni_k_step_repair.py

确认全部断言通过后落盘::

    python scripts/p43_cu_ni_k_step_repair.py --apply

幂等性：修复后目标行的 k 已回到段内真值，重跑时 **G1 会在第一行就 assert 失败**
（当前值已不再是污染值）→ 脚本拒绝重复修复，不会二次改写。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "raw"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "artifacts" / "_p2_backup_20260902"
SIDECAR_NAME = "_RAW_CLOSE_REPAIRS.json"

if str(PROJECT_ROOT) not in sys.path:  # 支持直接 ``python scripts/p43_...py`` 执行
    sys.path.insert(0, str(PROJECT_ROOT))

from hexbroker.utils.io import write_json  # noqa: E402  (需先补 sys.path)

#: 浮点比较容差（G1 旧值断言 / G4 k 不变量）
FLOAT_TOL = 1e-6
#: k 分段判定阈值（相对跳变超过该值即视为换月切段）
K_SEGMENT_TOL = 1e-6
#: 待修列（顺序即写入顺序）
REPAIR_COLS = ("open", "high", "low", "close", "adj_close")

#: ---------------------------------------------------------------------------
#: 修复计划表（权威值来源：pandadata get_future_daily_post(method="close_pcr")，
#: 2026-09-02 实取；与定罪报告 §1.3「pandadata close（权威）」列逐位一致，
#: 且与报告「全 OHLC 列偏差矩阵」15/15 项偏差逐项吻合 —— 见 verify 输出）
#: ---------------------------------------------------------------------------
REPAIR_PLAN: dict[str, list[dict[str, Any]]] = {
    "cu0": [
        {
            "date": "2026-08-21",
            # G1：改前必须观测到的湖内污染值（报告 §1.3「湖 close」+ 实测全列留痕）
            "before": {
                "symbol": "cu0",
                "open": 157442.837711,
                "high": 159243.365102,
                "low": 157250.978235,
                "close": 159021.988783,
                "volume": 73275.0,
                "amount": 0.0,
                "open_interest": 162899.0,
                "raw_close": 107520.0,
                "adj_close": 159021.988783,
                "limit_up": False,
                "limit_down": False,
                "is_rollover": False,
            },
            # pandadata close_pcr 权威 OHLC（未取整，便于留痕可追溯）
            "authority": {
                "open": 157077.31099601954,
                "high": 158903.4488699654,
                "low": 156915.31489429853,
                "close": 158682.5450948913,
            },
        },
        {
            "date": "2026-08-24",
            "before": {
                "symbol": "cu0",
                "open": 159627.084054,
                "high": 159804.185109,
                "low": 158771.095622,
                "close": 159553.291948,
                "volume": 74677.0,
                "amount": 0.0,
                "open_interest": 150334.0,
                "raw_close": 107910.0,
                "adj_close": 159553.291948,
                "limit_up": False,
                "limit_down": False,
                "is_rollover": False,
            },
            "authority": {
                "open": 159405.70773529768,
                "high": 159464.74142022882,
                "low": 158490.6856188651,
                "close": 159258.12352296984,
            },
        },
    ],
    "ni0": [
        {
            "date": "2026-08-20",
            "before": {
                "symbol": "ni0",
                "open": 158140.389781,
                "high": 160150.5451,
                "low": 157895.248889,
                "close": 158348.75954,
                "volume": 198463.0,
                "amount": 0.0,
                "open_interest": 118724.0,
                "raw_close": 129460.0,
                "adj_close": 158348.75954,
                "limit_up": False,
                "limit_down": False,
                "is_rollover": False,
            },
            "authority": {
                "open": 158470.89450468138,
                "high": 160485.250937697,
                "low": 158225.24128114287,
                "close": 158679.6997446891,
            },
        },
        {
            "date": "2026-08-21",
            "before": {
                "symbol": "ni0",
                "open": 157380.453014,
                "high": 158581.643388,
                "low": 156620.516247,
                "close": 158042.333424,
                "volume": 120313.0,
                "amount": 0.0,
                "open_interest": 100021.0,
                "raw_close": 129200.0,
                "adj_close": 158042.333424,
                "limit_up": False,
                "limit_down": False,
                "is_rollover": False,
            },
            "authority": {
                "open": 157968.7911563072,
                "high": 158888.06950335274,
                "low": 156975.970541498,
                "close": 158361.01658437998,
            },
        },
        {
            "date": "2026-08-24",
            "before": {
                "symbol": "ni0",
                "open": 158532.615209,
                "high": 160812.42551,
                "low": 158017.819335,
                "close": 158839.041325,
                "volume": 152615.0,
                "amount": 0.0,
                "open_interest": 91631.0,
                "raw_close": 129860.0,
                "adj_close": 158839.041325,
                "limit_up": False,
                "limit_down": False,
                "is_rollover": False,
            },
            "authority": {
                "open": 158667.44270006183,
                "high": 161155.62275939845,
                "low": 158397.7877182618,
                "close": 159169.98152978005,
            },
        },
    ],
}

#: G4 k 不变量复核的**取证窗口**（定罪报告 §1.3 双独立源交叉比对的区间
#: 0814~0901；报告「13/13 逐位一致」即指该窗口的 13 个交易日）
K_WINDOW_START = "2026-08-14"
K_WINDOW_END = "2026-09-01"

#: G4 期望 k 值（报告 §1.3「权威隐含 k」列，6 位小数；容差 ≤1e-6）
EXPECTED_K_AFTER: dict[str, float] = {
    "cu0": 1.475842,
    "ni0": 1.225704,
}
#: G4 期望切换日（报告 §1.5：修复后 k 序列恢复两段式的切换点）
EXPECTED_K_SWITCH_DATE: dict[str, str] = {
    "cu0": "2026-08-21",
    "ni0": "2026-08-20",
}


# ===========================================================================
# 工具函数
# ===========================================================================
def sha256_of(path: Path) -> str:
    """计算文件 sha256（分块读取，避免大文件占内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _values_equal(a: Any, b: Any, tol: float = FLOAT_TOL) -> bool:
    """单值比较：浮点用相对/绝对容差，其余类型严格相等。"""
    if isinstance(a, (bool, np.bool_)) or isinstance(b, (bool, np.bool_)):
        return bool(a) is bool(b) or bool(a) == bool(b)
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        a_f, b_f = float(a), float(b)
        if a_f == b_f:
            return True
        return abs(a_f - b_f) <= tol or abs(a_f - b_f) <= tol * max(abs(a_f), abs(b_f))
    return a == b


def _series_bitwise_equal(before: pd.Series, after: pd.Series) -> bool:
    """两列是否**逐位**相等（NaN 视同相等；不做任何容差/近似）。"""
    b_arr = before.to_numpy()
    a_arr = after.to_numpy()
    if b_arr.dtype != a_arr.dtype:
        return False
    if b_arr.dtype.kind == "f":
        return bool(np.array_equal(b_arr, a_arr, equal_nan=True))
    return bool(np.array_equal(b_arr, a_arr))


def _partition_path(data_root: Path, sym0: str, year: int) -> Path:
    """年度分区路径（MEMORY.md：必须走 ``<sym>/1d/<year>.parquet``）。"""
    return data_root / "processed" / sym0 / "1d" / f"{year}.parquet"


def _k_series(df: pd.DataFrame) -> pd.Series:
    """``k = adj_close / raw_close``，以 ``datetime`` 为索引（便于按日期切段）。"""
    k = df["adj_close"].astype(float) / df["raw_close"].astype(float)
    return pd.Series(k.to_numpy(dtype=float),
                     index=pd.DatetimeIndex(df["datetime"]), name="k")


def _k_segments(k: pd.Series, tol: float = K_SEGMENT_TOL) -> list[dict[str, Any]]:
    """把 k 序列按「相对跳变 > tol」切成若干恒定段。

    返回每段的 ``start`` / ``end`` / ``median`` / ``max_dev``（段内相对中位数
    的最大偏离）/ ``n``。
    """
    k = k.dropna().sort_index()
    if k.empty:
        return []
    bounds: list[int] = [0]
    vals = k.to_numpy(dtype=float)
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if prev == 0 or abs(vals[i] / prev - 1.0) > tol:
            bounds.append(i)
    bounds.append(len(vals))
    out: list[dict[str, Any]] = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        seg = vals[s:e]
        med = float(np.median(seg))
        max_dev = float(np.max(np.abs(seg - med))) / abs(med) if med else float("inf")
        out.append({
            "start": str(pd.Timestamp(k.index[s]).date()),
            "end": str(pd.Timestamp(k.index[e - 1]).date()),
            "n": int(e - s),
            "median": med,
            "max_dev": max_dev,
        })
    return out


# ===========================================================================
# G0 备份
# ===========================================================================
def g0_backup(src: Path, backup_dir: Path) -> dict[str, Any]:
    """G0：复制备份 + sha256 逐位一致性校验。

    ``shutil.copy2`` 只写目标、不删除源，绝不触发 safe-delete 钩子。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{src.parent.parent.name}_1d_{src.stem}.parquet"
    src_sha = sha256_of(src)
    shutil.copy2(src, dst)
    dst_sha = sha256_of(dst)
    if src_sha != dst_sha:
        raise AssertionError(
            f"G0 失败：备份与原档 sha256 不一致\n"
            f"  src={src} {src_sha}\n  dst={dst} {dst_sha}"
        )
    print(f"    [G0] 备份 OK {dst.name}  sha256={dst_sha}")
    print(f"    [G0] 原档 sha256 逐位一致 ✓（{src.stat().st_size} bytes）")
    return {"path": str(dst), "sha256": dst_sha,
            "src_sha256": src_sha, "bytes": int(src.stat().st_size)}


# ===========================================================================
# G1 旧行精确断言
# ===========================================================================
def g1_assert_before(df: pd.DataFrame, sym0: str, spec: dict[str, Any]) -> int:
    """G1：目标行**全部列**当前值必须 == 报告记录的湖内污染值。返回行索引。"""
    date = pd.Timestamp(spec["date"])
    hits = df.index[df["datetime"] == date]
    if len(hits) != 1:
        raise AssertionError(
            f"G1 失败：{sym0} {spec['date']} 命中 {len(hits)} 行（期望 1）")
    idx = int(hits[0])
    row = df.loc[idx]
    if str(row["symbol"]) != sym0:
        raise AssertionError(
            f"G1 失败：{sym0} {spec['date']} symbol={row['symbol']!r} 不符")

    before = spec["before"]
    # 列集合必须完全覆盖（除 datetime 单独校验）
    expected_cols = set(before) | {"datetime"}
    missing = expected_cols - set(df.columns)
    if missing:
        raise AssertionError(f"G1 失败：{sym0} 缺少列 {sorted(missing)}")

    if pd.Timestamp(row["datetime"]) != date:
        raise AssertionError(f"G1 失败：{sym0} datetime 不匹配")

    mismatches: list[str] = []
    for col, want in before.items():
        got = row[col]
        if not _values_equal(got, want):
            mismatches.append(f"{col}: got={got!r} want={want!r}")
    if mismatches:
        raise AssertionError(
            f"G1 失败：{sym0} {spec['date']} 旧值不匹配（{len(mismatches)} 列）\n      "
            + "\n      ".join(mismatches))

    # 附加不变量：adj_close 必须与 close 逐位相同（污染是同步的）
    if float(row["adj_close"]) != float(row["close"]):
        raise AssertionError(
            f"G1 失败：{sym0} {spec['date']} adj_close != close "
            f"（{row['adj_close']!r} vs {row['close']!r}）")
    print(f"    [G1] {sym0} {spec['date']} 全 {len(before) + 1} 列旧值精确匹配 ✓")
    return idx


# ===========================================================================
# G2 / G3 结构 & 其余行不变
# ===========================================================================
def g2_g3_verify(before: pd.DataFrame, after: pd.DataFrame,
                 sym0: str, touched_idx: list[int]) -> dict[str, Any]:
    """G2 其余行逐位不变 + G3 结构不变。"""
    # ---- G3 结构不变 ----
    if list(before.columns) != list(after.columns):
        raise AssertionError(
            f"G3 失败：{sym0} 列集合/顺序变化\n  before={list(before.columns)}\n"
            f"  after ={list(after.columns)}")
    if len(before) != len(after):
        raise AssertionError(
            f"G3 失败：{sym0} 行数 {len(before)} → {len(after)}")
    if not before.index.equals(after.index):
        raise AssertionError(f"G3 失败：{sym0} 索引变化")
    dtype_diff = {c: (str(before[c].dtype), str(after[c].dtype))
                  for c in before.columns
                  if before[c].dtype != after[c].dtype}
    if dtype_diff:
        raise AssertionError(f"G3 失败：{sym0} dtype 变化 {dtype_diff}")
    print(f"    [G3] {sym0} 结构不变 ✓ "
          f"rows={len(after)} cols={len(after.columns)} "
          f"dtype 全 {len(after.columns)} 列一致 index 全等")

    # ---- G2 其余行逐位不变（全列、非抽样） ----
    other_mask = ~before.index.isin(touched_idx)
    n_other = int(other_mask.sum())
    bad: list[str] = []
    for col in before.columns:
        if not _series_bitwise_equal(before.loc[other_mask, col],
                                     after.loc[other_mask, col]):
            diff_n = int((before.loc[other_mask, col].to_numpy()
                          != after.loc[other_mask, col].to_numpy()).sum())
            bad.append(f"{col}({diff_n} 行不同)")
    if bad:
        raise AssertionError(
            f"G2 失败：{sym0} 非目标行被改动 → {bad}")
    print(f"    [G2] {sym0} 其余 {n_other} 行 × {len(before.columns)} 列"
          f" 逐位不变 ✓（np.array_equal 精确比较，非抽样）")
    return {"rows": int(len(after)), "cols": int(len(after.columns)),
            "untouched_rows": n_other,
            "untouched_cells": n_other * len(after.columns)}


# ===========================================================================
# G4 k 不变量复核
# ===========================================================================
def g4_verify_k(df: pd.DataFrame, sym0: str,
                expected_before_k: float | None) -> dict[str, Any]:
    """G4：修复后 ``adj_close / raw_close`` 段内恒定且回到报告期望值。

    .. important:: 切段**只统计取证窗口** :data:`K_WINDOW_START` ~
       :data:`K_WINDOW_END`（定罪报告 §1.3 交叉比对的 0814~0901 区间）。
       全年度分区本身含**每月换月**产生的多个 k 段（2026 年 1–9 月共 12 段），
       整年断言「恰好 2 段」是错误命题；而窗口外的行由 G2 保证逐位不变，
       其 k 自然不变，故窗口内外合起来已覆盖全部不变量。
    """
    k_all = _k_series(df)
    k = k_all.loc[K_WINDOW_START:K_WINDOW_END]
    if k.empty:
        raise AssertionError(
            f"G4 失败：{sym0} 取证窗口 {K_WINDOW_START}~{K_WINDOW_END} 内无数据")
    segs = _k_segments(k)
    print(f"    [G4] {sym0} k 分段（取证窗口 {K_WINDOW_START}~{K_WINDOW_END}，"
          f"n={len(k)}）：")
    for s in segs:
        print(f"          {s['start']} ~ {s['end']}  n={s['n']:3d}  "
              f"k={s['median']:.9f}  段内最大相对偏离={s['max_dev']:.3e}")

    if len(segs) != 2:
        raise AssertionError(
            f"G4 失败：{sym0} 窗口内 k 段数 = {len(segs)}（期望 2）→ {segs}")
    for s in segs:
        if s["max_dev"] > FLOAT_TOL:
            raise AssertionError(
                f"G4 失败：{sym0} 段 {s['start']}~{s['end']} 段内不恒定 "
                f"（最大相对偏离 {s['max_dev']:.3e} > {FLOAT_TOL:g}）")

    # 窗口必须被两段完整覆盖（首尾不留残段）
    if segs[0]["start"] != K_WINDOW_START or segs[-1]["end"] != K_WINDOW_END:
        raise AssertionError(
            f"G4 失败：{sym0} 分段未完整覆盖取证窗口 "
            f"[{segs[0]['start']}, {segs[-1]['end']}]")

    switch = segs[1]["start"]
    want_switch = EXPECTED_K_SWITCH_DATE[sym0]
    if switch != want_switch:
        raise AssertionError(
            f"G4 失败：{sym0} k 切换日 {switch} != 报告期望 {want_switch}")

    want_k = EXPECTED_K_AFTER[sym0]
    got_k = segs[1]["median"]
    if abs(got_k - want_k) > FLOAT_TOL:
        raise AssertionError(
            f"G4 失败：{sym0} 后段 k={got_k:.9f} != 报告期望 {want_k} "
            f"（容差 {FLOAT_TOL:g}）")

    if expected_before_k is not None:
        got_prev = segs[0]["median"]
        if abs(got_prev - expected_before_k) > FLOAT_TOL:
            raise AssertionError(
                f"G4 失败：{sym0} 前段 k={got_prev:.9f} != 修复前观测值 "
                f"{expected_before_k}（前段不应被本次修复影响）")

    print(f"    [G4] {sym0} k 不变量恢复 ✓  前段 {segs[0]['median']:.9f} → "
          f"后段 {got_k:.9f} @{switch}（期望 {want_k}，偏差 "
          f"{abs(got_k - want_k):.2e} ≤ {FLOAT_TOL:g}）")
    return {"segments": segs, "switch_date": switch, "k_after": got_k,
            "k_before": segs[0]["median"],
            "window": [K_WINDOW_START, K_WINDOW_END],
            "window_n": int(len(k)),
            "k_series_head": {str(d.date()): float(v)
                              for d, v in k.items()}}


# ===========================================================================
# G5 原子写
# ===========================================================================
def g5_atomic_write_parquet(df: pd.DataFrame, src: Path) -> None:
    """G5：先落 ``.parquet.tmp`` 再原子替换。绝不 unlink / 直接覆盖原档。

    全程只用 ``os.replace``（同目录原子 rename）：
      * 残留 tmp 改名 ``.parquet.stale`` 用 ``os.replace`` —— 不用 ``shutil.move``，
        它在 Windows 上会退化为 ``copy2(src→dst) + os.unlink(src)``，非原子且
        触发沙箱 safe-delete 钩子（与 G5 铁律自相矛盾，QA 复核 O3 提出）；
      * 正式落盘同样用 ``os.replace(tmp, src)``（见下）。
    """
    tmp = src.with_suffix(".parquet.tmp")
    if tmp.exists():  # 残留 tmp（上次异常中断）→ 先移开，不 unlink
        stale = tmp.with_suffix(".parquet.stale")
        os.replace(str(tmp), str(stale))
        print(f"    [G5] 发现残留 tmp，已改名为 {stale.name}")
    df.to_parquet(tmp)
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise AssertionError(f"G5 失败：tmp 写入异常 {tmp}")
    # os.replace = 同目录原子 rename；Windows 上 shutil.move 会退化为
    # copy2 + os.unlink(src)（非原子且触发沙箱钩子），故此处不用 shutil.move。
    os.replace(tmp, src)
    if tmp.exists():
        raise AssertionError(f"G5 失败：tmp 未被替换掉 {tmp}")
    print(f"    [G5] 原子写 OK → {src.name}（{src.stat().st_size} bytes）")


# ===========================================================================
# 主流程
# ===========================================================================
def prepare_symbol(data_root: Path, backup_dir: Path, sym0: str,
                   apply: bool) -> dict[str, Any]:
    """单品种**准备阶段**：读取 → G1 → 内存改 → G2/G2b/G3/G4 →（apply 时）G0 备份。

    返回待提交结果（含内存中的 ``df_after``）。**本函数绝不写分区**，
    保证台账可以先于数据落盘（写盘前留痕，符合 P 步留痕纪律）。
    """
    specs = REPAIR_PLAN[sym0]
    year = int(pd.Timestamp(specs[0]["date"]).year)
    src = _partition_path(data_root, sym0, year)
    if not src.exists():
        raise FileNotFoundError(f"分区不存在：{src}")

    print(f"\n  ── {sym0} ── {src.relative_to(PROJECT_ROOT).as_posix()}")
    df_before = pd.read_parquet(src)
    if "datetime" in df_before.columns:
        df_before["datetime"] = pd.to_datetime(df_before["datetime"])

    # 修复前窗口内 k 前段观测值（用于 G4 断言前段不受本次修复影响）
    k_pre = _k_series(df_before).loc[K_WINDOW_START:K_WINDOW_END]
    segs_pre = _k_segments(k_pre)
    if not segs_pre:
        raise AssertionError(f"{sym0} 取证窗口内无数据，无法建立 k 基线")
    expected_before_k = float(segs_pre[0]["median"])

    # ---- G1：旧值精确断言（改前） ----
    touched_idx: list[int] = []
    for spec in specs:
        touched_idx.append(g1_assert_before(df_before, sym0, spec))

    # ---- 内存应用修复 ----
    df_after = df_before.copy()
    changes: list[dict[str, Any]] = []
    for spec, idx in zip(specs, touched_idx):
        auth = spec["authority"]
        new_close = round(auth["close"], 2)
        row_change: dict[str, Any] = {
            "date": spec["date"],
            "raw_close": float(df_before.loc[idx, "raw_close"]),
            "columns": {},
        }
        for col in REPAIR_COLS:
            old = float(df_before.loc[idx, col])
            # adj_close 口径：与 close 相同（MEMORY.md 第一节）
            new = round(new_close if col == "adj_close" else auth[col], 2)
            df_after.loc[idx, col] = new
            row_change["columns"][col] = {
                "old": old, "new": new,
                "authority_raw": float(auth[col]) if col != "adj_close"
                else float(auth["close"]),
                "dev_pct": (old - float(auth[col] if col != "adj_close"
                                        else auth["close"]))
                / float(auth[col] if col != "adj_close" else auth["close"]) * 100.0,
            }
        row_change["k_before"] = float(
            df_before.loc[idx, "close"] / df_before.loc[idx, "raw_close"])
        row_change["k_after"] = float(new_close / row_change["raw_close"])
        changes.append(row_change)
        print(f"    [FIX] {sym0} {spec['date']}: "
              + " ".join(f"{c}={row_change['columns'][c]['old']:.2f}→"
                         f"{row_change['columns'][c]['new']:.2f}"
                         for c in REPAIR_COLS)
              + f"  k {row_change['k_before']:.9f}→{row_change['k_after']:.9f}")

    # ---- G2 / G3 ----
    struct = g2_g3_verify(df_before, df_after, sym0, touched_idx)

    # ---- G2b：取证窗口外的 k 也必须逐位不变（纵深防御；G2 已隐含，此处显式化） ----
    k_b, k_a = _k_series(df_before), _k_series(df_after)
    outside = (k_b.index < K_WINDOW_START) | (k_b.index > K_WINDOW_END)
    n_outside = int(outside.sum())
    if n_outside and not np.array_equal(k_b[outside].to_numpy(),
                                        k_a[outside].to_numpy(),
                                        equal_nan=True):
        n_diff = int((k_b[outside].to_numpy() != k_a[outside].to_numpy()).sum())
        raise AssertionError(
            f"G2b 失败：{sym0} 取证窗口外 {n_diff} 行 k 被改动")
    print(f"    [G2b] {sym0} 取证窗口外 {n_outside} 行 k 逐位不变 ✓")

    # ---- G4 ----
    k_info = g4_verify_k(df_after, sym0, expected_before_k)

    result: dict[str, Any] = {
        "sym0": sym0,
        "path": str(src),
        "src": src,
        "year": year,
        "rows_changed": len(touched_idx),
        "touched_idx": touched_idx,
        "changes": changes,
        "structure": struct,
        "k": k_info,
        "outside_k_rows": n_outside,
        "backup": None,
        "written": False,
        # 仅内存持有，不进台账（不可 JSON 序列化）
        "_df_before": df_before,
        "_df_after": df_after,
        "_expected_before_k": expected_before_k,
    }

    if not apply:
        print(f"    [DRY-RUN] {sym0} 未写盘（G0~G4 全部通过）")
        return result

    # ---- G0 备份（apply 时才做，避免 dry-run 产生垃圾备份） ----
    result["backup"] = g0_backup(src, backup_dir)
    print(f"    [PREPARE] {sym0} 就绪，等待台账留痕后落盘")
    return result


def commit_symbol(pending: dict[str, Any]) -> dict[str, Any]:
    """**提交阶段**：G5 原子写分区 + 落盘后从磁盘读回复核 G2/G3/G4。"""
    sym0 = pending["sym0"]
    src: Path = pending["src"]
    df_after: pd.DataFrame = pending["_df_after"]
    df_before: pd.DataFrame = pending["_df_before"]

    # ---- G5 原子写 ----
    g5_atomic_write_parquet(df_after, src)

    # ---- 落盘后复核：重新从磁盘读回，再跑一遍 G2/G3/G4 ----
    df_disk = pd.read_parquet(src)
    if "datetime" in df_disk.columns:
        df_disk["datetime"] = pd.to_datetime(df_disk["datetime"])
    g2_g3_verify(df_before, df_disk, sym0, pending["touched_idx"])
    g4_verify_k(df_disk, sym0, pending["_expected_before_k"])
    print(f"    [POST] {sym0} 落盘后从磁盘重新读回复核 G2/G3/G4 ✓")
    pending["written"] = True
    return pending


def append_sidecar(data_root: Path, results: list[dict[str, Any]],
                   backup_dir: Path) -> Path:
    """向 ``_RAW_CLOSE_REPAIRS.json`` **追加**一条台账（保留原有内容）。"""
    sidecar = data_root / "processed" / SIDECAR_NAME
    log: list[Any] = []
    if sidecar.exists():
        try:
            log = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log = []
    if not isinstance(log, list):
        log = []

    log.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": "p43_cu_ni_k_step_repair",
        "n_partitions": len(results),
        "n_rows": sum(r["rows_changed"] for r in results),
        "method": ("pandadata get_future_daily_post(method=close_pcr) 权威 OHLC "
                   "定点覆盖 5 行 × 5 列（open/high/low/close/adj_close），"
                   "round 2 位；raw_close/volume/amount/open_interest 未动"),
        "verdict": ("cu0/ni0 换月领先窗口 tqsdk「seam∈tail + ext 非空」路径排放"
                    "旧约 bar × 当时 k 锚，raw 侧由 sina 回填正确新约名义价 → "
                    "k 幽灵台阶；双独立源交叉定罪（sina 证 raw 洁净 / pandadata "
                    "证 close 污染）"),
        "evidence": {
            "forensics_report": "deliverables/cu_ni_k_step_forensics_20260902.md",
            "authority_source": "pandadata get_future_daily_post(method=close_pcr)",
            "authority_fetch_sha256": {
                "CU_20260814_20260901":
                    "2571965d9360015dc90407133e413c070c93666f92a938a0c1e51cf1cd03fa58",
                "NI_20260814_20260901":
                    "b6985abf28f477567001e85f4c70c96339e99f4475ee4264d974c6d050ef2a2e",
                "CU_NI_day_session_open_20260824_20260901":
                    "bcb63f9ec775f061348c9e9bb1073e9ad646403505845fa2b4cf32ec19ce4e1d",
            },
            "cross_check": ("权威 close 与报告 §1.3「pandadata close（权威）」5/5 "
                            "逐位一致；15/15 项 OHLC 偏差与报告「全 OHLC 列偏差矩阵」"
                            "逐项吻合"),
        },
        "guards": ["G0 备份+sha256 逐位校验", "G1 目标行全列旧值精确断言",
                   "G2 其余行全列逐位不变", "G3 结构不变（行数/列序/dtype/索引）",
                   "G4 修复后 k 段内恒定且回到报告期望值",
                   "G5 tmp + os.replace 原子写（不 unlink 原档）",
                   "落盘后从磁盘读回复核 G2/G3/G4"],
        "backup_dir": str(backup_dir),
        "rows": [
            {
                "sym": r["sym0"],
                "date": c["date"],
                "file": r["path"],
                "changed_columns": list(REPAIR_COLS),
                "untouched_columns": ["raw_close", "volume", "amount",
                                      "open_interest", "symbol", "datetime",
                                      "limit_up", "limit_down", "is_rollover"],
                "raw_close": c["raw_close"],
                "before": {k: v["old"] for k, v in c["columns"].items()},
                "after": {k: v["new"] for k, v in c["columns"].items()},
                "authority_pandadata": {k: v["authority_raw"]
                                        for k, v in c["columns"].items()},
                "dev_pct_vs_authority": {k: round(v["dev_pct"], 4)
                                         for k, v in c["columns"].items()},
                "k_before": c["k_before"],
                "k_after": c["k_after"],
                "backup": r["backup"],
                "reason": "k 伪台阶污染（旧约 OHLC × 当时 k 锚），后复权序列伪不连续",
            }
            for r in results for c in r["changes"]
        ],
        "post_check": {
            "k_segments": {r["sym0"]: r["k"]["segments"] for r in results},
            "structure": {r["sym0"]: r["structure"] for r in results},
        },
        "downstream": ("主湖已变，需重建 p22 信号缓存（受 P1-2 禁写窗口约束："
                       "⛔ 08:50–11:30 / 13:20–15:00 / 20:50–02:30；"
                       "✅ 02:30–08:50 / 11:30–13:20 / 15:00–20:50）"),
    })
    write_json(log, sidecar)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="cu0/ni0 湖内 k 伪台阶后复权价定点修复（P-NEW）")
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ap.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    ap.add_argument("--symbols", default="cu0,ni0",
                    help="待修复品种（逗号分隔，默认 cu0,ni0）")
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认 dry-run 零写盘）")
    args = ap.parse_args(argv)

    data_root = Path(args.data_root)
    backup_dir = Path(args.backup_dir)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    unknown = [s for s in symbols if s not in REPAIR_PLAN]
    if unknown:
        print(f"[FAIL] 无修复计划的品种：{unknown}（本报告仅定罪 {list(REPAIR_PLAN)}）")
        return 2

    mode = "APPLY（会写盘）" if args.apply else "DRY-RUN（零写盘）"
    print("=" * 78)
    print(f"  P-NEW cu0/ni0 k 伪台阶修复  |  模式：{mode}")
    print(f"  数据根：{data_root}")
    print(f"  备份目录：{backup_dir}")
    print("=" * 78)

    # ---- 阶段 1：准备（G1~G4 全通过 + G0 备份），**不写分区** ----
    results: list[dict[str, Any]] = []
    try:
        for sym0 in symbols:
            results.append(prepare_symbol(data_root, backup_dir, sym0, args.apply))
    except AssertionError as exc:
        print(f"\n[ABORT] 护栏未通过，已中止且未写盘：\n  {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ABORT] 异常中止：{type(exc).__name__}: {exc}")
        return 4

    if not args.apply:
        print("\n" + "=" * 78)
        print("  汇总（DRY-RUN）")
        print("=" * 78)
        for r in results:
            print(f"  {r['sym0']}: 修复 {r['rows_changed']} 行 × {len(REPAIR_COLS)} 列 | "
                  f"其余 {r['structure']['untouched_rows']} 行 × "
                  f"{r['structure']['cols']} 列逐位不变 | "
                  f"k {r['k']['k_before']:.9f} → {r['k']['k_after']:.9f} "
                  f"@{r['k']['switch_date']} | 写盘=False")
        print("\n[DRY-RUN] G0~G4 全部通过，零写盘。确认无误后加 --apply 执行。")
        return 0

    # ---- 阶段 2：台账留痕（**先于**分区写盘） ----
    sidecar = append_sidecar(data_root, results, backup_dir)
    print(f"\n[LEDGER] 台账已追加（先于分区写盘）→ {sidecar}")

    # ---- 阶段 3：G5 原子写分区 + 落盘后复核 ----
    print()
    try:
        for pending in results:
            commit_symbol(pending)
    except AssertionError as exc:
        print(f"\n[ABORT] 落盘后复核未通过：\n  {exc}\n"
              f"  ⚠ 分区可能已改写，请用备份回滚：{backup_dir}")
        return 5
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ABORT] 写盘异常：{type(exc).__name__}: {exc}\n"
              f"  ⚠ 请用备份回滚：{backup_dir}")
        return 6

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("  汇总（APPLY）")
    print("=" * 78)
    for r in results:
        print(f"  {r['sym0']}: 修复 {r['rows_changed']} 行 × {len(REPAIR_COLS)} 列 | "
              f"其余 {r['structure']['untouched_rows']} 行 × "
              f"{r['structure']['cols']} 列逐位不变 | "
              f"k {r['k']['k_before']:.9f} → {r['k']['k_after']:.9f} "
              f"@{r['k']['switch_date']} | 写盘={r['written']} | "
              f"备份 sha256={r['backup']['sha256'][:16]}…")

    print("\n[OK] 修复完成。")
    print("  ⚠ 下游：主湖已变，需重建 p22 信号缓存（P1-2 允许窗口内执行）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
