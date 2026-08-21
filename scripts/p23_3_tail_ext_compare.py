#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P23-3 tail_ext 与 S4 影子跟踪对齐：v8 vs v8_tail_ext 信号层对比。

背景
----
P22-1 登记 tail_ext（artifacts/signals_cache18_grouped_v8_tail_ext.parquet）为
「引擎 A 信号恢复候选」：共享窗口（≤2026-06-29）与 v8 逐字节一致，仅尾部
（2026-07/08 月，36 个新交易日 / 648 行）追加末折模型外推信号，引擎 A OOS
1.064→1.096（+0.032）。P23-3 任务：把 tail_ext 纳入 S4 影子跟踪框架的对齐评估
（研究性，不改 v8 缓存、不改 hexbroker 包）。

本脚本**复用 p12_s4_shadow_monitor 的函数**（load_close_panel / load_signals /
add_fwd_returns / daily_cs_ic / daily_hit_rate / overlap_corr / pooled_spearman /
rolling_mean / cache_metrics / corr_metrics / evaluate_trigger / evaluate_drift /
OOS_START / HORIZON / ROLL_WINDOW / TOP_K / MIN_SYMBOLS / CORR_ALERT /
CORR_WATCH_DELTA），支持任意两缓存对比（--cache-a/--cache-b），不修改 p12 已
QA 的基线逻辑。

评估维度
--------
  1. 滚动 OOS 截面 IC（63 窗滚动均值）与滚动命中率：tail_ext vs v8
     （tail_ext 的追加信号因 fwd5 未实现 → 尾部 IC 计为 NaN，只影响滚动窗尾部）
  2. 信号相关性（漂移监测核心）：
       - 全窗口池化 Spearman（63 窗）
       - 尾部窗口（>2026-06-29）池化 Spearman —— 评估"尾部有分化"是否显著
       - 日截面滚动均值
  3. fresh 判定：fresh_signal = tail_ext 信号最大日 > v8 信号最大日
     （tail_ext 有 07/08 新信号 → 应 YES）
  4. 主触发评估：滚动 IC/命中率连续胜计数（复用 p12.evaluate_trigger）
  5. 漂移判定：池化相关性 vs 绝对阈值 + P12 基线参考（rt30~v2 池化 ~0.60）

口径铁律：OOS 2024-07-18 后；只用 t 及以前信息（滚动窗尾随）；fwd5 =
close[t+5]/close[t]-1（品种内 shift）；完整回测口径不在此重算（信号层监测）。

用法
----
  python scripts/p23_3_tail_ext_compare.py                              # v8 vs v8_tail_ext
  python scripts/p23_3_tail_ext_compare.py --cache-a ... --cache-b ...  # 任意两缓存
  python scripts/p23_3_tail_ext_compare.py --json                       # stdout 末尾 JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.p12_s4_shadow_monitor import (
    CORR_ALERT,
    CORR_WATCH_DELTA,
    HORIZON,
    IC_THRESHOLD,
    MIN_SYMBOLS,
    OOS_START,
    ROLL_WINDOW,
    TOP_K,
    TRIGGER_CONSEC,
    cache_metrics,
    corr_metrics,
    evaluate_drift,
    evaluate_trigger,
    load_close_panel,
    load_signals,
)

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
TAIL_EXT_PATH = ART / "signals_cache18_grouped_v8_tail_ext.parquet"
LOG_PATH = ART / "p23_s4_tail_ext.log"
TAIL_CUT = pd.Timestamp("2026-06-29")  # v8 末信号日；tail_ext 的追加窗口起点


def _log(msg: str) -> None:
    print(msg)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def tail_window_corr(sig_a: pd.DataFrame, sig_b: pd.DataFrame,
                     cut: pd.Timestamp = TAIL_CUT) -> dict:
    """尾部窗口（> cut）池化 Spearman + 样本量，评估尾部信号分化。

    复用 p12.overlap_corr 的合并逻辑：取两缓存尾部（> cut）的同期 (symbol, ts)
    对一次计算 Spearman。
    """
    m = sig_a[["symbol", "ts", "exp_ret"]].merge(
        sig_b[["symbol", "ts", "exp_ret"]],
        on=["symbol", "ts"], suffixes=("_a", "_b"),
    )
    m_tail = m[m["ts"] > cut]
    if len(m_tail) < 10:
        return {"tail_pairs": int(len(m_tail)), "tail_pooled_corr": float("nan")}
    from scipy.stats import spearmanr

    r = spearmanr(m_tail["exp_ret_a"], m_tail["exp_ret_b"])
    corr = float(getattr(r, "statistic", getattr(r, "correlation", float("nan"))))
    return {
        "tail_pairs": int(len(m_tail)),
        "tail_pooled_corr": corr,
        "tail_date_min": m_tail["ts"].min().date(),
        "tail_date_max": m_tail["ts"].max().date(),
    }


