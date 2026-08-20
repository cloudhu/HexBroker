"""QA 独立交叉验证：不调用 p12_s4_shadow_monitor 任何函数，自行实现口径复算。

复算目标（与工程师报告对照）：
  1) v2 / rt30 滚动 63 窗池化 Spearman（期望 0.6037, n_pairs=453）
  2) v2 滚动 IC 末值 -0.3131（末值日 2026-06-10）；rt30 -0.0257（2026-07-24）
  3) 信号相关性日截面滚动均值 0.3462
  4) 滚动命中率末值 v2 0.375 / rt30 0.528
  5) 触发计数：IC 连续胜 7、命中率连续胜 3（对齐日 44）

口径（与 P12 文档一致，自行实现）：
  - fwd5 = close[t+5]/close[t]-1，品种内 shift，仅已实现收益
  - 日截面 Spearman(exp_ret, fwd5)，仅 OOS>=2024-07-18，当日品种>=3
  - 63 窗滚动均值（满窗，尾随）
  - 命中率 = 每日 exp_ret 截面 top30% 品种 fwd5>0 占比
  - 池化 = 最近 63 个重合日全部 (symbol,ts) 对一次 Spearman
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ART = ROOT / "artifacts"
KLINE_DIR = ROOT / "data" / "raw" / "processed"
OOS_START = pd.Timestamp("2024-07-18")
HORIZON = 5
WINDOW = 63
TOP_K = 0.30
MIN_SYMBOLS = 3


def sp(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2 or np.isnan(a).any() or np.isnan(b).any():
        return float("nan")
    return float(spearmanr(a, b).statistic)


def load_close():
    frames = []
    for sym_dir in sorted(KLINE_DIR.iterdir()):
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        d = sym_dir / "1d"
        if not d.exists():
            continue
        for f in sorted(d.glob("*.parquet")):
            try:
                df = pd.read_parquet(f)
            except Exception:
                continue
            if "datetime" not in df.columns or "close" not in df.columns:
                continue
            sub = df[["datetime", "close"]].copy()
            sub["date"] = pd.to_datetime(sub["datetime"]).dt.normalize()
            sub["symbol"] = sym
            frames.append(sub[["symbol", "date", "close"]])
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    return panel.reset_index(drop=True)


def load_sig(path):
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts"]).dt.normalize()
    return df


def add_fwd(sig, panel):
    close = panel.set_index(["symbol", "date"])["close"].sort_index()
    fwd = close.groupby(level="symbol").shift(-HORIZON) / close - 1.0
    fwd.name = f"fwd_{HORIZON}"
    fwd.index = fwd.index.set_names(["symbol", "ts"])
    return sig.join(fwd, on=["symbol", "ts"])


def daily_ic(sig):
    col = f"fwd_{HORIZON}"
    df = sig[(sig["ts"] >= OOS_START) & sig[col].notna()]
    rows = []
    for ts, g in df.groupby("ts"):
        if len(g) < MIN_SYMBOLS:
            continue
        r = sp(g["exp_ret"], g[col])
        if pd.notna(r):
            rows.append((ts, r, len(g)))
    out = pd.DataFrame(rows, columns=["ts", "ic", "n"]).sort_values("ts").reset_index(drop=True)
    return out


def daily_hit(sig):
    col = f"fwd_{HORIZON}"
    df = sig[(sig["ts"] >= OOS_START) & sig[col].notna()].copy()
    df["rp"] = df.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    rows = []
    for ts, g in df.groupby("ts"):
        if len(g) < MIN_SYMBOLS:
            continue
        top = g[g["rp"] >= 1.0 - TOP_K]
        if len(top) == 0:
            continue
        rows.append((ts, float((top[col] > 0).mean()), len(top)))
    out = pd.DataFrame(rows, columns=["ts", "hit", "n_top"]).sort_values("ts").reset_index(drop=True)
    return out


def roll_mean(s, window=WINDOW):
    return s.rolling(window, min_periods=window).mean()


def overlap(sig_rt, sig_v2):
    m = sig_rt[["symbol", "ts", "exp_ret"]].merge(
        sig_v2[["symbol", "ts", "exp_ret"]],
        on=["symbol", "ts"], suffixes=("_rt", "_v2"),
    )
    rows = []
    for ts, g in m.groupby("ts"):
        if len(g) < MIN_SYMBOLS:
            continue
        r = sp(g["exp_ret_rt"], g["exp_ret_v2"])
        if pd.notna(r):
            rows.append((ts, r, len(g)))
    c = pd.DataFrame(rows, columns=["ts", "corr", "n"]).sort_values("ts").reset_index(drop=True)
    return c, m


def pooled(c, m):
    tail = set(c["ts"].tail(WINDOW))
    sub = m[m["ts"].isin(tail)]
    return sp(sub["exp_ret_rt"], sub["exp_ret_v2"]), len(sub)


def main():
    print("=" * 78)
    print("QA 独立交叉验证（不复用 p12 函数）")
    print("=" * 78)
    panel = load_close()
    print(f"[env] kline 品种={panel['symbol'].nunique()} 日期范围={panel['date'].min().date()}~{panel['date'].max().date()}")

    sig_v2 = add_fwd(load_sig(ART / "signals_cache18_grouped_v2.parquet"), panel)
    sig_rt = add_fwd(load_sig(ART / "signals_cache18_grouped_v2_rt30.parquet"), panel)

    results = {}
    for label, sig in (("v2", sig_v2), ("rt30", sig_rt)):
        ic = daily_ic(sig)
        hit = daily_hit(sig)
        ic_r = roll_mean(ic.set_index("ts")["ic"])
        hit_r = roll_mean(hit.set_index("ts")["hit"])
        ic_last = float(ic_r.dropna().iloc[-1])
        hit_last = float(hit_r.dropna().iloc[-1])
        ic_d = ic_r.dropna().index[-1].date()
        hit_d = hit_r.dropna().index[-1].date()
        results[label] = dict(
            n_rows=len(sig), n_dates=sig["ts"].nunique(),
            oos_ic_days=len(ic), ic_last=ic_last, ic_last_date=ic_d,
            hit_last=hit_last, hit_last_date=hit_d,
        )
        print(f"\n[{label}] 行数={len(sig)} 日数={sig['ts'].nunique()} OOS_IC日={len(ic)}")
        print(f"  滚动IC末值 = {ic_last:+.6f} (末值日 {ic_d})   期望 -0.313078 / -0.025730")
        print(f"  滚动命中率末值 = {hit_last:.6f} (末值日 {hit_d})  期望 0.375397 / 0.527778")

    c, m = overlap(sig_rt, sig_v2)
    daily_r = roll_mean(c.set_index("ts")["corr"])
    daily_last = float(daily_r.dropna().iloc[-1])
    daily_last_d = daily_r.dropna().index[-1].date()
    pooled_corr, n_pairs = pooled(c, m)
    print(f"\n[相关性] 重合日={len(c)} 末重合日={c['ts'].max().date()}")
    print(f"  日截面滚动均值 = {daily_last:.6f} (末值日 {daily_last_d})   期望 0.346176")
    print(f"  池化63窗 = {pooled_corr:.6f} (n_pairs={n_pairs})   期望 0.603704 / 453")

    # 触发计数（对齐日上的尾部连续优势）
    ic_rt = roll_mean(daily_ic(sig_rt).set_index("ts")["ic"])
    ic_v2 = roll_mean(daily_ic(sig_v2).set_index("ts")["ic"])
    hit_rt = roll_mean(daily_hit(sig_rt).set_index("ts")["hit"])
    hit_v2 = roll_mean(daily_hit(sig_v2).set_index("ts")["hit"])
    common = ic_rt.dropna().index.intersection(ic_v2.dropna().index)
    ic_win = ((ic_rt.reindex(common) > ic_v2.reindex(common)) &
              (ic_rt.reindex(common).abs() > 0.03))
    hit_win = ((hit_rt.reindex(common) > hit_v2.reindex(common)) &
               (hit_rt.reindex(common) > 0.50))
    def consec(b):
        n = 0
        for v in b.iloc[::-1]:
            if v:
                n += 1
            else:
                break
        return n
    print(f"\n[触发] 对齐日={len(common)} IC连续胜={consec(ic_win)} 命中率连续胜={consec(hit_win)}"
          f"   期望 44 / 7 / 3")
    checks = [
        abs(results['v2']['ic_last'] - (-0.313078)) < 1e-3,
        abs(results['rt30']['ic_last'] - (-0.025730)) < 1e-3,
        abs(results['v2']['hit_last'] - 0.375397) < 1e-3,
        abs(results['rt30']['hit_last'] - 0.527778) < 1e-3,
        abs(pooled_corr - 0.603704) < 1e-3,
        abs(daily_last - 0.346176) < 1e-3,
    ]
    print(f"\n[小结] 全部复算值与工程师基线快照一致：{all(checks)}")
    for name, ok in zip(
        ["v2_ic", "rt30_ic", "v2_hit", "rt30_hit", "pooled_corr", "daily_corr"], checks):
        print(f"  {name}: {'OK' if ok else 'MISMATCH'}")


if __name__ == "__main__":
    main()
