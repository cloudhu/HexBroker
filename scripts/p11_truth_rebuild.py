"""P0-11：rb0/2020 缺失分区真值重建驱动器。

背景（2026-08-29 11:49 pytest 污染事故）
----------------------------------------
AkshareSource(save=True) 默认落盘 + DataLake.save_processed 整文件覆盖，
rb0/2020 分区被写成 2 行合成数据，已隔离（artifacts/quarantine/）并挂
``_MISSING_2020.json``。留一法实测插值重建不可行（53.22 bp 均值误差），
唯一出路 = pandadata ``get_future_daily_post(method=close_pcr)`` 重拉 2020
全年真值 + P0-9 rebuild 流水线整分区新建。

用法（两步走）
--------------
1. 拉真值（需 pandadata MCP 工具，本会话未注册时在新会话执行）::

     mcp pandadata get_future_daily_post
       underlying_symbol=["RB"] start_date=20200101 end_date=20201231
       method=close_pcr
     → 结果 JSON 存为 artifacts/p11_rb0_2020_truth.json
       （格式与 p6_4 persisted 一致：{"result": {"columns": [...], "rows": [...]}}）

2. 重建（默认 dry-run，--apply 才写盘）::

     python scripts/p11_truth_rebuild.py --persisted artifacts/p11_rb0_2020_truth.json
     python scripts/p11_truth_rebuild.py --persisted ... --apply

安全语义
--------
- dry-run 默认：只解析/规范化/名义价回填/预览，零写盘；
- --apply 走 P0-9 ``rebuild_partition``（missing 路径）：整分区新建 +
  自动清 ``_MISSING_2020.json`` + manifest 重写（source=truth-rebuild）；
- 真值仅取目标年度行（rebuild_partition 跨界拒绝双保险）；
- 名义价回填（P1-c ``enrich_raw_close``）默认开启，失败大声降级。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"


def _load_p6_4():
    """复用 p6_4 的持久化解析与规范化（已测路径，避免第三份实现）。"""
    spec = importlib.util.spec_from_file_location(
        "p6_4_fill_gaps", PROJECT_ROOT / "scripts" / "p6_4_fill_gaps.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p6_4_fill_gaps"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-11 缺失分区真值重建（rb0/2020）")
    ap.add_argument("--persisted", required=True,
                    help="pandadata 拉取结果 JSON（p6_4 persisted 格式）")
    ap.add_argument("--sym", default="rb0", help="品种 sym0（默认 rb0）")
    ap.add_argument("--year", type=int, default=2020, help="目标年度（默认 2020）")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT), help="数据湖根目录")
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘（默认 dry-run 只预览）")
    ap.add_argument("--skip-nominal", action="store_true",
                    help="跳过 P1-c 名义价回填（离线场景）")
    args = ap.parse_args(argv)

    from hexbroker.data.rebuild import RebuildNeeded, rebuild_partition
    from hexbroker.data.store import DataLake

    p6_4 = _load_p6_4()

    persisted = Path(args.persisted)
    if not persisted.exists():
        print(f"[FAIL] 真值文件不存在: {persisted}")
        return 2
    if not args.apply:
        print("[DRY-RUN] 预览模式，不写盘（加 --apply 执行重建）")

    # 1) 解析 + 规范化（复用 p6_4 已测路径，含 RAW_SCALE_FIX）
    df = p6_4.load_persisted_rows(persisted)
    print(f"[PARSE] 真值文件 {len(df)} 行，列: {list(df.columns)[:12]}...")
    underlying, sym0 = p6_4.normalize_sym_arg(args.sym)
    truth = p6_4.normalize_new_df(
        df, underlying, sym0,
        date(args.year, 1, 1), date(args.year, 12, 31),
    )
    if truth.empty:
        print(f"[FAIL] 规范化后无 {sym0} 在 {args.year} 年的行 —— "
              f"检查拉取参数（underlying_symbol/日期区间/method）")
        return 1
    year_rows = truth[truth["datetime"].dt.year == args.year]
    if len(year_rows) != len(truth):
        print(f"[WARN] 真值含非 {args.year} 年行 {len(truth) - len(year_rows)} 条，"
              f"已过滤（rebuild_partition 亦有跨界拒绝双保险）")
        truth = year_rows
    print(f"[PARSE] 目标真值 {len(truth)} 行，"
          f"{truth['datetime'].min().date()} ~ {truth['datetime'].max().date()}")

    # 2) P1-c 名义价回填（失败大声降级）
    if args.skip_nominal:
        print(f"[NOMINAL] {sym0}: 已跳过（--skip-nominal），raw_close 仍为 adj 复制品")
    else:
        truth, notes = p6_4.enrich_raw_close(truth, sym0)
        for note in notes:
            tag = "[NOMINAL]" if "回填成功" in note else "[WARN][NOMINAL]"
            print(f"{tag} {sym0}: {note}")

    # 3) 重建（missing 路径）
    item = RebuildNeeded(symbol=sym0, freq="1d", year=args.year,
                         kind="missing", dates=[], detail={
                             "driver": "scripts/p11_truth_rebuild.py",
                             "persisted": str(persisted),
                         })
    if not args.apply:
        print(f"[DRY-RUN] 将新建 {sym0}/1d/{args.year}.parquet "
              f"({len(truth)} 行) 并清除 _MISSING_{args.year}.json；预览:")
        print(truth.head(3).to_string(index=False))
        return 0

    res = rebuild_partition(Path(args.data_root), item, truth)
    print(f"[REBUILD] {res.symbol}/{res.year}({res.kind}) status={res.status} "
          f"rows {res.rows_before}→{res.rows_after} "
          f"appended={len(res.appended)} replaced={len(res.replaced)} "
          f"marker_cleared={res.sidecar_cleared}"
          + (f" reason={res.reason}" if res.reason else ""))
    if res.status != "OK":
        print(f"[FAIL] 重建未完成：{res.reason}")
        return 1

    # 4) 重建后验证：行数 / 洞告警消除 / 年度边界连续性
    lake = DataLake(root=args.data_root)
    bf = lake.load_processed(sym0, "1d", warn=False)
    dts = bf.df.index.get_level_values("datetime")
    print(f"[VERIFY] {sym0} 全量 {len(bf.df)} 行，{dts.min().date()} ~ {dts.max().date()}")

    notes = lake.quality_notes(sym0, "1d")
    holes = [n for n in notes if n.kind == "hole"]
    print(f"[VERIFY] quality_notes hole 数: {len(holes)}"
          + (f"（仍剩: {[n.year for n in holes]}）" if holes else "（2020 洞已消除）"))

    adj = bf.df["adj_close"].reset_index(drop=True)
    dt = pd.Series(dts)
    y19_end = adj[dt.dt.year == 2019].iloc[-1] if (dt.dt.year == 2019).any() else None
    y20_first = adj[dt.dt.year == args.year].iloc[0]
    y20_last = adj[dt.dt.year == args.year].iloc[-1]
    y21_first = adj[dt.dt.year == 2021].iloc[0] if (dt.dt.year == 2021).any() else None
    for label, a, b in (("2019→2020", y19_end, y20_first),
                        ("2020→2021", y20_last, y21_first)):
        if a is None or b is None or not a or not b:
            print(f"[VERIFY] 边界 {label}: 数据不足，跳过")
            continue
        jump = abs(b / a - 1)
        flag = "OK" if jump <= 0.10 else "🔴 可疑假跳变（>10%，需人工复核）"
        print(f"[VERIFY] 边界 {label}: adj 跳变 {jump:.4%} —— {flag}")

    print(f"[OK] P0-11 完成：{sym0}/{args.year} 真值重建 + 洞标记清除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
