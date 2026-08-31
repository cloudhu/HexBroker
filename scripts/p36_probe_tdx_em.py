#!/usr/bin/env python
# coding: utf-8
"""P36 免费数据源实测探针（主理人独立取证用，与 PM 的探针互补）。
背景
----
生产刷新管线当前**单点依赖 pandadata MCP**，而 MCP 依赖 AI 会话注入工具索引
（2026-08-28 夜盘因连接器未接入导致 0/18）。要打造"实时稳定可靠的数据流"，
备源的**最高权重指标**是：

    ✅ 能否由纯脚本调用（不依赖 AI 会话 / 不依赖 MCP 注入 / 无 token 配额）

本脚本对候选源做**连接级实测**，输出每个源的：可达性 / 延迟 / 最新数据日期 /
覆盖品种数 / 失败原因。全部结果落盘 JSON，供架构师做故障切换设计取证。

覆盖源
------
1. ``pytdx``  —— 通达信协议，纯 TCP，无 token 无配额（重点候选）
2. ``eastmoney`` —— 东方财富 push2his 多主机轮询（HTTP）
3. ``sina``   —— 新浪期货日线（含主连 xx0 与具体月份合约两种口径对比）

用法
----
    python scripts/p36_probe_tdx_em.py                 # 全量探测
    python scripts/p36_probe_tdx_em.py --only tdx,em   # 只探测指定源
    python scripts/p36_probe_tdx_em.py --symbols ag0,rb0

输出
----
    artifacts/p36_probe_leads.json

退出码
------
- ``0`` 至少一个源返回"可用且新鲜"（last_date 距今 <= --fresh-days，默认 7 天）
- ``1`` 全部源不可用或全部不新鲜（**这正是"必须建备源"的证据**）
- ``3`` 脚本自身异常
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUT_PATH = PROJECT_ROOT / "artifacts" / "p36_probe_leads.json"

# 18 个生产品种（小写 + 0 = 主连口径，与生产缓存 symbol 对齐）
DEFAULT_SYMBOLS = [
    "ag0", "al0", "au0", "cf0", "cu0", "hc0", "i0", "j0", "jm0",
    "m0", "ni0", "p0", "rb0", "sc0", "sr0", "ta0", "y0", "zn0",
]

# 品种 → 交易所（用于 pytdx market 码与东方财富 secid 前缀）
EXCHANGE = {
    "ag": "SHFE", "al": "SHFE", "au": "SHFE", "cu": "SHFE", "hc": "SHFE",
    "ni": "SHFE", "rb": "SHFE", "sc": "INE", "zn": "SHFE",
    "cf": "CZCE", "sr": "CZCE", "ta": "CZCE",
    "i": "DCE", "j": "DCE", "jm": "DCE", "m": "DCE", "p": "DCE", "y": "DCE",
}

# pytdx 期货市场码（通达信扩展市场）
TDX_MARKET = {"SHFE": 30, "DCE": 29, "CZCE": 28, "INE": 30, "CFFEX": 47}

# 公开 TDX 行情服务器（社区长期维护的免费列表，逐个试连）
TDX_SERVERS = [
    ("119.147.212.81", 7709),
    ("124.71.187.122", 7709),
    ("47.107.64.152", 7709),
    ("120.53.120.235", 7709),
    ("154.39.73.66", 7709),
    ("59.175.238.38", 7709),
    ("218.6.170.47", 7709),
    ("123.125.108.24", 7709),
]

# 东方财富 push2his 主机池（单主机常不可达，需轮询）
EM_HOSTS = [
    "push2his.eastmoney.com",
    "46.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
    "50.push2his.eastmoney.com",
    "push2his.eastmoney.com",
]

# 东方财富 secid 前缀（113=上期所, 114=大商所, 115=郑商所, 142=上期能源）
EM_PREFIX = {"SHFE": "113", "DCE": "114", "CZCE": "115", "INE": "142", "CFFEX": "8"}

# 新浪期货日线（社区常用入口）
SINA_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
    "/var%20_{sym}=/InnerFuturesNewService.getDailyKLine?symbol={sym_up}"
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- pytdx ----
def probe_tdx(symbols: List[str], count: int = 10) -> Dict[str, Any]:
    """通达信协议实测：先连服务器，再取各品种日线。

    pytdx 的期货行情需要「先 get_instrument_info 枚举合约」再按 code 取 K 线，
    code 是**数字索引**而非品种字母，因此这里枚举后按品种名前缀匹配。
    """
    out: Dict[str, Any] = {"source": "pytdx", "servers": [], "symbols": {}}
    try:
        from pytdx.hq import TdxHq_API  # type: ignore
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["err"] = f"pytdx 未安装或导入失败：{exc}"
        return out

    api = TdxHq_API(heartbeat=True)
    connected_at: Optional[tuple] = None
    for host, port in TDX_SERVERS:
        t0 = time.time()
        try:
            if api.connect(host, port, time_out=5):
                out["servers"].append(
                    {"host": host, "port": port, "ok": True, "latency_s": round(time.time() - t0, 2)}
                )
                connected_at = (host, port)
                break
            out["servers"].append(
                {"host": host, "port": port, "ok": False, "err": "connect() 返回 False"}
            )
        except Exception as exc:  # noqa: BLE001
            out["servers"].append(
                {"host": host, "port": port, "ok": False, "err": f"{type(exc).__name__}: {exc}"[:160]}
            )

    if connected_at is None:
        out["ok"] = False
        out["err"] = "全部 TDX 服务器均不可连"
        return out

    out["connected"] = connected_at
    out["ok"] = True

    # 枚举三所合约，建立 品种字母 → (market, code) 映射
    index: Dict[str, List[tuple]] = {}
    try:
        for market in (28, 29, 30, 47):
            start = 0
            while start < 4000:
                batch = api.get_instrument_info(start, 100, market=market)
                if not batch:
                    break
                for it in batch:
                    name = str(it.get("name", "")).strip().lower()
                    if not name:
                        continue
                    # name 形如 "ag2610" / "AG2610"
                    for i, ch in enumerate(name):
                        if ch.isdigit():
                            key = name[:i]
                            if len(key) in (1, 2):
                                index.setdefault(key, []).append((market, it["code"], name))
                            break
                start += 100
    except Exception as exc:  # noqa: BLE001
        out["instrument_enum_err"] = f"{type(exc).__name__}: {exc}"[:200]

    out["instruments_indexed"] = sum(len(v) for v in index.values())
    out["index_keys"] = sorted(index.keys())[:60]

    for sym in symbols:
        key = sym[:-1]  # 去掉末尾的 "0"
        cands = index.get(key, [])
        rec: Dict[str, Any] = {"ok": False, "candidates": len(cands)}
        if not cands:
            rec["err"] = f"合约枚举中未找到品种 {key}"
            out["symbols"][sym] = rec
            continue
        # 取成交量最大的合约作为主连代理（与"主力"口径最接近）
        picked = None
        for market, code, name in cands[-12:]:
            try:
                bars = api.get_security_bars(9, market, code, 0, count)
            except Exception:  # noqa: BLE001
                continue
            if bars:
                vol = sum(float(b.get("vol", 0)) for b in bars[-5:])
                if picked is None or vol > picked[0]:
                    picked = (vol, market, code, name, bars)
        if picked is None:
            rec["err"] = "枚举到合约但取不到 K 线"
            out["symbols"][sym] = rec
            continue
        _, market, code, name, bars = picked
        last = bars[-1]
        rec.update(
            ok=True,
            contract=name,
            market=market,
            bars=len(bars),
            last_date=str(last.get("datetime", ""))[:10],
            last_close=str(last.get("close")),
            last_vol=str(last.get("vol")),
        )
        out["symbols"][sym] = rec

    try:
        api.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return out


# ----------------------------------------------------------- eastmoney ----
def probe_eastmoney(symbols: List[str], timeout: int = 12) -> Dict[str, Any]:
    """东方财富 push2his 多主机轮询实测。

    注意：需要给具体月份合约（ag2610），主连代码（ag0）在该接口上通常无数据。
    这里用「当前年月滚动」的方式猜 4 个候选合约，取最新有数据的那个。
    """
    out: Dict[str, Any] = {"source": "eastmoney", "hosts": [], "symbols": {}}
    try:
        import urllib.request

        def fetch(url: str) -> Dict[str, Any]:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                    ),
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "*/*",
                },
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return {"ok": True, "latency_s": round(time.time() - t0, 2), "body": raw}

        probe_url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid=113.ag2610&fields1=f1,f2&fields2=f51,f52,f53&klt=101&fqt=1&lmt=2"
        )
        for host in EM_HOSTS:
            url = probe_url.replace("push2his.eastmoney.com", host, 1)
            try:
                res = fetch(url)
                ok = bool(res["ok"]) and '"data"' in res["body"]
                out["hosts"].append(
                    {"host": host, "ok": ok, "latency_s": res["latency_s"],
                     "preview": res["body"][:160]}
                )
                if ok:
                    break
            except Exception as exc:  # noqa: BLE001
                out["hosts"].append(
                    {"host": host, "ok": False, "err": f"{type(exc).__name__}: {exc}"[:160]}
                )

        if not any(h.get("ok") for h in out["hosts"]):
            out["ok"] = False
            out["err"] = "所有东方财富主机均不可达（含非沙箱环境）"
            return out

        out["ok"] = True
        good = next(h["host"] for h in out["hosts"] if h.get("ok"))

        for sym in symbols:
            key = sym[:-1]
            exch = EXCHANGE.get(key)
            if exch is None:
                out["symbols"][sym] = {"ok": False, "err": f"未知交易所：{key}"}
                continue
            prefix = EM_PREFIX[exch]
            rec: Dict[str, Any] = {"ok": False, "tried": []}
            # 候选合约：未来 6 个连续月份 + 当前年/明年
            now = datetime.now()
            cands = []
            for k in range(0, 13):
                y = now.year + (now.month - 1 + k) // 12
                m = (now.month - 1 + k) % 12 + 1
                cands.append(f"{key}{str(y)[2:]}{m:02d}")
            for contract in cands:
                secid = f"{prefix}.{contract}"
                url = (
                    f"https://{good}/api/qt/stock/kline/get"
                    f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
                    f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                    f"&klt=101&fqt=1&lmt={10}&end=20500101"
                )
                try:
                    res = fetch(url)
                    body = json.loads(res["body"])
                    data = (body or {}).get("data") or {}
                    klines = data.get("klines") or []
                except Exception as exc:  # noqa: BLE001
                    rec["tried"].append({"contract": contract, "err": f"{type(exc).__name__}"[:60]})
                    continue
                if not klines:
                    rec["tried"].append({"contract": contract, "bars": 0})
                    continue
                last = klines[-1].split(",")
                rec.update(
                    ok=True,
                    contract=contract,
                    secid=secid,
                    bars=len(klines),
                    last_date=last[0],
                    last_close=last[2],
                    last_vol=last[5] if len(last) > 5 else None,
                )
                break
            out["symbols"][sym] = rec

    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["err"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


# ---------------------------------------------------------------- sina ----
def probe_sina(symbols: List[str], timeout: int = 12) -> Dict[str, Any]:
    """新浪期货日线：同时测「主连 xx0」与「具体月份合约」两种口径。

    PM 的探测显示主连口径末日为 2024-07-17（停更）。本函数验证：
    换成具体月份合约后是否恢复更新 —— 决定新浪到底是"死源"还是"口径错"。
    """
    out: Dict[str, Any] = {"source": "sina", "symbols": {}}
    try:
        import urllib.request

        def fetch(url: str) -> str:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")

        now = datetime.now()
        for sym in symbols:
            key = sym[:-1]
            rec: Dict[str, Any] = {"ok": False, "tried": []}
            # 口径 A：主连 xx0（PM 用的口径）
            variants = [sym]
            # 口径 B/c：具体月份合约
            for k in range(0, 13):
                y = now.year + (now.month - 1 + k) // 12
                m = (now.month - 1 + k) % 12 + 1
                variants.append(f"{key}{str(y)[2:]}{m:02d}")
            for v in variants:
                url = SINA_URL.format(sym=v, sym_up=v.upper())
                try:
                    t0 = time.time()
                    raw = fetch(url)
                    lat = round(time.time() - t0, 2)
                except Exception as exc:  # noqa: BLE001
                    rec["tried"].append({"contract": v, "err": f"{type(exc).__name__}"[:60]})
                    continue
                if "=[" not in raw or "]" not in raw:
                    rec["tried"].append({"contract": v, "empty": True, "len": len(raw)})
                    continue
                try:
                    payload = raw[raw.index("=[") + 2: raw.rindex("]")]
                    rows = json.loads(payload)
                except Exception:  # noqa: BLE001
                    rec["tried"].append({"contract": v, "parse_err": True, "len": len(raw)})
                    continue
                if not rows:
                    rec["tried"].append({"contract": v, "rows": 0})
                    continue
                last = rows[-1]
                rec.update(
                    ok=True,
                    contract=v,
                    bars=len(rows),
                    last_date=str(last[0])[:10] if last else None,
                    last_close=str(last[1]) if len(last) > 1 else None,
                    latency_s=lat,
                )
                break
            out["symbols"][sym] = rec
        out["ok"] = any(v.get("ok") for v in out["symbols"].values())
        if not out["ok"]:
            out["err"] = "新浪全部口径均无数据"
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["err"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


# ---------------------------------------------------------------- main ----
def _fresh_days(last_date: Optional[str], today: date) -> Optional[int]:
    if not last_date:
        return None
    try:
        d = datetime.strptime(str(last_date)[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        try:
            d = datetime.strptime(str(last_date)[:10], "%Y/%m/%d").date()
        except Exception:  # noqa: BLE001
            return None
    return (today - d).days


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="P36 免费期货数据源实测探针")
    ap.add_argument("--only", default="tdx,em,sina", help="只探测指定源，逗号分隔")
    ap.add_argument("--symbols", default="", help="覆盖品种列表，逗号分隔（默认 18 品种）")
    ap.add_argument("--fresh-days", type=int, default=7, help="新鲜度判定天数（默认 7）")
    ap.add_argument("--out", default=str(OUT_PATH), help="输出 JSON 路径")
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()}
    today = date.today()

    results: Dict[str, Any] = {
        "probe_at": _now(),
        "asof": today.isoformat(),
        "symbols": symbols,
        "sources": {},
    }

    if "tdx" in only:
        print("[探针] pytdx ...", flush=True)
        results["sources"]["pytdx"] = probe_tdx(symbols)
    if "em" in only:
        print("[探针] eastmoney ...", flush=True)
        results["sources"]["eastmoney"] = probe_eastmoney(symbols)
    if "sina" in only:
        print("[探针] sina ...", flush=True)
        results["sources"]["sina"] = probe_sina(symbols)

    # 新鲜度汇总
    summary = []
    for name, src in results["sources"].items():
        ok_syms = [k for k, v in (src.get("symbols") or {}).items() if v.get("ok")]
        dates = [
            (src.get("symbols") or {}).get(k, {}).get("last_date")
            for k in ok_syms
        ]
        ages = [a for a in (_fresh_days(d, today) for d in dates) if a is not None]
        summary.append(
            {
                "source": name,
                "reachable": bool(src.get("ok")),
                "ok_symbols": len(ok_syms),
                "total_symbols": len(symbols),
                "max_last_date": max((d for d in dates if d), default=None),
                "min_age_days": min(ages) if ages else None,
                "fresh_within": args.fresh_days if ages and min(ages) <= args.fresh_days else False,
                "err": src.get("err"),
            }
        )
    results["summary"] = summary

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[探针] 结果落盘：{out_path}")
    print(f"{'源':<12}{'可达':<6}{'可用/总':<10}{'最新日期':<14}{'最小龄期':<10}新鲜")
    for s in summary:
        print(
            f"{s['source']:<12}{str(s['reachable']):<6}"
            f"{s['ok_symbols']}/{s['total_symbols']:<8}"
            f"{str(s['max_last_date']):<14}{str(s['min_age_days']):<10}{s['fresh_within']}"
        )

    return 0 if any(s["fresh_within"] for s in summary) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[探针] 🔴 脚本异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(3)
