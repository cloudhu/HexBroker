#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0-A 显式修复脚本：把指定品种/日期的**复权价格格**回滚到权威快照。

背景（2026-09-03 cu0/ni0 复权污染事故）
--------------------------------------
``scripts/refresh_pull_local.py`` 每次拉取都带完整 10 日窗口，融合阶段
``p6_4_fill_gaps.py`` 的 ``drop_duplicates(subset="datetime", keep="last")``
让 tqsdk 换月窗口内带有 seam 误差的后复权价整格覆盖主湖既有序列：

  - cu0 2026-08-21 close 158682.55 → 159021.99（+0.2139%）
  - cu0 2026-08-24 close 159258.12 → 159553.29（+0.1853%）
  - ni0 2026-08-21 close 158361.02 → 158042.33（−0.2012%）
  - ni0 2026-08-24 close 159169.98 → 158839.04（−0.2079%）

同一主力段内 ``k = adj_close / raw_close`` 本应恒定，污染后段内 k 出现
0.2% 级漂移 → 判定新值为假，旧值为真（段内 k 自洽）。详见
``deliverables/cu_ni_rollback_20260903.md``。

为什么是"显式修复脚本"而不是让融合再覆盖回去
--------------------------------------------
后复权序列是**累积状态量**，任何隐式覆盖都会让污染不可追溯。本脚本：
  1. 只改白名单里的格子（默认 20 格），其余一律不动；
  2. 保留 ``raw_close``、``volume``、``open_interest``（P2-4 方案 A 授权的
     持仓量修正）、以及 09-02/09-03 新增行；
  3. 写前先做一次独立备份，写用 ``tmp + os.replace`` 原子替换（G5）；
  4. 每次执行落一份取证 JSON（改前值/改后值/来源快照/mtime），可复核。

⛔ 本脚本**不删除任何文件**，不使用 ``shutil.move``。

用法
----
  # 默认 dry-run，只打印将要改的格子
  python scripts/rollback_adj_cells.py

  # 实际执行（含写前备份 + 取证留痕）
  python scripts/rollback_adj_cells.py --apply

  # 自定义格子（调试/未来同类事故复用）
  python scripts/rollback_adj_cells.py --symbol cu0 --date 2026-08-21 --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "raw" / "processed"

# 权威快照（污染发生前的备份，2026-09-03 17:10）
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "artifacts" / "_p2_backup_20260903"
# 本次修改前的再备份（G5：修改前必须留一份可回退的副本）
DEFAULT_PRE_DIR = PROJECT_ROOT / "artifacts" / "_p2_backup_20260903_pre_rollback"
AUDIT_DIR = PROJECT_ROOT / "artifacts" / "_tmp"

# 只允许被回滚的列：价格 + 后复权价
ROLLBACK_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "adj_close")
# ⛔ 绝不触碰：名义价（已核干净）、成交量、持仓量（P2-4 方案 A 授权修正）
FROZEN_COLUMNS: tuple[str, ...] = ("raw_close", "volume", "amount", "open_interest")

# 默认回滚清单（G1：恰好 20 格 = 5 列 × 2 日期 × 2 品种）
DEFAULT_PLAN: dict[str, tuple[str, ...]] = {
    "cu0": ("2026-08-21", "2026-08-24"),
    "ni0": ("2026-08-21", "2026-08-24"),
}


# --------------------------------------------------------------------------- #
# 备份（只 copy，绝不 move/delete）
# --------------------------------------------------------------------------- #
def backup_symbol(sym0: str, dest_root: Path) -> list[str]:
    """把主湖 ``{sym0}/1d/`` 下所有文件复制到 ``dest_root/{sym0}/1d/``。

    返回实际复制的相对路径列表。已存在则覆盖（同目录重复执行幂等）。
    """
    src_dir = PROCESSED_DIR / sym0 / "1d"
    if not src_dir.exists():
        raise FileNotFoundError(f"主湖目录不存在: {src_dir}")
    copied: list[str] = []
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        dst = dest_root / sym0 / "1d" / src.relative_to(src_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(src.relative_to(src_dir)))
    return copied


