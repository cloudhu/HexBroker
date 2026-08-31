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
import bisect
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "data/raw/processed"
DEFAULT_SYMBOL = "rb0"

sys.path.insert(0, str(ROOT))

# ⛔⛔ **验证脚本不得自带生产逻辑的副本**（2026-08-31 教训）
# 本文件初版自己写了一份 ``caliber_c`` 的本地实现，结果「C 误拦 0.00%」
# 这个选型决定性证据是用一份**与生产代码漂移的副本**算出来的 —— 该副本
# 用 ``prev = [d for d in cal if d < today]``，既没有「当日日盘是否已收盘」
# 的概念（无法区分日盘/夜盘），也掩盖了夜盘 `bisect_right` 回退的失明缺陷。
# 现在 **C 口径直接调用生产实现**，A/B 是对立候选（无生产对应物），才允许本地实现。
from hexbroker.paper.signals import (  # noqa: E402
    DAY_SESSION_CLOSE,
    _closed_by,
    _trading_lag,
    augment_calendar,
    load_trading_calendar,
)


def blocked(v: int | None, thr: int) -> bool:
    """是否拦截。⛔ `0 or 99` 会返回 99（Python 假值陷阱），None 必须显式判断。"""
    return True if v is None else v > thr


def _old_trading_lag(sig_day, ref, calendar, day_close):
    """**已知错误实现**（自验基线，仅用于证明样本有判别力，勿用于生产）。

    旧版：closed 分支用 ``bisect_right(cal, ref_day) - 1``，ref_day 不在日历时
    静默回退到上一交易日 → 夜盘缓存停更整一个交易日却算出 lag=0 放行。
    """
    if sig_day is None or ref is None or not calendar:
        return None
    ref_day = ref.date() if isinstance(ref, dt.datetime) else ref
    i_ref = (bisect.bisect_right(calendar, ref_day) if _closed_by(ref, day_close)
             else bisect.bisect_left(calendar, ref_day)) - 1
    if i_ref < 0:
        return None
    i_sig = bisect.bisect_left(calendar, sig_day)
    if i_sig >= len(calendar) or calendar[i_sig] != sig_day:
        return None
    return i_ref - i_sig


def trading_calendar(symbol: str = DEFAULT_SYMBOL) -> list[dt.date]:
    """交易日历 = **全品种并集**（与生产同口径）。

    ⛔ 不能用单品种：sc0 少 54 天（2018-03 上市）、ni0 少 1 天。
    per-symbol 日历会让单品种数据缺口把「上一交易日」前移 → 门禁变松（保守方向错误）。
    """
    return list(load_trading_calendar())


def caliber_a(today: dt.date, sig: dt.date) -> int:
    """A：自然日差（P3-B 现行）。"""
    return (today - sig).days


def caliber_b(today: dt.date, sig: dt.date) -> int:
    """B：np.busday_count 工作日差（法定节假日被计为工作日）。"""
    return int(np.busday_count(sig, today))