def tail_quality(sig: pd.DataFrame, close_panel: pd.DataFrame,
                 cut: pd.Timestamp = TAIL_CUT) -> dict:
    """tail_ext 追加窗口（> cut）的信号质量：尾部每日 IC/命中率均值。

    v8 在尾部无信号行 → 尾部"分化"无法用两缓存相关性度量（0 重合对），
    只能直接刻画追加信号的已实现质量：仅用 t 及以前信息 + fwd5 已实现
    （尾部 5 日内不足 fwd5 → 不计入）。
    """
    from scripts.p12_s4_shadow_monitor import add_fwd_returns, daily_cs_ic, daily_hit_rate

    sig_t = sig[sig["ts"] > cut].copy()
    if sig_t.empty:
        return {"tail_rows": 0, "tail_dates": 0}
    sig_t = add_fwd_returns(sig_t, close_panel, HORIZON)
    ic_df = daily_cs_ic(sig_t)
    hit_df = daily_hit_rate(sig_t)
    return {
        "tail_rows": int(len(sig_t)),
        "tail_dates": int(sig_t["ts"].nunique()),
        "tail_ic_days": int(ic_df.shape[0]),
        "tail_mean_ic": round(float(ic_df["ic"].mean()), 6) if ic_df.shape[0] else None,
        "tail_mean_hit": round(float(hit_df["hit"].mean()), 6) if hit_df.shape[0] else None,
        "tail_ic_first": ic_df["ts"].min().date().isoformat() if ic_df.shape[0] else None,
        "tail_ic_last": ic_df["ts"].max().date().isoformat() if ic_df.shape[0] else None,
    }