# --------------------------------------------------------------------------- #
# 回滚计划
# --------------------------------------------------------------------------- #
def build_plan(
    plan: dict[str, tuple[str, ...]],
    lake_root: Path,
    source_root: Path,
) -> list[dict[str, Any]]:
    """读取主湖与权威快照，产出逐格差异清单（只含 ``ROLLBACK_COLUMNS``）。"""
    cells: list[dict[str, Any]] = []
    for sym0, dates in plan.items():
        lake_path = lake_root / sym0 / "1d" / "2026.parquet"
        snap_path = source_root / sym0 / "1d" / "2026.parquet"
        if not lake_path.exists():
            raise FileNotFoundError(f"主湖文件不存在: {lake_path}")
        if not snap_path.exists():
            raise FileNotFoundError(f"权威快照不存在: {snap_path}")
        lake = pd.read_parquet(lake_path)
        snap = pd.read_parquet(snap_path)
        lake["datetime"] = pd.to_datetime(lake["datetime"]).dt.normalize()
        snap["datetime"] = pd.to_datetime(snap["datetime"]).dt.normalize()
        li = lake.set_index("datetime")
        si = snap.set_index("datetime")
        for day in dates:
            ts = pd.Timestamp(day)
            if ts not in li.index:
                raise KeyError(f"{sym0} 主湖无 {day}")
            if ts not in si.index:
                raise KeyError(f"{sym0} 快照无 {day}")
            for col in ROLLBACK_COLUMNS:
                cur = float(li.loc[ts, col])
                old = float(si.loc[ts, col])
                cells.append({
                    "sym0": sym0,
                    "year": 2026,
                    "date": day,
                    "column": col,
                    "current": cur,
                    "target": old,
                    "delta": cur - old,
                    "delta_pct": (cur - old) / old * 100.0 if old else float("nan"),
                })
    return cells


def apply_plan(
    plan: dict[str, tuple[str, ...]],
    cells: list[dict[str, Any]],
    lake_root: Path,
    source_root: Path,
) -> list[dict[str, Any]]:
    """按清单写回主湖（``tmp + os.replace`` 原子替换，G5）。

    只写 ``ROLLBACK_COLUMNS``；其余列与主湖当前值保持完全一致。
    """
    applied: list[dict[str, Any]] = []
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for cell in cells:
        by_sym.setdefault(cell["sym0"], []).append(cell)

    for sym0, sym_cells in by_sym.items():
        year = int(sym_cells[0]["year"])
        lake_path = lake_root / sym0 / "1d" / f"{year}.parquet"
        snap_path = source_root / sym0 / "1d" / f"{year}.parquet"

        df = pd.read_parquet(lake_path)
        snap = pd.read_parquet(snap_path)
        # 写前列序：写回后必须逐位还原（G3）
        original_columns = list(df.columns)
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.normalize()
        snap["datetime"] = pd.to_datetime(snap["datetime"]).dt.normalize()
        di = df.set_index("datetime")
        si = snap.set_index("datetime")

        frozen_before = {
            col: [float(di.loc[pd.Timestamp(c["date"]), col]) for c in sym_cells]
            for col in FROZEN_COLUMNS if col in di.columns
        }

        for cell in sym_cells:
            ts = pd.Timestamp(cell["date"])
            di.loc[ts, cell["column"]] = float(si.loc[ts, cell["column"]])

        # G5：原子替换（tmp 与 year_path 同目录，保证同分区 rename）
        out = di.reset_index()[original_columns]  # 列序与写前完全一致（G3）
        lake_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = lake_path.with_suffix(".parquet.tmp")
        out.to_parquet(tmp, index=False)
        tmp.replace(lake_path)

        # 复核：冻结列必须逐位不变
        back = pd.read_parquet(lake_path)
        back["datetime"] = pd.to_datetime(back["datetime"]).dt.normalize()
        bi = back.set_index("datetime")
        for col, before in frozen_before.items():
            after = [float(bi.loc[pd.Timestamp(c["date"]), col]) for c in sym_cells]
            if before != after:
                raise RuntimeError(
                    f"{sym0} 冻结列 {col} 被改动！before={before} after={after}"
                )
        applied.extend(sym_cells)
    return applied


