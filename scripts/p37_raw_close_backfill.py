"""P1-c 存量回填（主理人 2026-08-29 拍板 ①离线批量）：raw_close 真名义价批量回填。

背景
----
P0-10 审计发现湖内 ``raw_close`` 列恒等于 ``adj_close``（管线伪映射），
nominal 校准信息丢失。P1-c 已修复 parse 阶段（新写入分区带真名义价），
存量分区经拍板选 ①离线批量：用 ``enrich_raw_close``（sina 备源，save=False
红线）对 18 品种全历史逐分区回填。

流程
----
1. 枚举 ``data/raw/processed/{sym}/1d/{year}.parquet`` 全部分区；
2. 每品种按**实际分区日期范围**仅拉一次备源（``_CachingFetcher.warm``
   预热 + 切片，``end`` 钳制到今天避免新鲜度误杀），
   再按分区窗口对齐 —— 18 品种 × 2 源（交叉校验）≈ 36 次请求而非 ~150 次；
3. 每分区 ``enrich_raw_close``（min_coverage=0.9，all-or-nothing）；
   覆盖率不足 / 备源失败 → 大声降级跳过，绝不半写；
4. dry-run 默认：打印计划（行数/覆盖率/raw_close 变化行数），零写盘；
5. ``--apply``：先全量备份 processed 层到数据根父目录
   （``p37_backup_processed_{ts}``），再逐分区覆写；
6. 幂等：回填值与现有 raw_close 逐行相同的分区**跳过不写**（今日
   P0-11 重建的 rb0/2020、rb0/cu0 2023 已带真名义价，自动跳过）；
7. manifest：仅重算 ``data_version`` 以 ``backfill-`` 开头的 freq 目录
   manifest（P1-b 全量拼接语义）；cu0/rb0 的 ``v1``（盘中自动化维护）
   **不触碰**，留待自动化下次写入刷新。

验证
----
- apply 后逐分区校验：除 ``raw_close`` 外所有列与备份逐值一致；
- 幂等复跑：零分区被改写；
- caliber 边界扫描应保持 0 告警（raw_close 不参与 adj 连续性）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"

#: 逐值一致性校验时跳过的列（唯一允许变化的列）
CHANGED_COL = "raw_close"


class _CachingFetcher:
    """每品种仅拉一次备源，后续按窗口切片（鸭子类型兼容 fetch_raw）。

    预热契约（``warm``）：按品种**实际分区日期范围**拉取一次并缓存，
    ``end`` 必须钳制到今天 —— ``BackupRawFetcher`` 内部 ``assert_fresh``
    只对 ``end < today - max_stale_days``（历史回填）豁免新鲜度判定，
    请求未来日期窗口（如 2099）会把"最新 bar = 今天"判为数据陈旧，
    导致全源失败（p37 首轮 dry-run 162/162 BackupExhaustedError 根因）。

    ``fetch_raw`` 对未预热品种兜底直拉请求自身窗口（同样不伪造未来窗口）。
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self._cache: dict[str, Any] = {}

    def warm(self, sym: str, start: str, end: str) -> None:
        if sym not in self._cache:
            pulls = self._inner.fetch_raw([sym], start, end)
            self._cache[sym] = pulls[sym]

    def fetch_raw(self, symbols: list[str], start: str, end: str) -> dict:
        out: dict = {}
        for sym in symbols:
            if sym not in self._cache:
                pulls = self._inner.fetch_raw([sym], start, end)
                self._cache[sym] = pulls[sym]
            pull = self._cache[sym]
            mask = (pull.close.index >= pd.Timestamp(start)) & (
                pull.close.index <= pd.Timestamp(end) + pd.Timedelta(days=1))
            out[sym] = dataclasses.replace(pull, close=pull.close[mask])
        return out


def _iter_partitions(root: Path) -> list[tuple[str, Path, int]]:
    """枚举 processed 层全部年度分区 → [(sym0, path, year)]。"""
    out: list[tuple[str, Path, int]] = []
    proc = root / "processed"
    if not proc.exists():
        return out
    for sym_dir in sorted(p for p in proc.iterdir() if p.is_dir()):
        freq_dir = sym_dir / "1d"
        if not freq_dir.exists():
            continue
        for fp in sorted(freq_dir.glob("*[0-9].parquet")):
            try:
                year = int(fp.stem)
            except ValueError:
                continue
            out.append((sym_dir.name, fp, year))
    return out


def _manifest_is_backfill_maintained(freq_dir: Path) -> bool:
    """freq 目录 manifest 是否由 backfill 维护（data_version=backfill-*）。

    cu0/rb0 等盘中自动化维护的 v1 manifest 不触碰（防 P1-b 记录的
    "无谓翻覆"），留待自动化下次写入刷新。
    """
    m = freq_dir / "manifest.json"
    if not m.exists():
        return False
    try:
        return str(json.loads(m.read_text(encoding="utf-8"))
                   .get("data_version", "")).startswith("backfill-")
    except (json.JSONDecodeError, OSError):
        return False


