#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""36 号交付物配套探针：18 品种 × 候选免费源的覆盖/口径/最新日期实测。

只读、无副作用（不写任何 parquet）。输出纯文本表格，供人工核对。
用法：
  python scripts/dev_probe_36_sources.py --source sina
  python scripts/dev_probe_36_sources.py --source eastmoney
  python scripts/dev_probe_36_sources.py --source akshare
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Callable

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 15

# 18 品种：sym0 -> (品种字母, 交易所)
SYM0 = {
    "ag0": ("ag", "SHFE"), "al0": ("al", "SHFE"), "au0": ("au", "SHFE"),
    "cf0": ("cf", "CZCE"), "cu0": ("cu", "SHFE"), "hc0": ("hc", "SHFE"),
    "i0": ("i", "DCE"), "j0": ("j", "DCE"), "jm0": ("jm", "DCE"),
    "m0": ("m", "DCE"), "ni0": ("ni", "SHFE"), "p0": ("p", "DCE"),
    "rb0": ("rb", "SHFE"), "sc0": ("sc", "INE"), "sr0": ("sr", "CZCE"),
    "ta0": ("ta", "CZCE"), "y0": ("y", "DCE"), "zn0": ("zn", "SHFE"),
}

# 东方财富期货 secid 市场段（经验值，需实测确认）
EM_MARKET = {"SHFE": 113, "DCE": 114, "CZCE": 115, "INE": 142, "CFFEX": 8}


def probe_sina(sym0: str, prod: str, exch: str) -> dict:
    """新浪内盘期货主力连续日线。"""
    url = ("http://stock2.finance.sina.com.cn/futures/api/json.php/"
           f"IndexService.getInnerFuturesDailyKLine?symbol={sym0}")
    t = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=UA)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:80]}"}
    if not data:
        return {"ok": False, "err": "empty"}
    rows = [x for x in data if isinstance(x, (list, tuple)) and len(x) >= 6]
    if not rows:
        return {"ok": False, "err": f"format={type(data[0]).__name__}"}
    last = rows[-1]
    return {
        "ok": True, "bars": len(rows),
        "first_date": str(rows[0][0])[:10], "last_date": str(last[0])[:10],
        "last_close": last[4], "last_vol": last[5],
        "cols": len(rows[0]),  # 6 = date,o,h,l,c,v（无 amount/oi）
        "latency_s": round(time.time() - t, 2),
    }


def probe_eastmoney(sym0: str, prod: str, exch: str) -> dict:
    """东方财富期货日线（尝试主力连续 secid，失败回退到具体合约）。"""
    mk = EM_MARKET.get(exch)
    if mk is None:
        return {"ok": False, "err": f"no market for {exch}"}
    # 东财期货「主力连续」secid 形如 113.rb（品种级）；先试品种级，再试 0 结尾
    trials = [f"{mk}.{prod}0", f"{mk}.{prod}"]
    last_err = ""
    for secid in trials:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={secid}&klt=101&fqt=1&lmt=100000&end=20500101"
               "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
        t = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=UA)
            r.raise_for_status()
            js = r.json()
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:60]}"
            continue
        d = (js or {}).get("data")
        if not d or not d.get("klines"):
            last_err = "no klines"
            continue
        kl = d["klines"]
        last = kl[-1].split(",")
        first = kl[0].split(",")
        return {
            "ok": True, "secid": secid, "name": d.get("name"), "bars": len(kl),
            "first_date": first[0], "last_date": last[0],
            "last_close": last[2], "last_vol": last[5] if len(last) > 5 else "?",
            "cols": len(last),  # f51..f58 -> 8 段
            "latency_s": round(time.time() - t, 2),
        }
    return {"ok": False, "err": last_err}


def probe_akshare(sym0: str, prod: str, exch: str) -> dict:
    """AkShare 期货接口（futures_main_sina / futures_zh_daily_sina）。"""
    t = time.time()
    try:
        import akshare as ak
    except Exception as exc:
        return {"ok": False, "err": f"import {type(exc).__name__}"}
    for fn_name in ("futures_main_sina", "futures_zh_daily_sina"):
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        try:
            if fn_name == "futures_main_sina":
                df = fn(symbol=sym0)
            else:
                df = fn(symbol=sym0)
        except Exception as exc:
            continue
        if df is None or df.empty:
            continue
        cols = list(df.columns)
        dcol = "date" if "date" in cols else cols[0]
        ccol = "close" if "close" in cols else cols[-1]
        return {
            "ok": True, "fn": fn_name, "bars": len(df),
            "first_date": str(df[dcol].iloc[0])[:10],
            "last_date": str(df[dcol].iloc[-1])[:10],
            "last_close": df[ccol].iloc[-1],
            "cols": ",".join(cols),
            "latency_s": round(time.time() - t, 2),
        }
    return {"ok": False, "err": "no usable akshare fn"}


PROBES: dict[str, Callable[[str, str, str], dict]] = {
    "sina": probe_sina,
    "eastmoney": probe_eastmoney,
    "akshare": probe_akshare,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=list(PROBES))
    ap.add_argument("--json", default=None, help="结果写 JSON 路径（可选）")
    args = ap.parse_args()

    fn = PROBES[args.source]
    results = {}
    n_ok = 0
    print(f"=== {args.source} · 18 品种探测 ===")
    print(f"{'sym0':6s} {'ok':4s} {'bars':>7s} {'first':>11s} {'last':>11s} {'close':>12s} {'cols':>5s} note")
    for sym0, (prod, exch) in SYM0.items():
        r = fn(sym0, prod, exch)
        results[sym0] = r
        if r.get("ok"):
            n_ok += 1
            print(f"{sym0:6s} {'OK':4s} {r['bars']:>7d} {str(r['first_date']):>11s} "
                  f"{str(r['last_date']):>11s} {str(r['last_close']):>12s} "
                  f"{str(r.get('cols')):>5s} {r.get('secid') or r.get('fn') or ''}")
        else:
            print(f"{sym0:6s} {'FAIL':4s} {'-':>7s} {'-':>11s} {'-':>11s} {'-':>12s} {'-':>5s} {r.get('err')}")
        time.sleep(0.15)
    print(f"\n覆盖: {n_ok}/18")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
