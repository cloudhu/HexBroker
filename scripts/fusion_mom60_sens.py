"""P1-1 补充：mom60 融合全样本 + 权重敏感性（验证长窗口动量融合的稳健性）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.evaluation.metrics import compute_metrics
from scripts.combo_validation import OOS_START
from scripts.fusion_signals import build_momentum, run_fusion
from scripts.monitor_adaptive_exposure import CONTRACTS, NOTIONAL_FRAC, build_rolling_spread, build_signals, scale_for

COST = CostModel(
    fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
    slippage_ticks=1.0, margin_rate=0.12, contracts=CONTRACTS,
)


def main() -> None:
    print("=" * 72)
    print("P1-1 补充：mom60 融合全样本 + 权重敏感性")
    print("=" * 72)
    sig, prices = build_signals(6, use_cache=True)
    sig60 = build_momentum(sig, prices, 60)
    spreads = build_rolling_spread(sig, prices, window=20)
    scale = scale_for(spreads, "step", 0.0)
    oos60 = sig60[sig60["ts"] >= OOS_START].copy()

    print("\n=== 全样本（2018~2026-08） ===")
    run_fusion(sig60, prices, scale, "exp_ret", label="C exp_ret top30%")
    run_fusion(sig60, prices, scale, "momentum", label="M1 纯动量60 top30%")
    run_fusion(sig60, prices, scale, "fusion", w1=0.7, w2=0.3, label="F1 0.7exp+0.3mom60")
    run_fusion(sig60, prices, scale, "fusion", w1=0.5, w2=0.5, label="F2 0.5exp+0.5mom60")
    run_fusion(sig60, prices, scale, "fusion", w1=0.3, w2=0.7, label="F3 0.3exp+0.7mom60")

    print("\n=== OOS 段（2024-07-18~2026-06，真新数据） ===")
    run_fusion(oos60, prices, scale, "exp_ret", label="C exp_ret top30%")
    run_fusion(oos60, prices, scale, "momentum", label="M1 纯动量60 top30%")
    run_fusion(oos60, prices, scale, "fusion", w1=0.7, w2=0.3, label="F1 0.7exp+0.3mom60")
    run_fusion(oos60, prices, scale, "fusion", w1=0.5, w2=0.5, label="F2 0.5exp+0.5mom60")
    run_fusion(oos60, prices, scale, "fusion", w1=0.3, w2=0.7, label="F3 0.3exp+0.7mom60")

    print("\n[报告] 见 deliverables/software-hexfutures-ai/multi-signal-fusion-2026-08-17.md")


if __name__ == "__main__":
    main()
