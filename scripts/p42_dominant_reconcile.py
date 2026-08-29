"""P1 收口：dominant 快照 × 湖内复权因子 独立交叉对账探针（只读）。

两个**独立证据源**：
  A. dominant 快照（``interim/dominant/{sym0}.parquet``）的
     ``detect_switches`` 切换日（排除段首 ``from_id`` 为空的非真切换）；
  B. 湖内 processed ``adj_close/raw_close`` 比值突变日 —— 复权因子切换
     只发生在换月（或口径事件），比值突变即因子动过。

语义（大声声明）
----------------
- A 有 B 无（±窗口内）→ **"dominant 切了、因子没动"**（ni0 型，
  §4.8 实测存在）——此类日期**不得**作为 graft ``rollover_dates`` 输入；
- B 有 A 无 → 因子动了但快照没登记，分两类：
  * **单日回落尖峰**（次日比值回到原水平）→ 数据毛刺嫌疑
    （⚠️ 2026-08-29 实测发现 cu0 2019-04-22 单日 raw_close 含复权价，
    与 2023 名义价冒充同族但反向）；
  * **持续阶梯** → roll 期震荡（备源主力逐日 OI 翻覆 vs 主源单日切换，
    rb0 2021-11-18~26 连续 5 天实测）或真实口径事件，需人工复核；
- 本探针**只读不写**（无 --apply），报告走 stdout / 可选 ``--json``。

用法
----
    python scripts/p42_dominant_reconcile.py --sym cu0,rb0
    python scripts/p42_dominant_reconcile.py --all --json artifacts/p42_reconcile.json
    python scripts/p42_dominant_reconcile.py --sym rb0 --start 2023-01-01 --end 2023-12-31

阈值说明：B 检测默认 ``--tol 1e-3``（10bp）—— 真换月跳空实测最小
62.7bp（≈6.3e-3），而 ``graft.DEFAULT_ALIGN_TOL=1e-6`` 是同源重叠区
容差，直接借用会把湖内跨源微差全判成突变（实测 cu0 212 处假阳性）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "raw"

from hexbroker.data import dominant as dom

PROCESSED_LAYERS = ("1d",)


def _load_processed(root: Path, sym0: str, freq: str = "1d") -> pd.DataFrame | None:
    """湖内 processed 全量拼接（datetime 列 → 索引，升序去重）。"""
    d = root / "processed" / sym0 / freq
    files = sorted(d.glob("*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = (df.sort_values("datetime")
            .drop_duplicates(subset="datetime", keep="last")
            .set_index("datetime"))
    return df


def _factor_jump_dates(df: pd.DataFrame, tol: float) -> pd.Series:
    """adj/raw 比值突变日 → {日期: 相对突变幅度}。

    仅在 raw_close/adj_close 均为正的样本上计算（复权价冒充名义价的
    单日毛刺比值会异常，正是要捕获的对象）。
    """
    m = (df.get("raw_close", pd.Series(dtype=float)) > 0) & \
        (df.get("adj_close", pd.Series(dtype=float)) > 0)
    if not m.any():
        return pd.Series(dtype=float)
    ratio = (df.loc[m, "adj_close"] / df.loc[m, "raw_close"]).sort_index()
    ratio = ratio[~ratio.index.duplicated(keep="last")]
    chg = ratio.pct_change().abs().dropna()
    return chg[chg > tol]


def _match(a_dates: list, b: pd.Series, window: int,
           cal_index: pd.DatetimeIndex) -> tuple[dict, set]:
    """A 每个切换日在 B 中找 ±``window`` 个**交易日**内最近突变。

    交易日偏移基于 ``cal_index``（湖内全交易日历）中的位置差，
    而非稀疏的 B 日期序列。返回
    （{a_date: {"offset": 交易日偏移, "magnitude": 突变幅度,
    "b_date": 命中的 B 日期}}, 已匹配的 B 日期集合）。
    """
    pos = {d: i for i, d in enumerate(cal_index)}
    b_pos = {d: pos[d] for d in b.index if d in pos}
    matched: dict = {}
    used_b: set = set()
    for a in a_dates:
        pa = pos.get(a)
        if pa is None:  # A 切换日不在湖内交易日历（窗口裁剪等）
            continue
        cands = [(abs(pb - pa), bd) for bd, pb in b_pos.items()
                 if abs(pb - pa) <= window and bd not in used_b]
        if not cands:
            continue
        dist, bd = min(cands, key=lambda t: (t[0], t[1] < a))
        matched[a] = {"offset": int(b_pos[bd] - pa),
                      "magnitude": float(b.loc[bd]),
                      "b_date": str(bd.date())}
        used_b.add(bd)
    return matched, used_b


def reconcile_sym(root: Path, sym0: str, tol: float, window: int,
                  start: str | None, end: str | None) -> dict:
    """单品种对账。快照或湖内数据缺失 → ``{"status": "skipped", ...}``。"""
    cal = dom.load_calendar(root, sym0)
    if cal is None:
        return {"sym0": sym0, "status": "skipped", "reason": "无 dominant 快照"}
    df = _load_processed(root, sym0)
    if df is None:
        return {"sym0": sym0, "status": "skipped", "reason": "湖内无 processed 数据"}

    sw = dom.detect_switches(cal)
    # 段首无 from（非真换月）+ 跨段接缝（日历间隔 >45 日历日的"切换"
    # 是两段快照拼接的伪切换，如 rb0 2020 段末→2023 段首）均不计入 A
    gaps = cal.index.to_series().diff()
    seam = cal.index[(gaps > pd.Timedelta(days=45)).fillna(False)]
    sw = sw[sw["from_id"].notna()
            & ~sw["date"].isin(set(seam) | {cal.index[0]})]
    coverage = (str(cal.index[0].date()), str(cal.index[-1].date()))
    a_dates = [d for d in sw["date"]
               if (start is None or d >= pd.Timestamp(start))
               and (end is None or d <= pd.Timestamp(end))]

    b = _factor_jump_dates(df, tol)
    if start:
        b = b[b.index >= pd.Timestamp(start)]
    if end:
        b = b[b.index <= pd.Timestamp(end)]
    # 未匹配 B 只在**快照覆盖区间**内归类 —— 区间外快照本无 A 数据，
    # 属"暂未入库"而非"dominant 未登记"
    b_cov = b[(b.index >= cal.index[0]) & (b.index <= cal.index[-1])]
    b_outside = int(len(b) - len(b_cov))

    matched, used_b = _match(a_dates, b_cov, window, df.index)
    unmatched_b = sorted(set(b_cov.index) - used_b)

    # 未匹配 B 细分：单日回落尖峰（含次日回复跳 —— 单日毛刺天然产生
    # "下跳 + 回复"两个突变，须成对消费）vs 持续阶梯
    m = (df.get("raw_close", pd.Series(dtype=float)) > 0) & \
        (df.get("adj_close", pd.Series(dtype=float)) > 0)
    ratio = (df.loc[m, "adj_close"] / df.loc[m, "raw_close"]).sort_index()
    ratio = ratio[~ratio.index.duplicated(keep="last")]
    pos_of = {d: i for i, d in enumerate(ratio.index)}
    unmatched_pos = sorted(pos_of[d] for d in unmatched_b if d in pos_of)
    spikes, steps = [], []
    consumed: set[int] = set()
    for i in unmatched_pos:
        if i in consumed:
            continue
        before = ratio.iloc[i - 1] if i > 0 else None
        after = ratio.iloc[i + 1] if i + 1 < len(ratio) else None
        if before is not None and after is not None and \
                abs(float(after) / float(before) - 1) <= tol:
            # V 形尖峰：次日比值回到突变前水平
            spikes.append(str(ratio.index[i].date()))
            consumed.add(i)
            # 成对消费次日回复跳（其水平 ≈ 毛刺前水平）
            j = i + 1
            if j in unmatched_pos and j < len(ratio) and \
                    abs(float(ratio.iloc[j]) / float(before) - 1) <= tol:
                spikes.append(str(ratio.index[j].date()))
                consumed.add(j)
        else:
            steps.append(str(ratio.index[i].date()))

    return {
        "sym0": sym0,
        "status": "ok",
        "tol": tol,
        "window_days": window,
        "snapshot_coverage": list(coverage),
        "b_outside_coverage": b_outside,
        "n_switches_a": len(a_dates),
        "n_jumps_b": int(len(b_cov)),
        "matched": {str(a.date()): v for a, v in matched.items()},
        "unmatched_a": [str(a.date()) for a in a_dates if a not in matched],
        "unmatched_b_spikes": spikes,   # 数据毛刺嫌疑（单日回落）
        "unmatched_b_steps": steps,     # roll 期震荡 / 口径事件（持续阶梯）
    }


def scan_spikes(root: Path, tol: float) -> list[dict]:
    """全湖单日回落尖峰扫描（不依赖快照）—— raw_close 口径残留定界。

    V 形签名：比值单日跳离且次日精确回落（含次日回复跳成对消费）。
    """
    out = []
    base = root / "processed"
    if not base.exists():
        return out
    for sym_dir in sorted(base.iterdir()):
        if not sym_dir.is_dir():
            continue
        df = None
        for freq in PROCESSED_LAYERS:
            df = _load_processed(root, sym_dir.name, freq)
            if df is not None:
                break
        if df is None:
            continue
        b = _factor_jump_dates(df, tol)
        m = (df.get("raw_close", pd.Series(dtype=float)) > 0) & \
            (df.get("adj_close", pd.Series(dtype=float)) > 0)
        if not m.any() or b.empty:
            continue
        ratio = (df.loc[m, "adj_close"] / df.loc[m, "raw_close"]).sort_index()
        ratio = ratio[~ratio.index.duplicated(keep="last")]
        pos_of = {d: i for i, d in enumerate(ratio.index)}
        spikes: list[str] = []
        consumed: set[int] = set()
        for d, _mag in b.items():
            i = pos_of.get(d)
            if i is None or i in consumed:
                continue
            before = ratio.iloc[i - 1] if i > 0 else None
            after = ratio.iloc[i + 1] if i + 1 < len(ratio) else None
            if before is not None and after is not None and \
                    abs(float(after) / float(before) - 1) <= tol:
                spikes.append(str(d.date()))
                consumed.add(i)
                # 次日回复跳（比值回到毛刺前水平）属同一毛刺事件，成对消费
                j = i + 1
                if j < len(ratio) and j not in consumed:
                    spikes.append(str(ratio.index[j].date()))
                    consumed.add(j)
        if spikes:
            out.append({"sym0": sym_dir.name, "spikes": spikes})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="dominant 快照 × 湖内因子 对账探针（只读）")
    ap.add_argument("--sym", help="品种列表（逗号分隔，如 cu0,rb0）")
    ap.add_argument("--all", action="store_true", help="扫快照目录全部品种")
    ap.add_argument("--data-root", default=str(DEFAULT_ROOT))
    ap.add_argument("--tol", type=float, default=1e-3,
                    help="比值突变阈值（默认 1e-3=10bp；真换月最小实测 62.7bp）")
    ap.add_argument("--window", type=int, default=3,
                    help="A↔B 匹配窗口（±交易日，默认 3）")
    ap.add_argument("--start", help="对账窗口起点（含）")
    ap.add_argument("--end", help="对账窗口终点（含）")
    ap.add_argument("--json", help="报告落盘路径（可选；默认仅 stdout，零写盘）")
    ap.add_argument("--spike-scan", action="store_true",
                    help="跳过 A↔B 对账，仅扫全湖单日回落毛刺（raw_close 口径残留定界）")
    args = ap.parse_args(argv)

    root = Path(args.data_root)

    if args.spike_scan:
        found = scan_spikes(root, args.tol)
        n = sum(len(f["spikes"]) for f in found)
        print(f"[SPIKE-SCAN] tol={args.tol} 全湖扫描：{len(found)} 品种 / {n} 处毛刺事件")
        for f in found:
            print(f"    {f['sym0']}: {f['spikes']}")
        if not found:
            print("    （未发现单日回落尖峰）")
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(found, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            print(f"[WRITE] {args.json}")
        return 0

    if args.all:
        cal_dir = root / "interim" / "dominant"
        syms = sorted(p.stem for p in cal_dir.glob("*.parquet"))
    elif args.sym:
        syms = [s.strip().lower() for s in args.sym.split(",") if s.strip()]
    else:
        ap.error("需要 --sym 或 --all")
    if not syms:
        print("[FAIL] 品种列表为空")
        return 2

    reports = [reconcile_sym(root, s, args.tol, args.window, args.start, args.end)
               for s in syms]

    n_a, n_unmatched_a = 0, 0
    for r in reports:
        if r["status"] != "ok":
            print(f"[SKIP] {r['sym0']}: {r['reason']}")
            continue
        n_a += r["n_switches_a"]
        n_unmatched_a += len(r["unmatched_a"])
        n_spike, n_step = len(r["unmatched_b_spikes"]), len(r["unmatched_b_steps"])
        offs = [v["offset"] for v in r["matched"].values()]
        off_str = (f"偏移 min={min(offs)}/max={max(offs)}" if offs else "无命中")
        cov = "/".join(r["snapshot_coverage"])
        print(f"[RECONCILE] {r['sym0']}（快照覆盖 {cov}）: "
              f"A 切换 {r['n_switches_a']} | 覆盖内 B 突变 {r['n_jumps_b']}"
              f"（覆盖外 {r['b_outside_coverage']} 暂未入库）| "
              f"匹配 {len(r['matched'])}（{off_str}）| "
              f"A 无因子 {len(r['unmatched_a'])} | 毛刺嫌疑 {n_spike} | "
              f"阶梯待复核 {n_step}")
        if r["unmatched_a"]:
            print(f"    A 无因子（dominant 切了但因子没动，ni0 型）: {r['unmatched_a']}")
        if r["unmatched_b_spikes"]:
            print(f"    🔴 毛刺嫌疑（单日回落，raw_close 口径残留?）: {r['unmatched_b_spikes']}")
        if r["unmatched_b_steps"]:
            print(f"    阶梯待复核（roll 期震荡/口径事件）: {r['unmatched_b_steps']}")

    ok = [r for r in reports if r["status"] == "ok"]
    verdict = "✅ 全部命中" if ok and n_unmatched_a == 0 else \
              "🟡 存在未命中（见明细，人工复核后再用于 graft rollover_dates）"
    print(f"[SUMMARY] 品种 {len(ok)}/{len(reports)} 对账完成 | A 总切换 {n_a} | "
          f"未命中 {n_unmatched_a} | {verdict}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(reports, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"[WRITE] {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
