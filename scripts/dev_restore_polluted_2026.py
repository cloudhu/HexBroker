"""恢复 2026-08-29 11:49 被 pytest 污染的 processed 年度分区（P0-9a）。

事故背景
--------
``hexbroker/data/sources/test_akshare_source.py`` 的测试用 ``AkshareSource()``
默认 ``save=True`` 落盘，``DataLake.save_processed`` 以 ``write_parquet``
**整文件覆盖**（非 merge），把合成的 2~3 行数据写进了生产分区；且因
``DataLake`` 复用同一 root，cu0 与 rb0 之间发生了**跨品种串扰**
（cu0/2026 的内容实际是 rb0 的价格）。

受损清单（mtime 均为 2026-08-29 11:49:23，全盘 162 个分区中仅此 3 个 parquet）：
    data/raw/processed/rb0/1d/2020.parquet   9133 B  2 行
    data/raw/processed/rb0/1d/2026.parquet   9266 B  3 行
    data/raw/processed/cu0/1d/2026.parquet   9266 B  3 行（内容是 rb0 的价格）
    + 2 个 manifest.json（其余 16 个品种本就没有 manifest）

恢复策略（2026 年度）：真值重建，不用外推
-----------------------------------------
1. 基线：``artifacts/backup_p64/{sym}_2026_prefill_20260829_0939*.parquet``
   （pandadata 主源 ``close_pcr`` 后复权真值，155 行，截至 2026-08-27）。
2. 缺口 08-28 一天：主源（pandadata MCP）token 虽已失效，但
   ``artifacts/p6_4_pull_20260828_d1/{sym}.json`` 存有该日**主源原始返回的
   逐字落盘**（证明：ni0 该记录与 ni0 生产行逐位相同）。直接取真值，
   **无需 graft 外推，结果非 provisional**。
3. graft 续接结果作为**独立交叉验证**通道并行计算，与真值比对（不落盘）。

字段映射（经 ni0 对照组逐字段验证通过）
---------------------------------------
    open/high/low/close  <- 直取（已是 close_pcr 后复权）
    volume/amount/open_interest <- 直取
    raw_close = adj_close <- close（pandadata 只供复权价，raw_close 原样备份）
    limit_up/limit_down   <- bool(high >= limit_up) / bool(low <= limit_down)
    is_rollover           <- 由 adj/raw 比值是否恒定判定（恒定即未换月）

用法
----
    python scripts/dev_restore_polluted_2026.py            # 演练（默认，不落盘）
    python scripts/dev_restore_polluted_2026.py --apply     # 实写
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data.graft import graft_adjusted, verify_alignment  # noqa: E402
from hexbroker.data.schema import BarFrame  # noqa: E402
from hexbroker.data.store import DataLake  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PULL_DIR = ROOT / "artifacts" / "p6_4_pull_20260828_d1"
TARGET_DATE = pd.Timestamp("2026-08-28")

#: 待恢复目标：(symbol, year, 备份相对路径)
TARGETS = [
    ("cu0", 2026, "artifacts/backup_p64/cu0_2026_prefill_20260829_093952.parquet"),
    ("rb0", 2026, "artifacts/backup_p64/rb0_2026_prefill_20260829_093958.parquet"),
]

#: **对照组**：未受损品种，用于验证重建逻辑能精确复现生产行（回归门禁）
CONTROL = ("ni0", 2026)

#: 生产分区应有的列序（与 ni0 对齐；注意**不含** akshare 独有的 settlement）
EXPECTED_COLS = [
    "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
    "open_interest", "raw_close", "adj_close", "limit_up", "limit_down",
    "is_rollover",
]


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
def _load_backup(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    return df.sort_values("datetime").reset_index(drop=True)[EXPECTED_COLS]


def _load_pull(symbol: str, date: pd.Timestamp) -> dict:
    """从主源原始落盘取真值。"""
    p = PULL_DIR / f"{symbol}.json"
    if not p.exists():
        raise FileNotFoundError(f"主源落盘缺失: {p}")
    d = json.loads(p.read_text(encoding="utf-8"))
    cols = d["result"]["columns"]
    rows = [dict(zip(cols, r)) for r in d["result"]["rows"]]
    hit = [r for r in rows if r["date"] == date.strftime("%Y%m%d")]
    if not hit:
        raise ValueError(f"{symbol} 在 {p.name} 中无 {date:%Y-%m-%d} 的记录")
    return hit[0]


def _fetch_raw(symbol: str, start: str, end: str) -> pd.DataFrame:
    """备源名义价（仅用于交叉验证与换月判定）。save=False 是硬要求。"""
    from hexbroker.data.sources.akshare_source import AkshareSource

    src = AkshareSource(save=False)
    df = src.fetch_bars([symbol], start, end, "1d").df.reset_index()
    df = df[df["symbol"] == symbol].copy()
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    return df.sort_values("datetime").reset_index(drop=True)


# --------------------------------------------------------------------------
# 字段映射（经 ni0 对照组验证）
# --------------------------------------------------------------------------
def build_row(symbol: str, rec: dict, prev_ratio: float | None,
              cur_ratio: float | None) -> dict:
    high, low = float(rec["high"]), float(rec["low"])
    close = float(rec["close"])
    # 比值恒定 => 未换月。prev/cur 任一缺失时保守判 False。
    if prev_ratio is not None and cur_ratio is not None:
        rolled = abs(cur_ratio / prev_ratio - 1.0) > 1e-6
    else:
        rolled = False
    return {
        "symbol": symbol,
        "datetime": pd.Timestamp(rec["date"]),
        "open": float(rec["open"]),
        "high": high,
        "low": low,
        "close": close,
        "volume": float(rec["volume"]),
        "amount": float(rec["amount"] or 0.0),
        "open_interest": float(rec["open_interest"]),
        "raw_close": close,
        "adj_close": close,
        "limit_up": bool(high >= float(rec["limit_up"])),
        "limit_down": bool(low <= float(rec["limit_down"])),
        "is_rollover": bool(rolled),
    }


# --------------------------------------------------------------------------
# 对照组回归：验证映射能精确复现未受损品种的生产行
# --------------------------------------------------------------------------
def run_control() -> dict:
    sym, year = CONTROL
    out: dict = {"symbol": sym, "year": year}
    prod_path = ROOT / "data" / "raw" / "processed" / sym / "1d" / f"{year}.parquet"
    prod = pd.read_parquet(prod_path)
    prod["datetime"] = pd.to_datetime(prod["datetime"]).dt.tz_localize(None)
    actual = prod[prod["datetime"] == TARGET_DATE]
    if actual.empty:
        return {"status": "SKIP", "reason": f"{sym} 无 {TARGET_DATE:%Y-%m-%d} 生产行"}
    actual = actual.iloc[0]

    rec = _load_pull(sym, TARGET_DATE)
    raw = _fetch_raw(sym, "2026-08-01", TARGET_DATE.strftime("%Y-%m-%d"))
    raw_s = raw.set_index("datetime")["close"].astype(float)
    adj_s = prod.set_index("datetime")["close"].astype(float)
    prev_d = adj_s.index[adj_s.index < TARGET_DATE].max()
    prev_ratio = float(adj_s.loc[prev_d] / raw_s.loc[prev_d])
    cur_ratio = float(adj_s.loc[TARGET_DATE] / raw_s.loc[TARGET_DATE])

    built = build_row(sym, rec, prev_ratio, cur_ratio)

    diffs = []
    for c in EXPECTED_COLS:
        a, b = actual[c], built[c]
        if c == "datetime":
            same = pd.Timestamp(a) == pd.Timestamp(b)
        elif c == "symbol":
            same = str(a) == str(b)
        elif isinstance(a, (bool,)) or isinstance(b, (bool,)):
            same = bool(a) == bool(b)
        else:
            same = abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
        if not same:
            diffs.append({"col": c, "actual": str(a), "built": str(b)})

    out["status"] = "PASS" if not diffs else "FAIL"
    out["n_diff"] = len(diffs)
    out["diffs"] = diffs
    out["actual_close"] = float(actual["close"])
    out["built_close"] = float(built["close"])
    return out


# --------------------------------------------------------------------------
# 单品种恢复
# --------------------------------------------------------------------------
def restore_one(symbol: str, year: int, backup_rel: str, apply: bool) -> dict:
    out: dict = {"symbol": symbol, "year": year, "backup": backup_rel}
    backup_path = ROOT / backup_rel
    if not backup_path.exists():
        out["status"] = "FAIL"
        out["reason"] = f"备份不存在: {backup_path}"
        return out

    bak = _load_backup(backup_path)
    out["backup_rows"] = int(len(bak))
    out["backup_range"] = [f"{bak['datetime'].min():%Y-%m-%d}",
                           f"{bak['datetime'].max():%Y-%m-%d}"]

    # --- 1) 取主源真值 ---
    rec = _load_pull(symbol, TARGET_DATE)
    out["pull_dominant"] = rec["dominant_id"]
    out["pull_method"] = rec.get("method")
    out["truth_close"] = float(rec["close"])

    # --- 2) 备源名义价：用于换月判定 + graft 交叉验证 ---
    raw = _fetch_raw(symbol, "2026-01-01", TARGET_DATE.strftime("%Y-%m-%d"))
    raw_s = raw.set_index("datetime")["close"].astype(float)
    adj_s = bak.set_index("datetime")["close"].astype(float)

    prev_d = adj_s.index.max()
    prev_ratio = float(adj_s.loc[prev_d] / raw_s.loc[prev_d])
    cur_ratio = float(rec["close"] / raw_s.loc[TARGET_DATE])
    out["ratio_prev"] = prev_ratio
    out["ratio_cur"] = cur_ratio
    out["ratio_jump_bp"] = (cur_ratio / prev_ratio - 1.0) * 1e4

    # --- 3) graft 独立交叉验证（不落盘）---
    res = graft_adjusted(adj_s, raw_s, lookback=60)
    al = verify_alignment(adj_s, raw_s, lookback=60)
    graft_val = float(res.series.loc[TARGET_DATE]) if TARGET_DATE in res.series.index else None
    out["cross_check"] = {
        "graft_value": graft_val,
        "truth_value": float(rec["close"]),
        "diff_bp": (abs(graft_val / float(rec["close"]) - 1.0) * 1e4
                    if graft_val else None),
        "graft_warnings": len(res.warnings),
        "align_cv": al["ratio_cv"],
        "align_breaks": len(al.get("breaks", [])),
    }

    # --- 4) 构造真值行并合并 ---
    row = build_row(symbol, rec, prev_ratio, cur_ratio)
    new_df = pd.DataFrame([row])[EXPECTED_COLS]
    merged = pd.concat([bak, new_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["datetime"], keep="last")
    merged = merged.sort_values("datetime").reset_index(drop=True)
    out["final_rows"] = int(len(merged))
    out["final_range"] = [f"{merged['datetime'].min():%Y-%m-%d}",
                          f"{merged['datetime'].max():%Y-%m-%d}"]
    out["appended"] = {k: (str(v) if k == "datetime" else v) for k, v in row.items()}

    target = ROOT / "data" / "raw" / "processed" / symbol / "1d" / f"{year}.parquet"
    out["target"] = str(target.relative_to(ROOT)).replace("\\", "/")

    if apply:
        stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        keep = ROOT / "artifacts" / "backup_p64" / "pre_restore" / f"{symbol}_{year}_{stamp}.parquet"
        keep.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, keep)
            out["pre_restore_backup"] = str(keep.relative_to(ROOT)).replace("\\", "/")

        merged_idx = merged.set_index(["symbol", "datetime"])
        DataLake(ROOT / "data" / "raw").save_processed(
            BarFrame(df=merged_idx, freq="1d", source="pandadata"),
            symbol=symbol,
        )
        out["status"] = "APPLIED"
    else:
        out["status"] = "DRY_RUN"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="恢复被 pytest 污染的 2026 年度分区")
    ap.add_argument("--apply", action="store_true", help="实写落盘（默认仅演练）")
    args = ap.parse_args()

    print("=" * 74)
    print("对照组回归（验证字段映射能精确复现未受损品种 ni0 的生产行）")
    print("=" * 74)
    ctrl = run_control()
    print(json.dumps(ctrl, ensure_ascii=False, indent=2, default=str))
    if ctrl["status"] == "FAIL":
        print("\n[中止] 对照组未通过，字段映射不可信，拒绝恢复。")
        raise SystemExit(2)
    if ctrl["status"] == "PASS":
        print(f"\n[PASS] 对照组逐字段一致（{len(EXPECTED_COLS)} 列，0 处差异）")

    print()
    print("=" * 74)
    print(f"恢复目标演练{'与落盘' if args.apply else '（未落盘）'}")
    print("=" * 74)
    reports = [restore_one(s, y, b, args.apply) for s, y, b in TARGETS]
    for r in reports:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    print()
    print("=" * 74)
    for r in reports:
        tag = {"DRY_RUN": "[演练]", "APPLIED": "[已落盘]",
               "FAIL": "[失败]"}.get(r["status"], r["status"])
        cc = r.get("cross_check", {})
        print(f"{tag} {r['symbol']}/{r['year']}: "
              f"{r.get('backup_rows')} 行 -> {r.get('final_rows')} 行 {r.get('final_range', r.get('reason',''))}")
        print(f"        真值 close={r.get('truth_close')}  "
              f"graft={cc.get('graft_value')}  偏差={cc.get('diff_bp'):.4f} bp"
              if cc.get("diff_bp") is not None else f"        真值 close={r.get('truth_close')}")
        print(f"        比值跳跃 {r.get('ratio_jump_bp'):.4f} bp  "
              f"graft告警 {cc.get('graft_warnings')}  对齐breaks {cc.get('align_breaks')}")
    if not args.apply:
        print("\n演练模式：未落盘。确认无误后加 --apply 实写。")


if __name__ == "__main__":
    main()