def caliber_c(
    asof: dt.datetime, sig: dt.date, cal: list[dt.date]
) -> int | None:
    """C：交易日历 lag —— **直接委托生产实现** ``_trading_lag``。

    ⛔ 参数语义：C 必须收 **含时刻的 asof**（不能只收 date）。
    生产的 R 取法按「当日日盘是否已收盘（≥15:00）」分岔：
      - 日盘（未收盘）→ R = 严格早于当日的最后交易日；
      - 夜盘（已收盘）→ R **必须严格命中当日**，不在日历 = 无法判定 → None（保守拦截）。
    """
    return _trading_lag(sig, asof, tuple(cal), DAY_SESSION_CLOSE)


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
    print(f"新鲜度三口径对比   交易日历（全品种并集） {len(cal)} 天")
    print("=" * 72)
    print(f"  今日           = {today}   （A/B 只看日期；C 需时刻，见下）")
    print(f"  上一交易日     = {prev[-1] if prev else 'n/a'}")
    print(f"  缓存最新信号日 = {sig}")

    if sig is None:
        print("\n⛔ 无法确定信号日，请用 --signal 指定。")
        return

    a, b = caliber_a(today, sig), caliber_b(today, sig)
    # ⛔ C 必须带时刻：日盘（未收盘）R=T-1；夜盘（已收盘）R 必须严格命中 T
    c_day = caliber_c(dt.datetime.combine(today, dt.time(10, 30)), sig, cal)
    c_night = caliber_c(dt.datetime.combine(today, dt.time(21, 30)), sig, cal)
    print("\n口径                          取值   阈值   判定")
    print("-" * 72)
    print(f"  A 自然日差（P3-B 现行）      {a:>7}      1     {verdict('A', a, 1)}")
    print(f"  B np.busday_count 工作日差   {b:>7}      1     {verdict('B', b, 1)}")
    print(f"  C 交易日历 lag · 日盘 10:30  {str(c_day):>7}      0     {verdict('C', c_day, 0)}")
    print(f"  C 交易日历 lag · 夜盘 21:30  {str(c_night):>7}      0     {verdict('C', c_night, 0)}")
    if c_night is None:
        print("\n  ⓘ 夜盘 None = 主湖无当日 bar（20:30 补数未执行/失败）→ 保守拦截。")
        print("    这是**预期行为**：夜盘开仓强依赖当日补数，不是缺陷（P3-C 补丁）。")

    print("\n" + "=" * 72)
    print("关键场景回检（用于确认 C 不会漏放 P0-3 病灶）")
    print("=" * 72)
    # ⚠️ 元组语义为 (name, today, signal, hour)，顺序写反会得到负值 / None 的无效回检
    scenarios = [
        ("周一（信号=上周五）· 日盘", today, sig, 10),
        ("P0-3 病灶：08-25 用 08-21 信号 · 夜盘", dt.date(2026, 8, 25), dt.date(2026, 8, 21), 21),
        ("缓存停更 5 个交易日 · 日盘", sig, sig, 10),
    ]
    for name, t, s, hour in scenarios:
        if name.startswith("缓存停更"):
            # 锚定日历末尾往前数 5 个交易日，避免用信号日时恰好越界被静默跳过
            s, t = cal[-6], cal[-1]
        cc = caliber_c(dt.datetime.combine(t, dt.time(hour, 30)), s, cal)
        aa, bb = caliber_a(t, s), caliber_b(t, s)
        print(f"  {name}")
        print(f"     A={aa}({verdict('A', aa, 1)})  B={bb}({verdict('B', bb, 1)})  "
              f"C={cc}({verdict('C', cc, 0)})")

    if args.full_history:
        full_history(cal)

    print("\n只读，未写任何生产文件。")


