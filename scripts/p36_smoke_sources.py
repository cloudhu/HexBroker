#!/usr/bin/env python
# coding: utf-8
"""P36 数据源真实联网冒烟（P0-8）。

与单测的区别：单测全部 mock、零联网，只能证明"代码逻辑自洽"，**无法证明源还活着**。
本脚本真实调用各源，回答一个单测回答不了的问题：

    「现在这一刻，这个数据源还能取到新鲜数据吗？」

刻意不进 pytest：CI 不应依赖外网，本脚本由人工/运维按需执行，或接入定时巡检。

用法
----
    python scripts/p36_smoke_sources.py                    # 冒烟全部品种
    python scripts/p36_smoke_sources.py --symbols rb0,cu0  # 指定品种
    python scripts/p36_smoke_sources.py --sources sina,akshare
    python scripts/p36_smoke_sources.py --lookback-days 10 --fresh-days 5

退出码
------
    0  至少一个源「可用且新鲜」
    1  没有任何源「可用且新鲜」（需要启用备源 / 人工介入）
    3  脚本自身异常
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)

#: 本系统 18 个品种（主力连续）
ALL_SYMBOLS = [
    "ag0", "al0", "au0", "cf0", "cu0", "hc0", "i0", "j0", "jm0",
    "m0", "ni0", "p0", "rb0", "sc0", "sr0", "ta0", "y0", "zn0",
]


def _build(sources: list[str]):
    """按需构造数据源实例（延迟 import，缺依赖不致命）。"""
    built = {}
    if "sina" in sources:
        try:
            from hexbroker.data.sources.sina_source import SinaSource

            built["sina"] = SinaSource(rate_limit_sleep=0.15, save=False)
        except Exception as exc:  # noqa: BLE001
            built["sina"] = exc
    if "akshare" in sources:
        try:
            from hexbroker.data.sources.akshare_source import AkshareSource

            built["akshare"] = AkshareSource()
        except Exception as exc:  # noqa: BLE001
            built["akshare"] = exc
    return built


def probe(src, symbols: list[str], start: str, end: str, fresh_days: int) -> dict:
    """对单个源做一次真实取数探测。"""
    t0 = time.time()
    rec: dict = {
        "source": getattr(src, "name", "unknown"),
        "ok": False,
        "rows": 0,
        "symbols_ok": 0,
        "last_date": None,
        "latency_s": None,
        "fresh": False,
        "error_type": None,
        "error": None,
        "expected_end": end,
        "fresh_days": fresh_days,
    }
    try:
        bf = src.fetch_bars(symbols, start, end, freq="1d", save=False)
        rec["ok"] = True
        rec["rows"] = len(bf.df)
        rec["symbols_ok"] = len(bf.symbols)
        if rec["rows"]:
            last = bf.df.index.get_level_values("datetime").max()
            rec["last_date"] = str(pd_Timestamp(last).date())
            rec["fresh"] = (
                date.fromisoformat(rec["last_date"]) >= date.fromisoformat(end) - timedelta(days=fresh_days)
            )
    except Exception as exc:  # noqa: BLE001
        rec["error_type"] = type(exc).__name__
        rec["error"] = str(exc)[:300]
    finally:
        rec["latency_s"] = round(time.time() - t0, 3)
    return rec


def pd_Timestamp(x):  # noqa: N802 - 局部小工具，避免顶层 import pandas 失败掩盖真实错误
    import pandas as pd

    return pd.Timestamp(x)


def main() -> int:
    ap = argparse.ArgumentParser(description="P36 数据源真实联网冒烟")
    ap.add_argument("--symbols", default=",".join(ALL_SYMBOLS), help="逗号分隔的品种列表")
    ap.add_argument("--sources", default="sina,akshare", help="逗号分隔的源列表")
    ap.add_argument("--lookback-days", type=int, default=10, help="请求窗口回看天数")
    ap.add_argument("--fresh-days", type=int, default=5, help="新鲜判定容忍天数")
    ap.add_argument("--end", default=None, help="请求结束日，默认今天")
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    end = args.end or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=args.lookback_days)).isoformat()

    print(f"[smoke] symbols={len(symbols)} sources={sources} window={start}..{end}")

    results = []
    built = _build(sources)
    for name in sources:
        src = built.get(name)
        if isinstance(src, Exception):
            results.append(
                {
                    "source": name,
                    "ok": False,
                    "rows": 0,
                    "symbols_ok": 0,
                    "last_date": None,
                    "latency_s": None,
                    "fresh": False,
                    "error_type": type(src).__name__,
                    "error": f"构造失败: {src}",
                    "expected_end": end,
                    "fresh_days": args.fresh_days,
                }
            )
            print(f"  [{name}] SKIP 构造失败: {src}")
            continue
        rec = probe(src, symbols, start, end, args.fresh_days)
        results.append(rec)
        flag = "OK  " if rec["ok"] and rec["fresh"] else ("STALE" if rec["ok"] else "FAIL")
        print(
            f"  [{name}] {flag} rows={rec['rows']} syms={rec['symbols_ok']} "
            f"last={rec['last_date']} {rec['latency_s']}s"
            + (f" ERR={rec['error_type']}: {rec['error']}" if rec["error"] else "")
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": symbols,
        "window": {"start": start, "end": end},
        "fresh_days": args.fresh_days,
        "results": results,
        "any_fresh": any(r["ok"] and r["fresh"] for r in results),
    }
    out = Path(args.out) if args.out else ARTIFACTS / "p36_smoke_sources.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[smoke] 结果已写入 {out}")

    return 0 if payload["any_fresh"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(3)