def main(argv: list[str] | None = None, *, fetcher: Any = None) -> int:
    ap = argparse.ArgumentParser(
        description="P1-c 存量 raw_close 真名义价离线批量回填")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT), help="数据湖根目录")
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认 dry-run 只出计划）")
    ap.add_argument("--min-coverage", type=float, default=0.9,
                    help="备源对齐覆盖率阈值（默认 0.9，低于则跳过该分区）")
    args = ap.parse_args(argv)

    from hexbroker.config import load_config
    from hexbroker.data.manifest import (
        _backfill_version,
        _constants_from_cfg,
        build_manifest,
        write_manifest,
    )
    from hexbroker.utils.io import read_parquet, write_parquet
    from scripts.p6_4_fill_gaps import enrich_raw_close

    if not args.apply:
        print("[DRY-RUN] 预览模式，不写盘（加 --apply 执行回填）")

    root = Path(args.data_root)
    partitions = _iter_partitions(root)
    if not partitions:
        print("[FAIL] 未找到任何 processed/{sym}/1d/{year}.parquet 分区")
        return 2
    syms = sorted({s for s, _, _ in partitions})
    print(f"[SCAN] {len(syms)} 品种 / {len(partitions)} 个年度分区")

    if fetcher is None:
        from hexbroker.data.backup import BackupRawFetcher
        fetcher = _CachingFetcher(BackupRawFetcher(root=None, save=False))
    cfg = load_config()

    # ---- 按品种真实数据范围预热缓存（每品种 2 次请求：sina + akshare 交叉校验）----
    # end 钳制到今天：未来日期窗口会被 assert_fresh 判"数据陈旧"（见 _CachingFetcher）。
    ranges: dict[str, tuple[int, int]] = {}
    for sym, _, year in partitions:
        lo, hi = ranges.get(sym, (year, year))
        ranges[sym] = (min(lo, year), max(hi, year))
    warm = getattr(fetcher, "warm", None)
    if warm is not None:
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        for sym in sorted(ranges):
            lo, hi = ranges[sym]
            end = min(f"{hi}-12-31", today)
            warm(sym, f"{lo}-01-01", end)
            print(f"[WARM] {sym}: {lo}-01-01 ~ {end} 预热完成")

    plan: list[tuple[str, Path, pd.DataFrame, int]] = []  # (sym, path, new_df, changed_rows)
    skipped: list[tuple[str, str]] = []                   # (sym/year, 归因)
    for sym, fp, year in partitions:
        df = read_parquet(fp)
        new_df, notes = enrich_raw_close(
            df, sym, fetcher=fetcher, min_coverage=args.min_coverage)
        ok = any("回填成功" in n for n in notes)
        changed = 0
        if ok and CHANGED_COL in df.columns and CHANGED_COL in new_df.columns:
            changed = int((df[CHANGED_COL].to_numpy() != new_df[CHANGED_COL].to_numpy()).sum())
        if not ok:
            for n in notes:
                print(f"[WARN][NOMINAL] {sym}/{year}: {n}")
            skipped.append((f"{sym}/{year}", "; ".join(notes)))
            continue
        if changed == 0:
            print(f"[SKIP] {sym}/{year}: raw_close 已是真名义价（{len(df)} 行，零改动）")
            continue
        print(f"[PLAN] {sym}/{year}: {len(df)} 行，raw_close 将更新 {changed} 行"
              f"（{notes[0]}）")
        plan.append((sym, fp, new_df, changed))

    print(f"[PLAN] 待写盘 {len(plan)} 个分区；"
          f"已是真名义价自动跳过 {len(partitions) - len(plan) - len(skipped)} 个；"
          f"回填失败大声降级 {len(skipped)} 个")
    if not plan:
        print("[OK] 无需写盘（全部分区已是真名义价或显式跳过）")
        return 0
    if not args.apply:
        print("[DRY-RUN] 结束（零写盘）")
        return 0

    # ---- 落盘前全量备份 processed 层 ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup_dir = root.parent / f"p37_backup_processed_{ts}"
    shutil.copytree(root / "processed", backup_dir)
    print(f"[BACKUP] processed 层已备份 → {backup_dir}")

    # ---- 逐分区覆写 + 一致性校验 + manifest 重算 ----
    rewritten_syms: set[str] = set()
    for sym, fp, new_df, _changed in plan:
        old = read_parquet(fp)
        other_cols = [c for c in old.columns if c != CHANGED_COL]
        assert all(old[c].equals(new_df[c]) for c in other_cols), \
            f"{fp}: raw_close 之外列发生意外变化 —— 中止该分区"
        write_parquet(new_df, fp)
        rewritten_syms.add(sym)
        print(f"[WRITE] {fp.relative_to(root)}（{len(new_df)} 行）")

    # manifest：仅重算 backfill-* 维护的 freq 目录；v1（盘中自动化）不触碰
    for sym in sorted(rewritten_syms):
        freq_dir = root / "processed" / sym / "1d"
        if not _manifest_is_backfill_maintained(freq_dir):
            print(f"[MANIFEST] {sym}: v1（盘中自动化维护）不触碰，"
                  f"留待自动化下次写入刷新")
            continue
        parts = [read_parquet(f) for f in sorted(freq_dir.glob("*.parquet"))]
        write_manifest(
            build_manifest("processed", sym, "1d", pd.concat(parts, ignore_index=True),
                           source="lake", data_version=_backfill_version(),
                           constants=_constants_from_cfg(cfg)),
            root,
        )
        print(f"[MANIFEST] {sym}: 已按全量拼接语义重算（backfill-*）")

    print(f"[OK] P1-c 存量回填完成：{len(plan)} 个分区覆写，"
          f"备份于 {backup_dir.name}（回滚 = 拷回）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
