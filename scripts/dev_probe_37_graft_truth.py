#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：用**真实数据**反证 graft_adjusted 的正确性（P0-5 端到端验证）。

思路（有真值对照，不是自证）：
  1. 主源真值 = pandadata close_pcr（artifacts/p6_4_pull_20260828/{sym}.json，10 行）
  2. 把真值**截断**到 cutoff（只保留前半段）当作"主源历史"
  3. 备源 = 新浪新端点主力连续名义价（真实联网抓取）
  4. graft_adjusted 续接出后半段
  5. 与**被截掉的 pandadata 真值后半段**逐日比对 —— 差异即真实误差

只读、无副作用：不写 parquet，不改 data/。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pandas as pd
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hexbroker.data.graft import graft_adjusted, verify_alignment  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SINA_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_"
            "{s}2021_08_17=/InnerFuturesNewService.getDailyKLine?symbol={s}&_=2021_08_17")

SYMBOLS = ["ag0", "al0", "au0", "cf0", "cu0", "hc0", "i0", "j0", "jm0", "m0",
           "ni0", "p0", "rb0", "sc0", "sr0", "ta0", "y0", "zn0"]


def sina_close(sym0: str) -> pd.Series:
    r = requests.get(SINA_URL.format(s=sym0), timeout=25, headers=UA)
    t = r.text
    i, j = t.find("(["), t.rfind("])")
    if i < 0 or j < 0:
        return pd.Series(dtype=float)
    rows = json.loads(t[i + 1:j + 1])
    s = pd.Series({pd.Timestamp(x["d"]): float(x["c"]) for x in rows}, dtype=float)
    return s.sort_index()


