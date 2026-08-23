"""P1 单因子 IC 验证 v2：品种内时序 RankIC（正确口径）。

关键方法论修正（v1 教训）：
  - 截面 IC 是错误口径：不同品种基差率水平不可比（RB 6~10% vs AU -0.6% vs SC -50%），
    截面排序会被品种水平主导，导致 IS 强 OOS 崩的假象。
  - 基差收敛是【品种内时序】信号：同一品种基差率偏离自身水平 → 期货向现货收敛。
  - 正确口径：per-symbol 时序 Spearman IC，再横截面平均；嵌套 cal_split=0.5。

判定门槛（防 vix 重演）：
  - |OOS IC| >= 0.03 且 IS/OOS 同号 → PASS
  - 附：2024-07-18 后（PandaData 真新数据）超严格 OOS 复核

用法：
  python scripts/p1_factor_ic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

FUND = Path("data/raw/fundamental")
KLINE = Path("data/raw/processed")

SYMS = ["ag", "al", "au", "cf", "cu", "hc", "i", "j", "jm", "m", "ni", "p", "rb", "sc", "sr", "ta", "y", "zn"]
HORIZONS = [5, 10, 20]
IC_THRESHOLD = 0.03
STRICT_OOS = pd.Timestamp("2024-07-18")  # PandaData 独立采集起点


def load_kline(sym: str) -> pd.DataFrame:
    frames = []
    d = KLINE / f"{sym}0" / "1d"
    if not d.exists():
        return pd.DataFrame()
    for f in sorted(d.glob("*.parquet")):
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    kdf = pd.concat(frames, ignore_index=True)
    kdf["date"] = pd.to_datetime(kdf["datetime"])
    return kdf[["date", "close"]].sort_values("date").drop_duplicates("date").set_index("date")


def load_fund(sym: str) -> pd.DataFrame:
    dfs = []
    for metric in ("basis", "warehouse"):
        f = FUND / f"{metric}_{sym.upper()}.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df.set_index("date"))
    if not dfs:
        return pd.DataFrame()
    return dfs[0].join(dfs[1], how="outer").sort_index()


def build_panel() -> pd.DataFrame:
    rows = []
    for sym in SYMS:
        k = load_kline(sym)
        f = load_fund(sym)
        if k.empty or f.empty:
            continue
        for h in HORIZONS:
            k[f"fwd_{h}"] = k["close"].shift(-h) / k["close"] - 1.0
        m = f.join(k[["close"] + [f"fwd_{h}" for h in HORIZONS]], how="inner")
        m["symbol"] = sym
        rows.append(m.reset_index())
    return pd.concat(rows, ignore_index=True)


def per_symbol_ts_ic(seg: pd.DataFrame, factor: str, h: int, min_n: int = 60) -> np.ndarray:
    ics = []
    for sym, grp in seg.groupby("symbol"):
        g = grp[[factor, f"fwd_{h}"]].dropna()
        if len(g) >= min_n:
            ic = g[factor].corr(g[f"fwd_{h}"], method="spearman")
            if pd.notna(ic):
                ics.append(ic)
    return np.array(ics)


def monotonicity_ts(seg: pd.DataFrame, factor: str, h: int) -> pd.DataFrame:
    """品种内分位单调性：每品种 factor 5 分位 → fwd 收益均值，再跨品种平均。"""
    rows = []
    for sym, grp in seg.groupby("symbol"):
        g = grp[[factor, f"fwd_{h}"]].dropna()
        if len(g) < 100:
            continue
        try:
            g["q"] = pd.qcut(g[factor], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        r = g.groupby("q")[f"fwd_{h}"].mean()
        for q, v in r.items():
            rows.append({"symbol": sym, "q": q, "ret": v})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.groupby("q")["ret"].agg(["mean", "count"]).sort_index()


def main() -> None:
    print("=" * 90)
    print("P1 单因子 IC 验证 v2 — 品种内时序 RankIC（嵌套 cal_split=0.5 | 门槛 |IC|>=0.03 且同向）")
    print("=" * 90)
    panel = build_panel()
    print(f"面板: {len(panel)} 行 | {panel['symbol'].nunique()} 品种 | "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}")
    dates = sorted(panel["date"].unique())
    split_dt = dates[len(dates) // 2]
    print(f"嵌套切分: {split_dt.date()}")
    print()

    factors = [
        ("basis_ratio", "基差率(%)"),
        ("basis", "基差(绝对)"),
        ("wr_change", "仓单变化(吨)"),
        ("wr_lot_change", "仓单变化(手)"),
    ]
    summary = []
    for factor, label in factors:
        if factor not in panel.columns:
            print(f"[SKIP] {label}: 列缺失")
            continue
        is_seg = panel[panel["date"] <= split_dt]
        oos_seg = panel[panel["date"] > split_dt]
        strict_seg = panel[panel["date"] >= STRICT_OOS]
        print(f"--- {label} ({factor}) ---")
        for h in HORIZONS:
            is_arr = per_symbol_ts_ic(is_seg, factor, h)
            oos_arr = per_symbol_ts_ic(oos_seg, factor, h)
            strict_arr = per_symbol_ts_ic(strict_seg, factor, h) if len(strict_seg) else np.array([])
            if len(is_arr) == 0 or len(oos_arr) == 0:
                print(f"  h={h:>2}: 样本不足")
                continue
            t_oos = oos_arr.mean() / (oos_arr.std() / np.sqrt(len(oos_arr))) if oos_arr.std() > 0 else 0.0
            same = np.sign(is_arr.mean()) == np.sign(oos_arr.mean())
            strong = abs(oos_arr.mean()) >= IC_THRESHOLD
            verdict = "PASS" if (strong and same) else ("WEAK" if same else "REVERSED")
            strict_txt = ""
            if len(strict_arr):
                t_st = strict_arr.mean() / (strict_arr.std() / np.sqrt(len(strict_arr))) if strict_arr.std() > 0 else 0.0
                strict_txt = f" | 2024-07后严格OOS IC={strict_arr.mean():+.4f}(t={t_st:+.2f},n={len(strict_arr)})"
            print(f"  h={h:>2}: IS IC={is_arr.mean():+.4f}(n={len(is_arr)}) | "
                  f"OOS IC={oos_arr.mean():+.4f}(t={t_oos:+.2f},正比={np.mean(oos_arr>0):.2f},n={len(oos_arr)})"
                  f"{strict_txt} → {verdict}")
            summary.append({"factor": factor, "label": label, "h": h,
                            "is_ic": is_arr.mean(), "oos_ic": oos_arr.mean(),
                            "oos_t": t_oos, "verdict": verdict,
                            "strict_oos_ic": strict_arr.mean() if len(strict_arr) else np.nan})
        mono = monotonicity_ts(oos_seg, factor, 10)
        if not mono.empty:
            print("  OOS h=10 品种内分位单调性 (Q1→Q5 平均未来收益): "
                  + " | ".join(f"Q{q}:{mono.loc[q,'mean']:+.4f}" for q in mono.index))
        print()

    print("=" * 90)
    print("判定汇总")
    print("=" * 90)
    sdf = pd.DataFrame(summary)
    if sdf.empty:
        print("无有效因子")
        return
    for factor in sdf["factor"].unique():
        sub = sdf[sdf["factor"] == factor]
        best = sub.loc[sub["oos_ic"].abs().idxmax()]
        final = "PASS" if best["verdict"] == "PASS" else ("WEAK" if best["verdict"] == "WEAK" else "NOT_PASS")
        print(f"  {factor:16s} best h={best['h']}: OOS IC={best['oos_ic']:+.4f} (t={best['oos_t']:+.2f}) → {final}")
    sdf.to_csv("artifacts/p1_factor_ic.csv", index=False)
    print("\n[OK] 结果 → artifacts/p1_factor_ic.csv")


if __name__ == "__main__":
    main()