def verify(
    plan: dict[str, tuple[str, ...]],
    lake_root: Path,
    source_root: Path,
) -> list[dict[str, Any]]:
    """回滚后复核：目标格 == 快照值；``raw_close`` 仍等于回滚前主湖值。"""
    residual: list[dict[str, Any]] = []
    for sym0, dates in plan.items():
        lake = pd.read_parquet(lake_root / sym0 / "1d" / "2026.parquet")
        snap = pd.read_parquet(source_root / sym0 / "1d" / "2026.parquet")
        lake["datetime"] = pd.to_datetime(lake["datetime"]).dt.normalize()
        snap["datetime"] = pd.to_datetime(snap["datetime"]).dt.normalize()
        li = lake.set_index("datetime")
        si = snap.set_index("datetime")
        for day in dates:
            ts = pd.Timestamp(day)
            for col in ROLLBACK_COLUMNS:
                if float(li.loc[ts, col]) != float(si.loc[ts, col]):
                    residual.append({
                        "sym0": sym0, "date": day, "column": col,
                        "lake": float(li.loc[ts, col]),
                        "snapshot": float(si.loc[ts, col]),
                    })
    return residual


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="P0-A 显式修复：把复权价格格回滚到权威快照（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认只打印计划）")
    ap.add_argument("--symbol", action="append", default=None,
                    help="只处理指定品种（可重复；默认 cu0 + ni0）")
    ap.add_argument("--date", action="append", default=None,
                    help="只处理指定日期 YYYY-MM-DD（可重复；默认 08-21 + 08-24）")
    ap.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR),
                    help=f"权威快照根目录（默认 {DEFAULT_SOURCE_DIR}）")
    ap.add_argument("--pre-backup-dir", default=str(DEFAULT_PRE_DIR),
                    help=f"写前再备份目录（默认 {DEFAULT_PRE_DIR}）")
    ap.add_argument("--lake-dir", default=str(PROCESSED_DIR),
                    help=f"主湖根目录（默认 {PROCESSED_DIR}）")
    ap.add_argument("--skip-backup", action="store_true",
                    help="跳过写前再备份（⚠️ 仅限已备份过的重跑）")
    args = ap.parse_args()

    plan: dict[str, tuple[str, ...]] = {}
    for sym0, dates in DEFAULT_PLAN.items():
        if args.symbol and sym0 not in args.symbol:
            continue
        picked = tuple(args.date) if args.date else dates
        plan[sym0] = picked

    if not plan:
        print("[FAIL] 空的回滚计划")
        return 2

    lake_root = Path(args.lake_dir)
    source_root = Path(args.source_dir)

    print("=" * 96)
    print("P0-A 复权污染回滚"
          f"{'（DRY-RUN，未写盘）' if not args.apply else '（--apply 实盘写）'}")
    print("=" * 96)
    print(f"主湖      : {lake_root}")
    print(f"权威快照  : {source_root}")
    print(f"品种/日期 : {plan}")

    cells = build_plan(plan, lake_root, source_root)
    print(f"\n[PLAN] 待回滚格子 {len(cells)} 个（G1 预算 20）")
    print(f"  {'sym':<6}{'date':<13}{'col':<12}{'current':>16}{'target':>16}{'Δ%':>12}")
    for c in cells:
        print(f"  {c['sym0']:<6}{c['date']:<13}{c['column']:<12}"
              f"{c['current']:>16.6f}{c['target']:>16.6f}{c['delta_pct']:>+12.6f}")
    if len(cells) != 20:
        print(f"\n[WARN] 格子数 {len(cells)} != 20，与 G1 预算不符，请人工确认")

    if not args.apply:
        print("\n[DRY-RUN] 未写盘。确认无误后加 --apply。")
        return 0

    # 1) 写前再备份
    pre_dir = Path(args.pre_backup_dir)
    if not args.skip_backup:
        for sym0 in plan:
            copied = backup_symbol(sym0, pre_dir)
            print(f"\n[BACKUP] {sym0} → {pre_dir / sym0 / '1d'}（{len(copied)} 个文件）")
            for rel in copied:
                print(f"         {rel}")
    else:
        print("\n[BACKUP] 已跳过（--skip-backup）")

    # 2) 写回
    applied = apply_plan(plan, cells, lake_root, source_root)
    print(f"\n[WRITE] 已原子写回 {len(applied)} 个格子（tmp + os.replace）")

    # 3) 复核
    residual = verify(plan, lake_root, source_root)
    if residual:
        print(f"\n[FAIL] 回滚后仍有 {len(residual)} 个格子与快照不一致：")
        for r in residual:
            print(f"   {r}")
        return 1

    # 4) 取证留痕
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_path = AUDIT_DIR / f"rollback_adj_cells_{ts}.json"
    audit_path.write_text(
        json.dumps({
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "lake_dir": str(lake_root),
            "source_dir": str(source_root),
            "pre_backup_dir": str(pre_dir),
            "plan": {k: list(v) for k, v in plan.items()},
            "columns": list(ROLLBACK_COLUMNS),
            "frozen_columns": list(FROZEN_COLUMNS),
            "cells": cells,
            "residual_after_verify": residual,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[AUDIT] 取证留痕 → {audit_path}")
    print("\n[OK] 回滚完成，复核通过（目标格 == 快照值，冻结列逐位未变）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