def run_compare(cache_a: Path, cache_b: Path, window: int, json_out: bool) -> dict:
    label_a = cache_a.stem.replace("signals_cache18_grouped_", "")
    label_b = cache_b.stem.replace("signals_cache18_grouped_", "")
    _log("=" * 92)
    _log(f"P23-3 tail_ext 与 S4 对齐：{label_a} vs {label_b}（影子跟踪信号层对比）")
    _log("=" * 92)
    _log(f"  cache_a: {cache_a.name}")
    _log(f"  cache_b: {cache_b.name}")
    _log(f"  窗口: {window} | 前瞻: {HORIZON} | OOS: {OOS_START.date()} | "
         f"min品种: {MIN_SYMBOLS} | top_k: {TOP_K} | 触发连续: {TRIGGER_CONSEC} | "
         f"IC阈值: {IC_THRESHOLD} | 漂移阈值: {CORR_ALERT}")

    t0 = time.time()
    close_panel = load_close_panel()
    sig_a = load_signals(cache_a)
    sig_b = load_signals(cache_b)
    _log(f"  K 线最新: {close_panel['date'].max().date()} | "
         f"{label_a} 行 {len(sig_a)}（{sig_a['ts'].min().date()}~{sig_a['ts'].max().date()}）| "
         f"{label_b} 行 {len(sig_b)}（{sig_b['ts'].min().date()}~{sig_b['ts'].max().date()}）")

    m_a = cache_metrics(sig_a, close_panel, label_a, window=window)
    m_b = cache_metrics(sig_b, close_panel, label_b, window=window)
    cm = corr_metrics(sig_b, sig_a, window=window)
    tail = tail_window_corr(sig_b, sig_a, TAIL_CUT)
    tq = tail_quality(sig_b, close_panel, TAIL_CUT)

    # fresh 判定：tail_ext 有 07/08 新信号（signal_max > v8 signal_max）
    fresh_signal = pd.Timestamp(m_b["signal_max_date"]) > pd.Timestamp(m_a["signal_max_date"])
    fresh_kline = pd.Timestamp(close_panel["date"].max()) > pd.Timestamp(m_a["signal_max_date"])

    # 主触发评估（滚动 IC/命中率连续胜）——复用 p12 逻辑
    trigger = evaluate_trigger(m_b["ic_series"], m_a["ic_series"],
                               m_b["hit_series"], m_a["hit_series"],
                               consec=TRIGGER_CONSEC, ic_threshold=IC_THRESHOLD)

    # 漂移判定：全窗口池化相关性（tail_ext vs v8）
    drift = evaluate_drift(cm["corr_pooled_63d"], None,
                           alert=CORR_ALERT, watch_delta=CORR_WATCH_DELTA)

    result = {
        "monitor_date": pd.Timestamp.now().normalize().date(),
        "params": {"window": window, "horizon": HORIZON, "oos_start": OOS_START.date(),
                   "min_symbols": MIN_SYMBOLS, "top_k": TOP_K,
                   "consec": TRIGGER_CONSEC, "ic_threshold": IC_THRESHOLD,
                   "corr_alert": CORR_ALERT, "corr_watch_delta": CORR_WATCH_DELTA},
        "fresh_kline": bool(fresh_kline),
        "fresh_signal": bool(fresh_signal),
        "kline_max_date": close_panel["date"].max().date(),
        label_a: {k: (v.date() if isinstance(v, pd.Timestamp) else v)
                  for k, v in m_a.items() if k not in ("ic_series", "hit_series")},
        label_b: {k: (v.date() if isinstance(v, pd.Timestamp) else v)
                  for k, v in m_b.items() if k not in ("ic_series", "hit_series")},
        "corr": cm,
        "tail_window": tail,
        "tail_quality": tq,
        "trigger": trigger,
        "drift": drift,
        "p12_reference": {
            "note": "P12 S4 影子跟踪 rt30~v2 池化 63 窗 ~0.60（重标定后参考线，非本对比基线）",
            "rt30_v2_pooled_ref": 0.60,
        },
    }
    _log("")
    _log("[缓存概览]")
    for label in (label_a, label_b):
        m = result[label]
        _log(f"  {label:<10}: {m['n_rows']} 行 | {m['n_dates']} 日 | "
             f"{m['coverage_start']} ~ {m['coverage_end']} | "
             f"日均 {m['avg_symbols_per_day']:.1f} 品种 | OOS {m['oos_n_dates']} 日")
    _log("")
    _log("[滚动指标末值 (63窗滚动均值, OOS)]")
    for label in (label_a, label_b):
        m = result[label]
        _log(f"  {label:<10}: 滚动截面IC={m['rolling_ic_63d']:+.4f} (末值 {m['ic_last_date']}) | "
             f"滚动命中率={m['rolling_hit_63d']:.3f} (末值 {m['hit_last_date']})")
    _log("")
    _log("[信号相关性 (漂移监测核心)]")
    _log(f"  全窗口池化63窗 Spearman = {cm['corr_pooled_63d']:.4f} "
         f"(n_pairs={cm['corr_pooled_n_pairs']}) | 末值 {cm['corr_last_date']}")
    _log(f"  日截面滚动均值 = {cm['corr_daily_rolling_63d']:.4f}")
    if pd.notna(tail["tail_pooled_corr"]):
        _log(f"  尾部窗口(>{TAIL_CUT.date()})池化 Spearman = {tail['tail_pooled_corr']:.4f} "
             f"(n_pairs={tail['tail_pairs']}, {tail['tail_date_min']}~{tail['tail_date_max']})")
    else:
        _log(f"  尾部窗口(>{TAIL_CUT.date()})池化 Spearman = NaN —— "
             f"v8 尾部无信号行（fold 截断），0 重合对 → 尾部'分化'无法用相关性度量，"
             f"直接刻画追加信号质量如下")
    _log(f"  tail_ext 追加窗口质量: {tq['tail_rows']} 行 / {tq['tail_dates']} 日 | "
         f"已实现 IC 日 {tq['tail_ic_days']}（{tq['tail_ic_first']}~{tq['tail_ic_last']}）| "
         f"日均IC={tq['tail_mean_ic']} | 日均命中率={tq['tail_mean_hit']}")
    _log("")
    _log(f"[fresh] K线新: {'YES' if fresh_kline else 'NO'} | "
         f"信号新: {'YES' if fresh_signal else 'NO'}（tail_ext 有 07/08 新信号）")
    t = trigger
    _log(f"[主触发] {t['verdict']} (IC连续胜={t['ic_consec_wins']}/{TRIGGER_CONSEC}, "
         f"命中率连续胜={t['hit_consec_wins']}/{TRIGGER_CONSEC}, 对齐日={t['n_common_dates']})")
    _log(f"[漂移判定] {drift}（绝对阈值 {CORR_ALERT}；P12 rt30~v2 参考 ~0.60）")
    _log(f"\n[OK] 对比完成 | 耗时 {time.time() - t0:.1f}s | 日志 → {LOG_PATH.name}")
    if json_out:
        print("\n===JSON===")
        print(json.dumps(result, ensure_ascii=False, default=str))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="P23-3 tail_ext 与 S4 对齐（v8 vs tail_ext 信号层对比）")
    ap.add_argument("--cache-a", type=str, default=str(V8_PATH),
                    help=f"缓存 A 路径（默认 {V8_PATH.name}）")
    ap.add_argument("--cache-b", type=str, default=str(TAIL_EXT_PATH),
                    help=f"缓存 B 路径（默认 {TAIL_EXT_PATH.name}）")
    ap.add_argument("--window", type=int, default=ROLL_WINDOW,
                    help=f"滚动窗口（默认 {ROLL_WINDOW}）")
    ap.add_argument("--json", action="store_true", help="stdout 末尾输出 JSON 结果块")
    args = ap.parse_args()

    run_compare(Path(args.cache_a), Path(args.cache_b), args.window, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