def full_history(cal: list[dt.date]) -> None:
    """全历史影响面实测（**日盘 / 夜盘两套健康态样本**）。

    ⛔⛔ 为什么必须分会话（P3-C 补丁，2026-08-31）：
    C 口径的 R 取法按「当日日盘是否已收盘」分岔，日盘与夜盘的**健康态定义不同**，
    混在一起统计会把「夜盘严格命中当日」这一正确行为误算成误拦。

      - **日盘健康态**（10:30，当日未收盘）：R = 上一交易日，信号 = 上一交易日
        → lag 应 = 0 → 放行。即「08:00 盘前刷新正常执行」的标准场景。
      - **夜盘健康态**（21:30，当日已收盘）：R = 当日，信号 = 当日
        → lag 应 = 0 → 放行。即「20:30 补数 + 重算信号成功」的标准场景
        （``refresh_pull_local.py`` 20:30 通道**含当日**，见其 docstring）。

    两者都属于「本该 100% 放行」的样本，误拦率必须为 0.00%。

    ⚠️ 注意：`0 or 99` 会返回 99（Python 假值陷阱），None 判断必须显式写。
    """
    import collections

    # (label, asof 时刻, 信号相对 today 的偏移：0=当日, -1=上一交易日)
    sessions = [
        ("日盘 10:30（信号=上一交易日）", dt.time(10, 30), -1),
        ("夜盘 21:30（信号=当日）", dt.time(21, 30), 0),
    ]

    print("\n" + "=" * 72)
    print("⛔ 请先读这段 —— C 列 0.00% 曾经是**同义反复**（QA 揪出，2026-08-31）")
    print("=" * 72)
    print("旧的样本定义把 today 与 sig 都从 cal 本身取：日盘 lag 恒 = (i-1)-(i-1) = 0、")
    print("夜盘恒 = i-i = 0，**与实现无关**。我一度把它当「选型决定性证据」写进交付报告，")
    print("这是错的：一个只会输出 0.00% 的验证，比不验证更有害。")
    print()
    print("新样本把**真值**与**测量工具**分开：")
    print("  · 参考日历（真值）= 完整主湖并集，用于确定「信号日应该是哪天」；")
    print("  · 测量日历（工具）= 截断到 T-1，**模拟主湖滞后**（生产真实状态）。")
    print("并追加两列**自验基线**（已知错误实现）：样本若无判别力，这两列也会是 0.00%。")
    print()
    print("⚠️ 日盘列的 C 仍近乎同义反复 —— 日盘 R 恒取上一交易日，不依赖实现细节。")
    print("   日盘列的用途是让 A/B 的误伤在同一个样本上显形；C 的真正判别力在**夜盘列**。")

    for label, tod, sig_off in sessions:
        n = 0
        a_blk = b_blk = c_blk = 0
        c_noaug = c_old = 0
        for i in range(1, len(cal)):
            today = cal[i]
            j = i + sig_off
            if j < 0:
                continue
            sig = cal[j]
            asof = dt.datetime.combine(today, tod)
            cal_trunc = cal[:i]                      # 主湖滞后：不含当日
            cal_aug = augment_calendar(cal_trunc, today, (sig,))
            n += 1
            if blocked(caliber_a(today, sig), 1):
                a_blk += 1
            if blocked(caliber_b(today, sig), 1):
                b_blk += 1
            if blocked(_trading_lag(sig, asof, cal_aug, DAY_SESSION_CLOSE), 0):
                c_blk += 1
            # --- 自验基线 1：不做日历增补（= 未修「行为反转」的实现）---
            if blocked(_trading_lag(sig, asof, cal_trunc, DAY_SESSION_CLOSE), 0):
                c_noaug += 1
            # --- 自验基线 2：旧版 bisect_right 回退（= 夜盘门禁失明的实现）---
            if blocked(_old_trading_lag(sig, asof, cal_trunc, DAY_SESSION_CLOSE), 0):
                c_old += 1

        print("\n" + "=" * 72)
        print(f"全历史影响面实测 · {label}")
        print(f"健康态样本 {n} 天：{cal[0]} ~ {cal[-1]}   测量日历恒截断到 T-1")
        print("=" * 72)
        print("口径                                阈值   误拦天数   误拦率")
        print("-" * 72)
        for nm, k, thr in (
            ("A 自然日差（P3-B 现行）", a_blk, 1),
            ("B np.busday_count 工作日差", b_blk, 1),
            ("C 交易日历 lag（生产实现）", c_blk, 0),
        ):
            mark = "✅" if k == 0 else "⛔"
            print(f"  {nm:<34}{thr}   {k:>6}   {k / n:7.2%}   {mark}")
        print("  " + "-" * 68)
        print("  自验基线（已知错误实现，用于证明样本有判别力）")
        print(f"  {'C 不做日历增补':<34}{0}   {c_noaug:>6}   {c_noaug / n:7.2%}   "
              f"{'✅ 样本有判别力' if c_noaug else '🔴 样本无判别力（空证据）'}")
        print(f"  {'C 旧版 bisect_right 回退':<34}{0}   {c_old:>6}   {c_old / n:7.2%}   "
              f"{'✅ 样本有判别力' if c_old else '🔴 样本无判别力（空证据）'}")

        if c_blk:
            print("\n  🔴 C 在此健康态样本上出现误拦，逐例列出（前 10）：")
            shown = 0
            for i in range(1, len(cal)):
                today, sig = cal[i], cal[i + sig_off]
                if blocked(_trading_lag(sig, dt.datetime.combine(today, tod),
                                        augment_calendar(cal[:i], today, (sig,)),
                                        DAY_SESSION_CLOSE), 0):
                    print(f"    today={today}({today.strftime('%a')}) sig={sig}")
                    shown += 1
                    if shown >= 10:
                        break

    # A/B 的分布分析仍用日盘样本（与历史口径一致，便于纵向对比）
    rows = []
    for i in range(1, len(cal)):
        today, sig = cal[i], cal[i - 1]
        rows.append((today, sig, caliber_a(today, sig), caliber_b(today, sig), None))

    n = len(rows)
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
    for today, sig, a, b, _c in sorted(bb, key=lambda r: -r[2])[:5]:
        print(f"    {today}({today.strftime('%a')}) 信号={sig}  A={a:>2}  B={b}(拦截)")


if __name__ == "__main__":
    main()
