"""P1 dominant 日历本地快照驱动器（§3 架构决策）。

把主源 pandadata 返回中的 ``dominant_id``（+ 不复权 ``open_interest``）
固化为本地快照 ``{root}/interim/dominant/{sym0}.parquet``，解开
"主源挂了，换月日历也得问主源要"的死结。

工作流与 p11 相同：会话内调用 pandadata MCP → 结果自动落盘
（connector-proxy JSON）→ 本驱动器入库。支持批量目录（多品种）。

用法
----
    python scripts/p41_dominant_snapshot.py --input artifacts/p11_rb0_2023_truth.json --sym RB
    python scripts/p41_dominant_snapshot.py --input-dir artifacts/persisted/ --apply
    python scripts/p41_dominant_snapshot.py --input-dir ... --sym-map "RB:rb0,CU:cu0"

语义
----
- dry-run 默认：只打印每品种将写入的统计（行数/新增/覆盖/切换次数）；
- ``--apply`` 落盘（merge-upsert，幂等）；
- 品种推断：显式 ``--sym`` 优先；否则取行内 ``symbol`` 列（``RB``）→ ``rb0``；
- ⚠️ 快照仅用于换月日候选/事后对账/分析 —— dominant_id 切换日 ≠
  后复权因子切换日（graft §4.8 实测），不得直接当调整依据。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"

from hexbroker.data import dominant as dom


def _infer_sym0(df: pd.DataFrame, explicit: str | None) -> str:
    if explicit:
        s = explicit.strip().lower()
        # 显式传品种（RB）→ 连续符号（rb0）；已是连续符号（rb0）则原样
        return s if s.endswith("0") else f"{s}0"
    if "symbol" not in df.columns or df.empty:
        raise ValueError("未指定 --sym 且输入缺少 symbol 列，无法推断品种")
    return f"{str(df['symbol'].iloc[0]).lower()}0"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P1 dominant 日历本地快照")
    ap.add_argument("--input", help="单个持久化 JSON（pandadata tool-result）")
    ap.add_argument("--input-dir", help="目录批量（递归收 *.json）")
    ap.add_argument("--sym", help="显式品种（RB → rb0）；批量模式可省")
    ap.add_argument("--sym-map", help="品种映射覆盖（RB:rb0,CU:cu0）")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT))
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    args = ap.parse_args(argv)

    from scripts.p6_4_fill_gaps import load_persisted_rows

    paths: list[Path] = []
    if args.input:
        paths.append(Path(args.input))
    elif args.input_dir:
        paths = sorted(Path(args.input_dir).rglob("*.json"))
    else:
        ap.error("需要 --input 或 --input-dir")
    if not paths:
        print("[FAIL] 未找到任何 JSON 输入")
        return 2

    sym_map: dict[str, str] = {}
    if args.sym_map:
        for pair in args.sym_map.split(","):
            k, _, v = pair.partition(":")
            sym_map[k.strip().upper()] = v.strip().lower()

    root = Path(args.data_root)
    per_sym: dict[str, pd.DataFrame] = {}
    for p in paths:
        try:
            df = load_persisted_rows(p)
        except Exception as exc:  # noqa: BLE001 - 跳过非 pandadata JSON 并留痕
            print(f"[SKIP] {p.name}: {type(exc).__name__}: {exc}")
            continue
        if df.empty or "dominant_id" not in df.columns:
            print(f"[SKIP] {p.name}: 非 pandadata 日线结果（{len(df)} 行）")
            continue
        upper = (args.sym or str(df["symbol"].iloc[0]) if "symbol" in df.columns
                 else None) or ""
        upper = str(upper).upper()
        sym0 = sym_map.get(upper) or _infer_sym0(df, args.sym)
        cal = dom.extract_calendar(df)
        prev = per_sym.get(sym0)
        per_sym[sym0] = pd.concat([prev, cal]).sort_index() if prev is not None else cal

    if not per_sym:
        print("[FAIL] 无可入库的 dominant 数据")
        return 2

    plan = []
    for sym0 in sorted(per_sym):
        # 日历 = 每交易日一行；extract_calendar 已按日期去重，此处只防
        # 多文件拼接后的重叠日期（⚠️ 不可按 dominant_id 去重 —— 会把
        # 同一主力合约存续期坍缩成单日）。
        cal = per_sym[sym0][~per_sym[sym0].index.duplicated(keep="last")]
        old = dom.load_calendar(root, sym0)
        n_new = 0 if old is None else len(cal.index.difference(old.index))
        n_cov = 0 if old is None else len(cal.index.intersection(old.index))
        switches = dom.detect_switches(cal)
        print(f"[PLAN] {sym0}: 日历 {len(cal)} 行（新增 {n_new} / 覆盖 {n_cov} / "
              f"历史切换 {max(len(switches) - 1, 0)} 次）")
        plan.append((sym0, cal))

    if not args.apply:
        print("[DRY-RUN] 结束（零写盘，加 --apply 落盘）")
        return 0

    for sym0, cal in plan:
        stat = dom.save_calendar(root, sym0, cal)
        print(f"[WRITE] {stat['path']}（total={stat['total']} "
              f"覆盖={stat['overwritten']} 新增={stat['appended']}）")
    print(f"[OK] dominant 快照完成：{len(plan)} 品种")
    return 0


if __name__ == "__main__":
    sys.exit(main())
