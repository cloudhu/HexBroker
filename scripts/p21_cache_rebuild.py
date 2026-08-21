#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P21-3 信号缓存重建编排：v8 同口径 → v12（每组合 checkpoint 落盘 + resume）。

背景
----
P21-3 需以含 08-18~08-21 新数据的 K 线重跑 walk_forward 信号生产，口径与 v8
完全一致（仅数据范围不同）：
  - group_modeling_v2.py（GROUPS_V2 8 组）
  - --label-mode cross_z --label-pool all（P8-4 v8 定稿口径）
  - cal_return_all=False（默认）、cal_split=0.5（build_group_signals 内硬编码）
  - walk_forward 网格：cfg.data.train_len=250/test_len=60/purge=5/embargo=2/mode=rolling
    （configs/base.yaml 默认，与 v8 构建一致）
  - 输出 artifacts/signals_cache18_grouped_v12.parquet（不覆盖 v8）

P13 教训：后台任务可能被回收 → 本脚本按组（8 组）独立落盘 checkpoint
（artifacts/p21_checkpoints/{group}.parquet），resume 时跳过已有组，
最后合并 8 个 checkpoint → v12。结果与一次性全量运行逐字节一致
（各组独立训练，合并仅 concat，无跨组状态）。

用法
----
  python scripts/p21_cache_rebuild.py                     # 全量（跳过已有 checkpoint）
  python scripts/p21_cache_rebuild.py --only agri_protein # 只跑单组（冒烟/续跑）
  python scripts/p21_cache_rebuild.py --force             # 删除已有 checkpoint 重跑
  python scripts/p21_cache_rebuild.py --skip-rebuild      # 仅合并已有 checkpoint → v12
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

ART = ROOT / "artifacts"
CKPT_DIR = ART / "p21_checkpoints"
V12_PATH = ART / "signals_cache18_grouped_v12.parquet"

# GROUPS_V2 组顺序（与 group_modeling_v2.py 一致；组名即 --only 参数）
GROUPS_V2 = [
    "precious",       # au0/ag0
    "ferrous_steel",  # rb0/hc0
    "ferrous_raw",    # i0/j0/jm0
    "industrial",     # cu0/al0/zn0/ni0
    "agri_oil",       # y0/p0
    "agri_protein",   # m0
    "agri_soft",      # sr0/cf0
    "chem_energy",    # ta0/sc0
]

# v8 各组合计行数（artifacts/p8_4_rebuild_v8.log 记录），用于校验 v12 组行数一致
V8_GROUP_ROWS = {
    "precious": 960,
    "ferrous_steel": 960,
    "ferrous_raw": 1440,
    "industrial": 1920,
    "agri_oil": 960,
    "agri_protein": 480,
    "agri_soft": 960,
    "chem_energy": 944,
}


def run_group(group: str, n_jobs: int, force: bool = False) -> Path:
    """运行单组建模并落盘 checkpoint。"""
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    ckpt = CKPT_DIR / f"{group}.parquet"
    if ckpt.exists() and not force:
        print(f"[SKIP] {group}: checkpoint 已存在 {ckpt.name}（如需重跑加 --force）")
        return ckpt
    if ckpt.exists():
        ckpt.unlink()
        print(f"[RM] {group}: 删除旧 checkpoint（--force）")
    cmd = [
        sys.executable, "-u", "scripts/group_modeling_v2.py",
        "--n-jobs", str(n_jobs),
        "--only", group,
        "--label-mode", "cross_z",
        "--label-pool", "all",
        "--output", str(ckpt),
    ]
    print(f"[RUN] {group}: {' '.join(cmd[2:])}")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] {group} 退出码 {r.returncode}")
    if not ckpt.exists():
        raise SystemExit(f"[FAIL] {group} 未产出 checkpoint")
    return ckpt


def merge_checkpoints() -> Path:
    """合并 8 组 checkpoint → v12；按 v8 组行数校验。"""
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    problems: list[str] = []
    for group in GROUPS_V2:
        ckpt = CKPT_DIR / f"{group}.parquet"
        if not ckpt.exists():
            problems.append(f"缺少 {group} checkpoint")
            continue
        df = pd.read_parquet(ckpt)
        frames.append(df)
        expected = V8_GROUP_ROWS[group]
        actual = len(df)
        mark = "OK" if actual == expected else f"!! (v8={expected})"
        print(f"[MERGE] {group}: {actual} 行 [{mark}]")
        if actual != expected:
            problems.append(f"{group}: v12 行数 {actual} != v8 行数 {expected}")
    if not frames:
        raise SystemExit("[FAIL] 无 checkpoint 可合并")
    all_sig = pd.concat(frames, ignore_index=True)
    ART.mkdir(exist_ok=True)
    all_sig.to_parquet(V12_PATH, index=False)
    print(f"[OK] v12 合并 {len(all_sig)} 条 → {V12_PATH}")
    if problems:
        print("[WARN] 行数差异（可能 fold 结构变化，需人工核对）:")
        for p in problems:
            print(f"  - {p}")
    return V12_PATH


def main() -> None:
    ap = argparse.ArgumentParser(description="P21-3 v12 信号缓存重建（checkpoint+resume）")
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔）")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="删除已有 checkpoint 重跑")
    ap.add_argument("--skip-rebuild", action="store_true", help="仅合并已有 checkpoint → v12")
    args = ap.parse_args()

    print("=" * 72)
    print("P21-3 v12 信号缓存重建（v8 同口径：cross_z + label_pool=all + cal_split=0.5）")
    print("=" * 72)

    only = set(args.only.split(",")) if args.only else None
    groups = [g for g in GROUPS_V2 if only is None or g in only]
    if not args.skip_rebuild:
        for g in groups:
            run_group(g, args.n_jobs, force=args.force)
    merge_checkpoints()
    print("=" * 72)
    print("[DONE] P21-3 缓存重建完成")


if __name__ == "__main__":
    main()
