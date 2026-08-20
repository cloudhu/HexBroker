"""P4-3 仓单周度复验：wr_change 5d/20d 滚动求和因子 品种内时序 RankIC。

背景
----
P1 日度仓单因子验证结论：wr_change（日度）为 WEAK/REVERSED，未过门槛
（IS IC≈-0.04~-0.06，OOS 变号/减弱），已否决日度仓单因子。
P4-3 用周度聚合（滚动 5 日/20 日求和）复验：
  - wr_change_5d  = wr_change 滚动 5 日求和
  - wr_change_20d = wr_change 滚动 20 日求和
  - 附加：wr_change_5d_pct / wr_change_20d_pct（变化率 = 求和 / 前值仓单量）
  - 品种内时序 RankIC（嵌套 cal_split=0.5，PandaData 2024-07-18 后严格 OOS 复核）
  - 门槛：|OOS IC|>=0.03 且 IS/OOS 同号
  - PASS → 给出是否可作为引擎B 辅助因子建议；NOT_PASS → 确认否决（与 P1 日度一致）

口径（铁律）：品种内时序 RankIC（禁止截面IC）；OOS 只做终裁。

用法：
  python scripts/p4_warehouse_weekly.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.build_signals18 import SYMBOLS18
from scripts.p1_factor_ic import IC_THRESHOLD, STRICT_OOS, load_kline, per_symbol_ts_ic

IC_HORIZONS = [5, 10, 20]
MIN_N = 60
FACTORS = ["wr_change_5d", "wr_change_20d", "wr_change_5d_pct", "wr_change_20d_pct"]
ART = ROOT / "artifacts"


def build_warehouse_ic_panel() -> pd.DataFrame:
    """仓单面板 + 5d/20d 滚动求和因子 + 前瞻收益 → 长表（symbol/date 列）。"""
    rows = []
    fund = ROOT / "data/raw/fundamental"
    for sym in SYMBOLS18:
        sym_u = sym[:-1].upper()
        f = fund / f"warehouse_{sym_u}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates("date").set_index("date")
        df["symbol"] = sym
        df["wr_change_5d"] = df["wr_change"].rolling(5).sum()
        df["wr_change_20d"] = df["wr_change"].rolling(20).sum()
        # 变化率（相对前值仓单量水平）
        df["wr_change_5d_pct"] = df["wr_change_5d"] / df["wr_quantity"].shift(5)
        df["wr_change_20d_pct"] = df["wr_change_20d"] / df["wr_quantity"].shift(20)
        df = df.replace([np.inf, -np.inf], np.nan)
        k = load_kline(sym[:-1])  # p1.load_kline 内部会补 '0'，这里传去掉尾部 0 的品种名
        for h in IC_HORIZONS:
            k[f"fwd_{h}"] = k["close"].shift(-h) / k["close"] - 1.0
        m = df.join(k[["close"] + [f"fwd_{h}" for h in IC_HORIZONS]], how="inner")
        rows.append(m.reset_index())
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    print("=" * 96)
    print("P4-3 仓单周度复验：wr_change_5d/20d（滚动求和）+ 变化率 — 品种内时序 RankIC")
    print("=" * 96)

    panel = build_warehouse_ic_panel()
    dates = sorted(panel["date"].unique())
    split_dt = dates[len(dates) // 2]
    print(f"面板: {len(panel)} 行 | {panel['symbol'].nunique()} 品种 | "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}")
    print(f"嵌套切分(cal_split=0.5): {split_dt.date()} | 严格OOS: {STRICT_OOS.date()} | "
          f"门槛 |OOS IC|>={IC_THRESHOLD} 且 IS/OOS 同号\n")

    is_seg = panel[panel["date"] <= split_dt]
    oos_seg = panel[panel["date"] > split_dt]
    strict_seg = panel[panel["date"] >= STRICT_OOS]

    summary = []
    print("--- 品种内时序 RankIC（嵌套） ---")
    for factor in FACTORS:
        if factor not in panel.columns:
            print(f"[SKIP] {factor}: 列缺失")
            continue
        print(f"[因子] {factor}")
        best = None
        for h in IC_HORIZONS:
            is_arr = per_symbol_ts_ic(is_seg, factor, h, min_n=MIN_N)
            oos_arr = per_symbol_ts_ic(oos_seg, factor, h, min_n=MIN_N)
            strict_arr = per_symbol_ts_ic(strict_seg, factor, h, min_n=MIN_N) if len(strict_seg) else np.array([])
            if len(is_arr) == 0 or len(oos_arr) == 0:
                print(f"  h={h:>2}: 样本不足")
                continue
            is_ic, oos_ic = float(is_arr.mean()), float(oos_arr.mean())
            same = np.sign(is_ic) == np.sign(oos_ic)
            strong = abs(oos_ic) >= IC_THRESHOLD
            verdict = "PASS" if (strong and same) else ("WEAK" if same else "REVERSED")
            strict_ic = float(strict_arr.mean()) if len(strict_arr) else np.nan
            print(f"  h={h:>2}: IS IC={is_ic:+.4f}(n={len(is_arr)}) | "
                  f"OOS IC={oos_ic:+.4f}(n={len(oos_arr)}) | "
                  f"strict IC={strict_ic:+.4f} → {verdict}")
            row = {"factor": factor, "h": h, "is_ic": is_ic, "oos_ic": oos_ic,
                   "is_n": len(is_arr), "oos_n": len(oos_arr),
                   "strict_oos_ic": strict_ic, "verdict": verdict}
            summary.append(row)
            if best is None or abs(oos_ic) > abs(best["oos_ic"]):
                best = dict(row)
        if best:
            print(f"  >> 最佳 h={best['h']}: IS IC={best['is_ic']:+.4f} | "
                  f"OOS IC={best['oos_ic']:+.4f} → {best['verdict']}")
        print()

    sdf = pd.DataFrame(summary)
    ART.mkdir(exist_ok=True)
    sdf.to_csv(ART / "p4_warehouse_ic.csv", index=False)
    print(f"[OK] IC 结果 → {ART / 'p4_warehouse_ic.csv'}\n")

    # ---- 判定 ----
    print("=" * 96)
    print("判定汇总（门槛 |OOS IC|>=0.03 且 IS/OOS 同号 → PASS）")
    print("=" * 96)
    any_pass = False
    for factor in FACTORS:
        sub = sdf[sdf["factor"] == factor]
        if sub.empty:
            continue
        best = sub.loc[sub["oos_ic"].abs().idxmax()]
        final = "PASS" if best["verdict"] == "PASS" else "NOT_PASS"
        if best["verdict"] == "PASS":
            any_pass = True
        print(f"  {factor:22s} best h={best['h']:>2}: IS IC={best['is_ic']:+.4f} | "
              f"OOS IC={best['oos_ic']:+.4f} | strict IC={best['strict_oos_ic']:+.4f} → {final}")

    print()
    if any_pass:
        print("结论: 周度仓单因子 PASS —— 可考虑作为引擎B 辅助因子（需在 IS 上定参后再做组合验证）")
    else:
        print("结论: 周度仓单因子 NOT_PASS —— 确认否决（与 P1 日度仓单因子结论一致），"
              "不建议作为引擎B 辅助因子")


if __name__ == "__main__":
    main()