def panda_frame(sym0: str, pull_dir: pathlib.Path) -> pd.DataFrame | None:
    f = pull_dir / f"{sym0}.json"
    if not f.exists():
        return None
    res = json.loads(f.read_text(encoding="utf-8"))["result"]
    cols = res["columns"]
    df = pd.DataFrame(res["rows"], columns=cols)
    df["ts"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df.set_index("ts").sort_index()


def _err_bp(res, truth) -> pd.Series:
    pred = res.series.reindex(truth.index)
    return ((pred - truth) / truth * 1e4).abs()


def _run_ablation(syms, pull_dir: pathlib.Path, cutoff_default: str, days: int) -> None:
    """消融三种换月处理策略（P0-5 默认选择的取证依据）。

    A 无视换月      ：k 全程恒定（等价于"备源未换月"假设）
    B dominant+近似 ：按 pandadata dominant_id 换月，价差用 raw[R]/raw[R-1] 近似
    C dominant+精确 ：按 dominant_id 换月，价差由真值反解（**Oracle，仅作误差下界**）
    """
    hdr = (f"{'sym':5s} {'换月':>4s} {'A_无视bp':>10s} {'B_近似bp':>10s} "
           f"{'C_精确bp':>10s} {'最优':>6s} {'B劣化':>8s}")
    print("消融：三种换月处理策略的最大绝对误差（bp），续接窗口内逐日取最大")
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for sym in syms:
        pdf = panda_frame(sym, pull_dir)
        if pdf is None or pdf.empty:
            continue
        adj_all = pdf["close"].astype(float)
        cutoff = pd.Timestamp(cutoff_default)
        if days:
            cutoff = adj_all.index[min(days, len(adj_all) - 1) - 1]
        hist = adj_all[adj_all.index <= cutoff]
        truth = adj_all[adj_all.index > cutoff]
        if len(hist) < 2 or truth.empty:
            continue
        try:
            raw = sina_close(sym)
        except Exception:  # noqa: BLE001
            continue
        if raw.empty:
            continue

        dom = pdf["dominant_id"]
        rolls = [d for d in truth.index
                 if dom.get(d) != dom.shift(1).get(d)]
        exact = {}
        for d in rolls:
            prev = raw.index[raw.index < d][-1]
            exact[d] = (float(raw.loc[d]) / float(raw.loc[prev])) / (
                float(truth.loc[d]) / float(adj_all.loc[prev]))

        a = graft_adjusted(hist, raw)
        b = graft_adjusted(hist, raw, rollover_dates=rolls)
        c = graft_adjusted(hist, raw, rollover_dates=rolls,
                           rollover_spreads=exact or None)
        ea, eb, ec = (_err_bp(x, truth).max() for x in (a, b, c))
        best = min([("A", ea), ("B", eb), ("C", ec)], key=lambda t: t[1])[0]
        rows.append((sym, ea, eb, ec))
        print(f"{sym:5s} {len(rolls):>4d} {ea:>10.2f} {eb:>10.2f} {ec:>10.2f} "
              f"{best:>6s} {eb - ea:>+8.2f}")

    if not rows:
        return
    print()
    ea = sum(r[1] for r in rows) / len(rows)
    eb = sum(r[2] for r in rows) / len(rows)
    ec = sum(r[3] for r in rows) / len(rows)
    n_swap = sum(1 for r in rows if r[3] < r[1])
    print(f"均值：A={ea:.2f} bp  B={eb:.2f} bp  C={ec:.2f} bp "
          f"（{len(rows)} 品种）")
    print(f"B 相对 A 劣化的品种数：{sum(1 for r in rows if r[2] > r[1])}/{len(rows)}；"
          f"C 严格优于 A 的品种数：{n_swap}/{len(rows)}")
    print("注：C 为真值反解的 Oracle，仅用于给出误差下界，生产不可用。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-08-20", help="主源历史截断日（含）")
    ap.add_argument("--pull-dir", default="artifacts/p6_4_pull_20260828")
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--days", type=int, default=0,
                    help="0=用 cutoff；>0 则按保留前 N 行推导 cutoff")
    ap.add_argument("--detail", default="",
                    help="逗号分隔品种，逐日打印真值 vs 续接值（并做精确价差消融）")
    ap.add_argument("--ablation", action="store_true",
                    help="消融三种换月处理策略的默认选择")
    args = ap.parse_args()

    pull_dir = pathlib.Path(args.pull_dir)
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if args.ablation:
        _run_ablation(syms, pull_dir, cutoff_default=args.cutoff, days=args.days)
        return 0

    print(f"主源截断策略：cutoff={args.cutoff}（含），之后为待续接区")
    print()
    hdr = (f"{'sym':5s} {'锚点日':>10s} {'k0':>9s} {'重叠n':>5s} {'cv':>10s} "
           f"{'换月':>4s} {'续接n':>5s} {'最大误差bp':>11s} {'中位bp':>9s} {'告警':>4s}")
    print(hdr)
    print("-" * len(hdr))

    worst = []
    detail = {s.strip() for s in args.detail.split(",") if s.strip()}
    for sym in syms:
        pdf = panda_frame(sym, pull_dir)
        if pdf is None or pdf.empty:
            print(f"{sym:5s} pandadata 无数据")
            continue
        adj_all = pdf["close"].astype(float)

        cutoff = pd.Timestamp(args.cutoff)
        if args.days:
            cutoff = adj_all.index[min(args.days, len(adj_all) - 1) - 1]
        adj_hist = adj_all[adj_all.index <= cutoff]
        truth = adj_all[adj_all.index > cutoff]

        try:
            raw = sina_close(sym)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:5s} 新浪抓取失败：{e}")
            continue
        if raw.empty or len(adj_hist) < 2 or truth.empty:
            print(f"{sym:5s} 样本不足（adj_hist={len(adj_hist)} truth={len(truth)}）")
            continue

        # 换月日：唯一权威来源 = pandadata dominant_id 变化（待续接区内）
        dom = pdf["dominant_id"]
        roll_dates = [d for d in truth.index
                      if d in dom.index and dom.get(d) != dom.shift(1).get(d)]

        try:
            res = graft_adjusted(adj_hist, raw, rollover_dates=roll_dates)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:5s} graft 失败：{type(e).__name__}: {e}")
            continue

        pred = res.series.reindex(truth.index)
        err_bp = ((pred - truth) / truth * 1e4).abs()
        al = res.alignment or {}
        cv = al.get("ratio_cv")
        worst.append((sym, float(err_bp.max())))
        print(f"{sym:5s} {str(res.anchor_date)[:10]:>10s} {res.anchor_ratio:>9.4f} "
              f"{al.get('n', 0):>5d} "
              f"{('%.2e' % cv) if isinstance(cv, float) else '—':>10s} "
              f"{len(roll_dates):>4d} {len(truth):>5d} "
              f"{err_bp.max():>11.2f} {err_bp.median():>9.2f} "
              f"{len(res.warnings):>4d}")
        for w in res.warnings:
            print(f"       ⚠ {w}")

        if sym in detail:
            dom = pdf["dominant_id"]
            print(f"       --- {sym} 逐日明细（换月日 "
                  f"{[str(d)[:10] for d in roll_dates] or '无'}）---")
            print(f"       {'日期':>10s} {'主力合约':>12s} {'真值':>15s} "
                  f"{'续接':>15s} {'误差bp':>10s} {'精确价差后bp':>13s}")
            # 消融：从真值反解精确价差，验证「误差是否 100% 来自价差近似」
            exact = {}
            if roll_dates:
                for d in roll_dates:
                    prev = raw.index[raw.index < d][-1]
                    exact[d] = (float(raw.loc[d]) / float(raw.loc[prev])) / (
                        float(truth.loc[d]) / float(adj_all.loc[prev]))
            res2 = graft_adjusted(adj_hist, raw, rollover_dates=roll_dates,
                                  rollover_spreads=exact or None)
            for d in truth.index:
                p, t = float(pred.loc[d]), float(truth.loc[d])
                p2 = float(res2.series.loc[d])
                print(f"       {str(d)[:10]:>10s} {dom.get(d):>12s} {t:>15.4f} "
                      f"{p:>15.4f} {(p - t) / t * 1e4:>10.2f} "
                      f"{(p2 - t) / t * 1e4:>13.2f}")
            if exact:
                print(f"       消融：精确价差 = "
                      f"{ {str(k)[:10]: round(v, 6) for k, v in exact.items()} }")
                print("       （精确价差由真值反解，仅用于证明『误差全部来自价差近似』，"
                      "生产须从逐合约价格独立取得）")

    print()
    if worst:
        worst.sort(key=lambda x: -x[1])
        print(f"最差品种：{worst[0][0]} {worst[0][1]:.2f} bp；"
              f"全部 {len(worst)} 品种最大误差中位数 "
              f"{sorted(v for _, v in worst)[len(worst)//2]:.2f} bp")
        print("判定：误差应集中在**换月当日**（近似价差），非换月日应 ≈0 bp。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
