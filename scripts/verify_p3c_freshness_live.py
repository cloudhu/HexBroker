"""P3-C 新鲜度口径判决性验证（交易日 lag，阈值 0）。

取代 ``verify_p3b_freshness_live.py``（自然日差口径，已证伪）：
    P3-B 的 V1 断言「周一必须拦截 fd=3」本身就是误伤，本脚本将其**反转**为
    V1「周一必须放行 lag=0」。旧脚本保留在 git 历史中作口径演化的留档。

⚠️ 方法论（沿用 v2 的修正，勿回退）：
    不能用「当前缓存 + 历史 asof」直接查询 —— 那属**离线反事实**，
    今天的缓存已含事后生成的信号，回溯查历史自然"新鲜"，是假象。
    **实盘仿真必须先把缓存截断到 asof 之前**，本脚本每个场景显式构造快照。

验证项（全部使用真实交易日历，来自主湖并集）：
    V1 周一               08-31 用 08-28(周五) 信号 → lag=0 → ✅ 放行（P3-B 误伤处）
    V2 常规隔夜           08-28 用 08-27(周四) 信号 → lag=0 → ✅ 放行
    V3 假期后首日         02-24 用 02-13(春节前) 信号 → lag=0 → ✅ 放行（B 口径也误伤处）
    V4 P0-3 真病灶        08-24 用 08-21(周五) 信号 → lag=1 → ⛔ 拦截（必须仍拦得住）
    V5 缓存停更 5 日      落后 5 个交易日            → lag=5 → ⛔ 拦截
    V6 夜盘（当日信号）   08-28 21:30 用 08-28 信号 → lag=-1 → ✅ 放行（负值非异常）
    V7 日历不可用         → 保守拦截 + 显式告警（门禁绝不放无法判定的信号）
    V8 前视泄露检验       p_up[T] 与 T+5 收益相关性 > T 当日收益

⚠️⛔ V4 的历史教训：**不要用事后完整日历验证历史事故**（2026-08-31 QA 揪出）
    事故当时主湖只到 08-21，日历里**没有** 08-24。V4 用今天的完整日历跑出
    lag=1 拦截 —— 结论对，但**证据是事后补数造出来的假象**，无法证明事故发生时
    门禁拦得住。故新增 V9/V10/V11，全部用**事故当时的日历**验证：

    V9  夜盘缓存停更 1 交易日（主湖无当日）    → lag=1    → ⛔ 拦截（旧版 lag=0 放行 = 失明）
    V10 夜盘行为反转修复（缓存有当日信号但主湖无当日 bar）
                                              → lag=0    → ✅ 放行（未增补则 None 拦截 = 反转）
    V11 P0-3 **事故当时日历**（截断至 08-21）  → lag=1    → ⛔ 拦截（V4 的诚实版）

    ⛔ V9/V11 现在给出**确定值**而非 None，靠的是日历与「数据是否已补」解耦：
    「今天是不是交易日」是独立知识（周一至周五 + 节假日表），不该由「主湖有没有
    那天的 bar」回答。解耦后守卫才能报「落后 1 个交易日」这种**可执行**的结论，
    而不是「无法判定」。

只读，不写任何生产文件。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from omegaconf import OmegaConf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hexbroker.paper.signals import (  # noqa: E402
    SignalEngine,
    _calendar_days,
    load_trading_calendar,
)

SYMBOLS = ["rb0", "ag0", "cu0", "ni0", "au0", "al0", "i0", "j0", "m0", "y0"]

# 场景：(名称, 信号日, asof（**必须带时刻**：决定当日日盘是否已收盘）, 期望 lag, 期望放行)
#   · 10:30 = 日盘中，当日未收盘 → ref 取上一交易日
#   · 21:30 = 夜盘，当日 15:00 已收盘 → ref 取当日
SCENARIOS = [
    ("V1 周一（信号=上周五）", "2026-08-28", "2026-08-31 10:30", 0, True),
    ("V2 常规隔夜（周四→周五）", "2026-08-27", "2026-08-28 10:30", 0, True),
    ("V3 假期后首日（春节前→后）", "2026-02-13", "2026-02-24 10:30", 0, True),
    # P0-3 事故：08-24 夜盘时 08-24 日盘已收盘（数据可得），缓存却仍是 08-21 → 落后 1 个交易日
    ("V4 P0-3 真病灶（夜盘漏刷）", "2026-08-21", "2026-08-24 21:30", 1, False),
    # 同信号、同日，但放在日盘（当日未收盘）→ ref=08-21 → 标准 T+1，放行（对照组）
    ("V4b 同信号在日盘（对照）", "2026-08-21", "2026-08-24 10:30", 0, True),
    ("V5 缓存停更 5 个交易日", "__LAG5__", "__LAG5__", 5, False),
    ("V6 夜盘（当日信号已刷新）", "2026-08-28", "2026-08-28 21:30", 0, True),
    # 负 lag：asof 是纯日期（视为盘前，ref 回退到上一交易日），但缓存已含当日信号
    ("V6b 负 lag（盘前 asof + 当日信号）", "2026-08-28", "2026-08-28", -1, True),
]


def _mini_cache(symbols: list[str], ts: str) -> pd.DataFrame:
    """构造单日最小信号缓存（校准口径用，不依赖生产缓存内容）。"""
    return pd.DataFrame(
        {
            "symbol": symbols,
            "ts": pd.to_datetime([ts] * len(symbols)),
            "p_up": [0.62] * len(symbols),
            "exp_ret": [0.5] * len(symbols),
            "is_effective": [True] * len(symbols),
        }
    )


def make_engine(cache: pd.DataFrame, tmpdir: Path, **kw) -> SignalEngine:
    """用内存快照构造 SignalEngine（不落盘生产缓存）。"""
    p = Path(tmpdir) / "snap.parquet"
    cache.to_parquet(p)
    return SignalEngine(cache_paths=[p], **kw)


def main() -> int:
    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    thr = int(OmegaConf.select(cfg, "paper.freshness_threshold_trading_days"))
    print(f"[cfg] freshness_threshold_trading_days = {thr}（应为 0）")
    assert thr == 0, "配置未生效（阈值应为 0 = 标准 T+1）"

    cal = load_trading_calendar()
    print(f"[日历] 主湖并集 {len(cal)} 天：{cal[0]} ~ {cal[-1]}\n")
    if not cal:
        print("⛔ 交易日历为空，无法验证")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="p3c_"))
    results: dict[str, bool] = {}

    print("=" * 74)
    print("口径场景验证（交易日 lag，阈值 0）")
    print("=" * 74)
    for name, sig_day, asof, exp_lag, exp_ok in SCENARIOS:
        if sig_day == "__LAG5__":
            # 停更 5 个交易日：日历末位往前数 5 个交易日作信号日，末位夜盘作 asof
            sig_day, asof = cal[-6].isoformat(), f"{cal[-1].isoformat()} 21:30"
        eng = make_engine(_mini_cache(SYMBOLS, sig_day), tmpdir,
                          freshness_threshold_trading_days=thr)
        sig = eng.latest_signal("rb0", asof)
        got_lag = sig.freshness_days if sig else None
        got_ok = bool(sig and sig.is_effective)
        ok = (got_lag == exp_lag) and (got_ok == exp_ok)
        asof_day = pd.Timestamp(asof).date()
        print(f"  {name}")
        print(f"    信号={sig_day}  asof={asof_day}  →  lag={got_lag}（期望 {exp_lag}）  "
              f"放行={got_ok}（期望 {exp_ok}）  {'✅ PASS' if ok else '❌ FAIL'}")
        print(f"    [对照·自然日差] {_calendar_days(date.fromisoformat(sig_day), asof_day)}"
              f"  ← P3-B 口径，注意它与信息陈旧无关")
        results[name.split()[0]] = ok
    print()

    # ---------------- V7：日历不可用 → 保守拦截（绝不放行无法判定的信号）----------------
    print("=== V7  交易日历不可用 → 必须保守拦截 ===")
    eng = make_engine(_mini_cache(SYMBOLS, "2026-08-28"), tmpdir,
                      freshness_threshold_trading_days=thr, trading_calendar=[])
    sig7 = eng.latest_signal("rb0", "2026-08-31 10:30")
    ok_v7 = sig7 is not None and sig7.freshness_days == 10 ** 9 and sig7.is_effective is False
    print(f"  空日历 → lag={sig7.freshness_days if sig7 else 'n/a'}  "
          f"effective={sig7.is_effective if sig7 else 'n/a'}")
    print(f"  V7 {'✅ PASS' if ok_v7 else '❌ FAIL'}：无法判定 → 保守拦截并告警，不静默放行\n")
    results["V7"] = ok_v7

    # ---------------- V9/V10/V11：夜盘门禁（用**事故当时**的日历，不用事后完整日历）----
    # QA 揪出的 P0 缺陷：旧版 `bisect_right(cal, ref_day) - 1` 在 ref_day 不在日历时
    # 静默回退到上一交易日 → 夜盘缓存停更整一个交易日却算出 lag=0 放行（门禁失明）；
    # 且 20:30 刷出当日信号的反因当日不在日历而被拦（行为反转）。
    print("=== V9~V11  夜盘门禁（事故当时日历，非事后完整日历）===")
    lake_last = cal[-1]                      # 主湖并集末位（当前 2026-08-28）
    cal_no_today = [d for d in cal if d <= lake_last]   # 主湖无当日 —— 夜盘补数前的真实状态

    # V9：夜盘缓存停更 1 个交易日。asof 用主湖末位**之后**的第一个自然日（周一），
    #     当日已收盘但主湖没有 → R 必须严格命中当日 → 不在日历 → None → 拦截
    v9_asof_day = lake_last.fromordinal(lake_last.toordinal() + 3)   # 08-28(五) → 08-31(一)
    v9 = make_engine(_mini_cache(SYMBOLS, lake_last.isoformat()), tmpdir,
                     freshness_threshold_trading_days=thr, trading_calendar=cal_no_today)
    sig9 = v9.latest_signal("rb0", f"{v9_asof_day.isoformat()} 21:30")
    lag9 = sig9.freshness_days if sig9 else None
    ok_v9 = lag9 == 1 and sig9.is_effective is False    # 确定值 1（旧版为 0 → 漏放）
    print(f"  V9  夜盘缓存停更1交易日  信号={lake_last} asof={v9_asof_day} 21:30")
    print(f"      → lag={lag9}（期望 1=落后一个交易日）ef={sig9.is_effective}（期望 False）  "
          f"{'✅ PASS' if ok_v9 else '❌ FAIL'}")
    print("      （旧版在此算 lag=0 放行 —— 门禁失明，P0-3 事故原样复现）")
    results["V9"] = ok_v9

    # V10：行为反转修复。同样主湖无当日，但缓存**当天已刷出当日信号** →
    #      `_calendar_for` 把「缓存中 ≤ ref_day 的信号日」并入日历 → R 命中当日 → lag=0 放行
    v10 = make_engine(_mini_cache(SYMBOLS, v9_asof_day.isoformat()), tmpdir,
                      freshness_threshold_trading_days=thr, trading_calendar=cal_no_today)
    sig10 = v10.latest_signal("rb0", f"{v9_asof_day.isoformat()} 21:30")
    lag10 = sig10.freshness_days if sig10 else None
    ok_v10 = lag10 == 0 and sig10.is_effective is True
    print(f"  V10 夜盘当日已刷新（主湖仍无当日）信号={v9_asof_day} asof={v9_asof_day} 21:30")
    print(f"      → lag={lag10}（期望 0）ef={sig10.is_effective}（期望 True）  "
          f"{'✅ PASS' if ok_v10 else '❌ FAIL'}")
    print("      （旧版在此返回 None 拦截 —— 越新的信号越被拦 = 行为反转）")
    results["V10"] = ok_v10

    # V11：P0-3 事故的**当时**日历（主湖截断至 08-21），夜盘 08-24 用 08-21 信号
    cal_0821 = [d for d in cal if d <= date(2026, 8, 21)]
    v11 = make_engine(_mini_cache(SYMBOLS, "2026-08-21"), tmpdir,
                      freshness_threshold_trading_days=thr, trading_calendar=cal_0821)
    sig11 = v11.latest_signal("rb0", "2026-08-24 21:30")
    lag11 = sig11.freshness_days if sig11 else None
    ok_v11 = lag11 == 1 and sig11.is_effective is False   # 确定值 1（不依赖事后补数）
    print("  V11 P0-3 事故当时日历（止 08-21） 信号=2026-08-21 asof=2026-08-24 21:30")
    print(f"      → lag={lag11}（期望 1=落后一个交易日）ef={sig11.is_effective}（期望 False）  "
          f"{'✅ PASS' if ok_v11 else '❌ FAIL'}")
    print("      判定得出同样的 1，事故发生时即成立 —— 这才是 V4 应有的诚实证据）")
    results["V11"] = ok_v11
    print()

    # ---------------- V8：前视泄露检验（horizon=5）----------------
    print("=== V8  前视泄露检验（horizon=5）===")
    HORIZON = 5
    ok_v8 = True
    cache = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
    if not cache.exists():
        print("  缓存缺失，跳过")
    else:
        raw = pd.read_parquet(cache)
        raw["ts"] = pd.to_datetime(raw["ts"]).dt.tz_localize(None)
        for s in ["rb0", "ag0", "cu0", "ni0"]:
            try:
                bars = pd.read_parquet(ROOT / f"data/raw/processed/{s}/1d/2026.parquet")
            except Exception:
                print(f"  {s}: 湖数据缺失，跳过")
                continue
            bars["d"] = pd.to_datetime(bars["datetime"]).dt.date
            close = bars.set_index("d")["close"].astype(float).sort_index()
            same_day = close.pct_change()
            fwd_h = close.shift(-HORIZON) / close - 1.0
            sub = raw[raw["symbol"] == s].copy()
            sig_s = sub.set_index(pd.to_datetime(sub["ts"]).dt.date)["p_up"].astype(float)
            sig_s = sig_s[sig_s.index >= date(2026, 1, 1)]
            aligned = pd.DataFrame({"p_up": sig_s, "same": same_day, "fwd": fwd_h}).dropna()
            if len(aligned) < 30:
                print(f"  {s}: 样本不足({len(aligned)})，跳过")
                continue
            c_same = aligned["p_up"].corr(aligned["same"])
            c_fwd = aligned["p_up"].corr(aligned["fwd"])
            good = abs(c_fwd) > abs(c_same)
            ok_v8 = ok_v8 and good
            print(f"  {s:<4} n={len(aligned):<4} corr(当日)={c_same:+.4f}  "
                  f"corr(T+{HORIZON})={c_fwd:+.4f}   {'✅ 正常' if good else '⚠️ 复核'}")
    print(f"  V8 {'✅ 正常' if ok_v8 else '⚠️ 观察项'}")
    # ⛔ V8 是**信号质量观察项**，不参与本次口径判定的总判定：
    #    样本量 n=68 且 horizon=5 标签重叠（lag-1 自相关 ρ=+0.759）→ 有效样本量 ≈9，
    #    单点相关系数不足以判定「是否存在前视泄露」或「信号是否失效」。
    #    需要结论时改用分块自助法：scripts/ic_audit_block_bootstrap.py
    print("     （信号质量观察项，不计入口径判定；显著性须用分块自助法复核）\n")

    print("=" * 74)
    for k, v in results.items():
        print(f"  {k}: {'✅ PASS' if v else '❌ FAIL'}")
    print("=" * 74)
    all_ok = all(results.values())
    print(f"总判定（口径 V1~V7 + 夜盘门禁 V9~V11）："
          f"{'✅ 全部 PASS' if all_ok else '⛔ 存在未通过项'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
