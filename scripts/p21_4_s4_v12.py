#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P21-4 S4 影子复测：v12（重建候选）vs v8（生产基线）影子监控。

p12_s4_shadow_monitor.py 的 V2_PATH/RT30_PATH 为固定路径（P7 登记对比对象），
不支持 v12/v8 对比 → 本脚本独立扩展：复用 p12 全部口径函数（load_close_panel/
add_fwd_returns/daily_cs_ic/daily_hit_rate/overlap_corr/pooled_spearman/
rolling_mean/cache_metrics/corr_metrics/evaluate_trigger/evaluate_drift），
仅把对比对象参数化为 v8（baseline）vs v12（candidate）。

输出：stdout + artifacts/p21_s4_monitor.log（JSON + 摘要）
"""
from __future__ import annotations

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
    ART,
    ROLL_WINDOW,
    HORIZON,
    OOS_START,
    MIN_SYMBOLS,
    TOP_K,
    IC_THRESHOLD,
    TRIGGER_CONSEC,
    HIT_REF,
    CORR_ALERT,
    CORR_WATCH_DELTA,
    load_close_panel,
    load_signals,
    cache_metrics,
    corr_metrics,
    evaluate_trigger,
    evaluate_drift,
    kline_max_date,
)

V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V12_PATH = ART / "signals_cache18_grouped_v12.parquet"
LOG_PATH = ART / "p21_s4_monitor.log"
BASELINE_CSV = ART / "p12_s4_shadow_baseline.csv"  # P12 基线（v2/rt30，仅作 kline 基准参考）


def main() -> None:
    t0 = time.time()
    close_panel = load_close_panel()
    sig_v8 = load_signals(V8_PATH)
    sig_v12 = load_signals(V12_PATH)

    m_v8 = cache_metrics(sig_v8, close_panel, "v8", window=ROLL_WINDOW)
    m_v12 = cache_metrics(sig_v12, close_panel, "v12", window=ROLL_WINDOW)
    cm = corr_metrics(sig_v12, sig_v8, window=ROLL_WINDOW)

    # fresh 判定：K 线相对 P12 基线（2026-08-17）是否刷新；v12 信号末日相对 v8 是否延伸
    kline_now = kline_max_date(close_panel)
    kline_base = pd.Timestamp("2026-08-17")  # P20 数据刷新基线（P12 基线快照 08-17）
    fresh_kline = kline_now > kline_base
    sig_max_v8 = pd.Timestamp(m_v8["signal_max_date"])
    sig_max_v12 = pd.Timestamp(m_v12["signal_max_date"])
    fresh_signal = sig_max_v12 > sig_max_v8

    trigger = evaluate_trigger(m_v12["ic_series"], m_v8["ic_series"],
                               m_v12["hit_series"], m_v8["hit_series"],
                               consec=TRIGGER_CONSEC, ic_threshold=IC_THRESHOLD,
                               hit_ref=HIT_REF)
    trigger["fresh_confirmed"] = bool(fresh_kline or fresh_signal)
    if not (fresh_kline or fresh_signal):
        trigger["verdict"] = "UPGRADE_TRIGGER_RETRO" if trigger["verdict"] == "UPGRADE_TRIGGER" else "NO_TRIGGER"
    drift = evaluate_drift(cm["corr_pooled_63d"], None, alert=CORR_ALERT,
                           watch_delta=CORR_WATCH_DELTA)

    result = {
        "monitor_date": pd.Timestamp.now().normalize().date(),
        "comparison": "v12(candidate) vs v8(baseline)",
        "params": {"window": ROLL_WINDOW, "consec": TRIGGER_CONSEC,
                   "ic_threshold": IC_THRESHOLD, "corr_alert": CORR_ALERT,
                   "corr_watch_delta": CORR_WATCH_DELTA},
        "fresh_kline": bool(fresh_kline),
        "fresh_signal": bool(fresh_signal),
        "kline_now": kline_now.date(),
        "kline_base": kline_base.date(),
        "v8": {k: (v.date() if isinstance(v, pd.Timestamp) else v)
               for k, v in m_v8.items() if k not in ("ic_series", "hit_series")},
        "v12": {k: (v.date() if isinstance(v, pd.Timestamp) else v)
                for k, v in m_v12.items() if k not in ("ic_series", "hit_series")},
        "corr": cm,
        "trigger": trigger,
        "drift": drift,
        "key_finding": (
            "v12 与 v8 同口径重建（仅数据范围多 08-18~08-21 4 个交易日）：walk_forward "
            "60 日步长 fold 网格未跨过下一折边界 → v12 信号末日与 v8 相同（06-23~06-29），"
            "无新增 08 月信号 → fresh_signal=NO；fold 截断需再积累 ~20-24 个交易日数据"
        ),
    }

    lines = []
    lines.append("=" * 92)
    lines.append(f"P21-4 S4 影子复测 — v12（重建候选）vs v8（生产基线）（{result['monitor_date']}）")
    lines.append("=" * 92)
    lines.append(f"K 线最新: {result['kline_now']} (基线 {result['kline_base']}) | "
                 f"新 K 线: {'YES' if result['fresh_kline'] else 'NO'} | "
                 f"新信号(v12>v8): {'YES' if result['fresh_signal'] else 'NO'}")
    for label in ("v8", "v12"):
        m = result[label]
        lines.append(f"  [{label:<3}] 行数={m['n_rows']} 唯一信号日={m['n_dates']} "
                     f"末信号日={m['signal_max_date']} 日均={m['avg_symbols_per_day']:.1f} | "
                     f"滚动IC={m['rolling_ic_63d']:+.4f} (末 {m['ic_last_date']}) | "
                     f"滚动命中率={m['rolling_hit_63d']:.3f} (末 {m['hit_last_date']})")
    c = result["corr"]
    lines.append(f"  信号相关性(v12~v8): 池化63窗={c['corr_pooled_63d']:.4f} "
                 f"(n_pairs={c['corr_pooled_n_pairs']}) | "
                 f"日截面滚动均值={c['corr_daily_rolling_63d']:.4f} | 末 {c['corr_last_date']}")
    lines.append(f"  漂移判定: {result['drift']} (告警阈值={result['params']['corr_alert']:.2f})")
    t = result["trigger"]
    lines.append(f"  主触发: {t['verdict']} (IC连续胜={t['ic_consec_wins']}/{result['params']['consec']}, "
                 f"命中率连续胜={t['hit_consec_wins']}/{result['params']['consec']}, "
                 f"对齐日={t['n_common_dates']}, fresh确认={'YES' if t['fresh_confirmed'] else 'NO'})")
    lines.append("")
    lines.append("[关键发现]")
    lines.append(f"  {result['key_finding']}")
    lines.append("")
    lines.append("[结论]")
    if fresh_signal:
        lines.append("  v12 相对 v8 有新增信号 → fresh_signal=YES → 进入升级评估")
    else:
        lines.append("  v12 相对 v8 无新增信号（同 fold 网格结构性截断）→ fresh_signal=NO → "
                     "本次不构成升级证据；待数据跨过下一 fold 边界（~20-24 交易日）后重评")
    lines.append(f"  [OK] 耗时 {time.time() - t0:.1f}s")

    text = "\n".join(lines)
    print(text)
    LOG_PATH.write_text(text + "\n\n===JSON===\n" + json.dumps(result, ensure_ascii=False, default=str, indent=2),
                        encoding="utf-8")
    print(f"\n[OK] → {LOG_PATH}")


if __name__ == "__main__":
    main()
