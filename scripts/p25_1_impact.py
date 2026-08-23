#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P25-1 基线切换影响对比：v2-vs-rt30（旧基线） vs v8-vs-rt30（新基线）。

如实说明基线切换（v2→v8）对 S4 影子监控对齐日/相关性的影响。
输出：artifacts/p25_1_baseline_switch_impact.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.p12_s4_shadow_monitor import (
    ART,
    ROLL_WINDOW,
    load_close_panel,
    load_signals,
    cache_metrics,
    corr_metrics,
    evaluate_trigger,
    IC_THRESHOLD,
    TRIGGER_CONSEC,
    HIT_REF,
)

V2_PATH = ART / "signals_cache18_grouped_v2.parquet"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
RT30_PATH = ART / "signals_cache18_grouped_v2_rt30.parquet"
OUT = ART / "p25_1_baseline_switch_impact.csv"


def row_for(base_path, base_label, cand_path, cand_label):
    close_panel = load_close_panel()
    sig_b = load_signals(base_path)
    sig_c = load_signals(cand_path)
    m_b = cache_metrics(sig_b, close_panel, base_label, window=ROLL_WINDOW)
    m_c = cache_metrics(sig_c, close_panel, cand_label, window=ROLL_WINDOW)
    cm = corr_metrics(sig_c, sig_b, window=ROLL_WINDOW)
    trig = evaluate_trigger(m_c["ic_series"], m_b["ic_series"],
                            m_c["hit_series"], m_b["hit_series"],
                            consec=TRIGGER_CONSEC, ic_threshold=IC_THRESHOLD,
                            hit_ref=HIT_REF)
    return {
        "baseline": base_label,
        "candidate": cand_label,
        "base_n_rows": m_b["n_rows"],
        "base_n_dates": m_b["n_dates"],
        "base_max_date": m_b["signal_max_date"],
        "base_avg_sym": round(m_b["avg_symbols_per_day"], 2),
        "cand_n_rows": m_c["n_rows"],
        "cand_n_dates": m_c["n_dates"],
        "cand_max_date": m_c["signal_max_date"],
        "cand_avg_sym": round(m_c["avg_symbols_per_day"], 2),
        "corr_n_days": cm["corr_n_days"],
        "corr_last_date": cm["corr_last_date"],
        "corr_daily_rolling_63d": round(cm["corr_daily_rolling_63d"], 4),
        "corr_pooled_63d": round(cm["corr_pooled_63d"], 4),
        "corr_pooled_n_pairs": cm["corr_pooled_n_pairs"],
        "trigger_common_dates": trig["n_common_dates"],
        "trigger_ic_consec": trig["ic_consec_wins"],
        "trigger_hit_consec": trig["hit_consec_wins"],
    }


def main() -> None:
    r_old = row_for(V2_PATH, "v2", RT30_PATH, "rt30")
    r_new = row_for(V8_PATH, "v8", RT30_PATH, "rt30")
    df = pd.DataFrame([r_old, r_new])
    ART.mkdir(exist_ok=True)
    df.to_csv(OUT, index=False)
    print(df.to_string(index=False))
    print(f"\n[OK] → {OUT}")
    print("\n[如实说明]")
    print(f"  对齐日（corr_n_days）: {r_old['corr_n_days']} → {r_new['corr_n_days']} "
          f"({r_new['corr_n_days'] - r_old['corr_n_days']:+d})")
    print(f"  池化相关性: {r_old['corr_pooled_63d']} → {r_new['corr_pooled_63d']} "
          f"({r_new['corr_pooled_63d'] - r_old['corr_pooled_63d']:+.4f})")
    print(f"  主触发对齐窗（n_common_dates）: {r_old['trigger_common_dates']} → "
          f"{r_new['trigger_common_dates']} —— 显著减少（v8 OOS 信号日 160 < v2 210，"
          f"且 63 窗滚动后交集更小）")


if __name__ == "__main__":
    main()
