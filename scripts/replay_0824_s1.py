#!/usr/bin/env python
# scripts/replay_0824_s1.py
# 复跑 2026-08-24 上午「做多后 60s 秒平」场景，对比 S1 修复前/后的开-平循环。
#
# 场景（来自真实成交取证）：rb0 价格在 3038↔3036 两值间交替（60s 步长）、
# 日线 MA20≈3037（恰落其间）、信号 p_up=0.733 恒定（多头意图）。
# 修复前：S1 无缓冲无带宽 → 3036<3037 触发 → 开-平循环。
# 修复后：开仓缓冲(min_bars=2) + MA 带宽死区(0.1×ATR) → 贴线穿越被过滤 → 持续持仓。
#
# 用法：
#   python scripts/replay_0824_s1.py [--rounds 60] [--mode old|new|both]
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.risk_gate import RiskGate  # noqa: E402
from hexbroker.paper.signals import SignalFrame  # noqa: E402
from hexbroker.paper.types import AccountSnapshot, PositionCtx, Quote  # noqa: E402

# ---- 场景参数（来自 8/24 上午取证） ----
SYMBOL = "rb0"
PRICES = [3038.0, 3036.0]        # 两值交替（真实成交价）
MA20 = 3037.0                    # 日线 MA20（落在两值之间）
P_UP = 0.7333                    # 信号 p_up（恒定多头）
ATR = 40.0                       # rb0 日线 ATR（量级参考）
EQUITY = 100_000.0
INTENT = 0.30                    # RiskGate default_intent


def _risk_cfg(s1_min_bars: int, s1_band_atr: float) -> OmegaConf:
    return OmegaConf.create({
        "risk": {
            "vol_target": 0.20,
            "kelly_cap": 0.25,
            "max_position_pct": 0.30,
            "recovery_drawdown_r1": 0.05,
            "recovery_drawdown_r2": 0.10,
            "recovery_drawdown_r3": 0.15,
            "position_scalar_r1": 0.5,
            "position_scalar_r2": 0.0,
            "position_scalar_r3": 0.2,
            "position_scalar_r4": 1.0,
            "vol_low_q": 0.2,
            "vol_high_q": 0.8,
            "sell_s1_min_bars": s1_min_bars,
            "sell_s1_band_atr": s1_band_atr,
        }
    })


def _quote(price: float, t: datetime) -> Quote:
    return Quote(symbol=SYMBOL, ts=t, price=price, open=price, high=price + 1, low=price - 1, pre_settle=price)


def _acct() -> AccountSnapshot:
    return AccountSnapshot(ts=datetime(2026, 8, 24, 9, 5), equity=EQUITY, cash=EQUITY, margin_used=0.0,
                           drawdown=0.0, peak_equity=EQUITY)


def _sig(now: datetime) -> SignalFrame:
    return SignalFrame(symbol=SYMBOL, ts=now, p_up=P_UP, exp_ret=0.0,
                       is_effective=True, source="replay", freshness_days=1)


def replay(rounds: int, s1_min_bars: int, s1_band_atr: float) -> dict:
    """模拟 60s 步长决策循环，返回 (开仓次数, 平仓次数, 末态持仓)。"""
    gate = RiskGate(_risk_cfg(s1_min_bars, s1_band_atr), default_intent=INTENT)
    position = 0.0
    entry = 0.0
    bars_in_pos = 0
    opens = closes = 0

    t0 = datetime(2026, 8, 24, 9, 5, 0)
    for i in range(rounds):
        price = PRICES[i % 2]
        now = t0 + timedelta(minutes=i)
        quote = _quote(price, now)
        acct = _acct()
        pos_ctx = PositionCtx(
            symbol=SYMBOL, position=position, entry_price=entry, atr=ATR,
            bars_in_position=bars_in_pos,
            highest_since_entry=max(entry, price) if entry else price,
            lowest_since_entry=min(entry, price) if entry else price,
        )
        decision = gate.evaluate(
            _sig(now), quote, acct, pos_ctx,
            recent_returns=np.array([]), recent_volumes=np.array([]), ma_price=MA20,
        )
        if decision.liquidate and abs(position) > 1e-12:
            closes += 1
            position = 0.0
            entry = 0.0
            bars_in_pos = 0
        elif abs(decision.target_position) > 1e-9 and abs(position) < 1e-12:
            opens += 1
            position = 1.0 if decision.target_position > 0 else -1.0
            entry = price
            bars_in_pos = 1
        elif abs(position) > 1e-12:
            bars_in_pos += 1
    return {"rounds": rounds, "opens": opens, "closes": closes, "end_position": position}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=60)
    ap.add_argument("--mode", default="both", choices=["old", "new", "both"])
    args = ap.parse_args()

    print(f"场景复现（rb0，MA20={MA20}，价格 3038↔3036 交替，p_up={P_UP}，ATR={ATR}，{args.rounds} 轮/60s 步长）")
    print("=" * 72)
    if args.mode in ("old", "both"):
        r_old = replay(args.rounds, s1_min_bars=1, s1_band_atr=0.0)  # 修复前：无缓冲、无带宽
        print(f"[修复前] min_bars=1 band=0.0 : 开仓 {r_old['opens']} 次 | 平仓 {r_old['closes']} 次 | 末态持仓 {r_old['end_position']}")
    if args.mode in ("new", "both"):
        r_new = replay(args.rounds, s1_min_bars=2, s1_band_atr=0.1)  # 修复后：缓冲 2 根 + 带宽 0.1×ATR
        print(f"[修复后] min_bars=2 band=0.1*ATR: 开仓 {r_new['opens']} 次 | 平仓 {r_new['closes']} 次 | 末态持仓 {r_new['end_position']}")
    if args.mode == "both":
        print("=" * 72)
        print("预期：修复前开-平循环（开≈平≈rounds/2）；修复后持续持仓（开=1、平=0）。")


if __name__ == "__main__":
    main()
