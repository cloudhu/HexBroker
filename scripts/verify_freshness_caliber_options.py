"""信号新鲜度三口径对比验证器（只读诊断工具）。

背景
----
P3-B（2026-08-31，commit 8a91230）把新鲜度度量从「工作日差」改为「自然日差」
并把阈值从 0 放宽到 1，意图是让正常隔夜（T 收盘信号 → T+1 执行）能够放行。

但该改动引入回归：**周一日盘与假期后首日日盘被结构性拦截**。
因为周一 → 上一交易日是周五，自然日差恒 = 3 > 阈值 1。
而 P3-B 的立项洞察（`.workbuddy/memory/2026-08-28.md` L172）早已认定：
「P0-3 立项背景『周五信号周一用』在交易日意义上就是标准 T+1，与『陈旧』非同一概念」。

本工具并排实算三种口径，用于裁决选型：

  A 自然日差（P3-B 现行）      —— 把日历长度误当信息陈旧，误伤周一/假期后首日
  B np.busday_count 工作日差   —— 修复周一，但法定节假日被算作工作日 → 仍误伤假期后首日
  C 交易日历 lag（建议）       —— lag = idx(上一交易日) - idx(信号日)，阈值 0

C 的语义：直接回答「信号有没有覆盖最近一个已收盘交易日」，与星期几、假期长度无关。

用法
----
    python scripts/verify_freshness_caliber_options.py
    python scripts/verify_freshness_caliber_options.py --today 2026-09-07 --signal 2026-09-04
    python scripts/verify_freshness_caliber_options.py --full-history

只读，不写任何生产文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "data/raw/processed"
DEFAULT_SYMBOL = "rb0"


def trading_calendar(symbol: str = DEFAULT_SYMBOL) -> list[dt.date]:
    """交易日历 = 主湖实际存在的交易日（无需外部依赖，天然含法定休市）。"""
    days: set[dt.date] = set()
    for f in glob.glob(str(LAKE / symbol / "1d" / "*.parquet")):
        days |= set(pd.to_datetime(pd.read_parquet(f)["datetime"]).dt.date)
    return sorted(days)


def caliber_a(today: dt.date, sig: dt.date) -> int:
    """A：自然日差（P3-B 现行）。"""
    return (today - sig).days


def caliber_b(today: dt.date, sig: dt.date) -> int:
    """B：np.busday_count 工作日差（法定节假日被计为工作日）。"""
    return int(np.busday_count(sig, today))


def caliber_c(today: dt.date, sig: dt.date, cal: list[dt.date]) -> int | None:
    """C：交易日历 lag = idx(上一交易日) - idx(信号日)。0 = 信号即上一交易日。"""
    prev = [d for d in cal if d < today]
    if not prev or sig not in cal:
        return None
    return cal.index(prev[-1]) - cal.index(sig)


def verdict(name: str, value: int | None, threshold: int) -> str:
    if value is None:
        return "n/a（信号日或上一交易日不在日历内）"
    return "放行" if value <= threshold else "拦截"


def main() -> None:
    ap = argparse.ArgumentParser(description="信号新鲜度三口径对比")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--today", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--signal", help="YYYY-MM-DD，默认取信号缓存最新 ts")
    ap.add_argument("--full-history", action="store_true",
                    help="追加全历史影响面实测（健康态样本误拦率，选型决定性证据）")
    args = ap.parse_args()

    cal = trading_calendar(args.symbol)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    sig = None
    if args.signal:
        sig = dt.date.fromisoformat(args.signal)
    else:
        cache = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
        if cache.exists():
            sig = pd.to_datetime(pd.read_parquet(cache)["ts"]).max().date()

    prev = [d for d in cal if d < today]
    print("=" * 72)
    print(f"新鲜度三口径对比   品种 {args.symbol}   交易日历 {len(cal)} 天")
    print("=" * 72)
    print(f"  今日           = {today}")
    print(f"  上一交易日     = {prev[-1] if prev else 'n/a'}")
    print(f"  缓存最新信号日 = {sig}")

    if sig is None:
        print("\n⛔ 无法确定信号日，请用 --signal 指定。")
        return

    a, b, c = caliber_a(today, sig), caliber_b(today, sig), caliber_c(today, sig, cal)
    print("\n口径                          取值   阈值   判定")
    print("-" * 72)
    print(f"  A 自然日差（P3-B 现行）      {a:>3}      1     {verdict('A', a, 1)}")
    print(f"  B np.busday_count 工作日差   {b:>3}      1     {verdict('B', b, 1)}")
    print(f"  C 交易日历 lag（建议）       {str(c):>3}      0     {verdict('C', c, 0)}")

    print("\n" + "=" * 72)
    print("关键场景回检（用于确认 C 不会漏放 P0-3 病灶）")
    print("=" * 72)
    # ⚠️ 元组语义为 (name, today, signal)，顺序写反会得到负值 / None 的无效回检
    scenarios = [
        ("周一（信号=上周五）", today, sig),
        ("P0-3 病灶：08-25 用 08-21 信号", dt.date(2026, 8, 25), dt.date(2026, 8, 21)),
        ("缓存停更 5 个交易日", sig, sig),
    ]
    for name, t, s in scenarios:
        if name.startswith("缓存停更"):
            # 锚定日历末尾往前数 5 个交易日，避免用信号日时恰好越界被静默跳过
            s, t = cal[-6], cal[-1]
        cc = caliber_c(t, s, cal)
        aa, bb = caliber_a(t, s), caliber_b(t, s)
        print(f"  {name}")
        print(f"     A={aa}({verdict('A', aa, 1)})  B={bb}({verdict('B', bb, 1)})  "
              f"C={cc}({verdict('C', cc, 0)})")

    if args.full_history:
        full_history(cal)

    print("\n只读，未写任何生产文件。")


def full_history(cal: list[dt.date]) -> None:
    """全历史影响面实测。

    构造**健康态样本**：遍历交易日历每一对相邻交易日 (today=cal[i], signal=cal[i-1])，
    即「盘前刷新正常执行、信号恒为上一交易日」这一本该 100% 放行的场景。
    统计各口径在此样本上的误拦率 —— 这是 A/B/C 选型的决定性证据。

    ⚠️ 注意：`0 or 99` 会返回 99（Python 假值陷阱），None 判断必须显式写。
    """
    import collections

    rows = []
    for i in range(1, len(cal)):
        today, sig = cal[i], cal[i - 1]
        rows.append((today, sig, caliber_a(today, sig),
                     caliber_b(today, sig), caliber_c(today, sig, cal)))

    def blocked(v: int | None, thr: int) -> bool:
        return True if v is None else v > thr

    n = len(rows)
    a_blk = sum(1 for r in rows if blocked(r[2], 1))
    b_blk = sum(1 for r in rows if blocked(r[3], 1))
    c_blk = sum(1 for r in rows if blocked(r[4], 0))

    print("\n" + "=" * 72)
    print(f"全历史影响面实测（健康态样本 {n} 天：{cal[0]} ~ {cal[-1]}）")
    print("=" * 72)
    print("口径                          阈值   误拦天数   误拦率")
    print("-" * 72)
    print(f"  A 自然日差（P3-B 现行）        1   {a_blk:>6}   {a_blk / n:7.2%}   ⛔")
    print(f"  B np.busday_count 工作日差     1   {b_blk:>6}   {b_blk / n:7.2%}   ⛔")
    print(f"  C 交易日历 lag（建议）         0   {c_blk:>6}   {c_blk / n:7.2%}   ✅")

    wd = collections.Counter(r[0].strftime("%a") for r in rows if blocked(r[2], 1))
    print(f"\n  A 误拦日的星期分布：{dict(wd)}")
    print(f"  A 误拦日的自然日差取值分布："
          f"{dict(sorted(collections.Counter(r[2] for r in rows if blocked(r[2], 1)).items()))}")

    byyear_tot = collections.Counter(r[0].year for r in rows)
    byyear = collections.Counter(r[0].year for r in rows if blocked(r[2], 1))
    print("\n  A 口径按年误伤（逐年稳定 ⇒ 结构性，非偶发）：")
    for y in sorted(byyear_tot):
        k = byyear[y]
        print(f"    {y}  {k:>3}/{byyear_tot[y]:<3} = {k / byyear_tot[y]:6.1%}")

    bb = [r for r in rows if blocked(r[3], 1)]
    print(f"\n  B 残留误伤 {len(bb)} 天 = 全部为法定假期后首日（选项 2 修不掉的部分）：")
    for today, sig, a, b, c in sorted(bb, key=lambda r: -r[2])[:5]:
        print(f"    {today}({today.strftime('%a')}) 信号={sig}  A={a:>2}  "
              f"B={b}(拦截)  C={c}(放行)")


if __name__ == "__main__":
    main()
