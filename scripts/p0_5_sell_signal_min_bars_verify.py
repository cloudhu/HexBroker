"""P0-5 S2/S5 开仓保护门 —— 端到端验证（只读，零生产副作用）。

背景
----
2026-09-01 14:00:10 模拟盘 rb0 开多 @3197，14:01:10 即被 **S5 波动异常**平仓。
取证链（artifacts/_tmp/）：
  - ``p0_2_caliber_crosscheck.py``  : 生产 bars 来自 sina（名义价），与主湖复权口径无关
  - ``p0_2_sina_caliber.py``        : sina close==raw_close==adj_close（k=1），全链路名义自洽
  - ``p0_5_s5_lag_repro.py``        : sina 日线不含当日 → recent_returns[-1] 是昨日收益
                                      08-31 涨 2.731%，z=3.6411 > 3.0
  - ``p0_5_s5_impact_scan.py``      : 触发率 现状 0.70% vs 改当日收益口径 2.62%
                                      → 不做口径替换（会破坏回测对齐且更激进）

结论：真正缺陷是 **S2/S5 缺 S1 已有的开仓保护门**，导致「开仓 → 下一 tick 秒平」。

本脚本**调用生产实现** ``hexbroker.risk.sell_engine.detect_sell_signals``，
不自带任何生产逻辑副本。只读，不写生产数据。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hexbroker.constants import SellSignalCode  # noqa: E402
from hexbroker.risk.sell_engine import detect_sell_signals  # noqa: E402
from hexbroker.risk.types import RiskState  # noqa: E402

PASS = 0
FAIL = 0
LINES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        line = f"  ✅ {name}"
    else:
        FAIL += 1
        line = f"  ⛔ {name}"
    if detail:
        line += f"  —— {detail}"
    print(line)
    LINES.append(line)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 事故现场（data/paper/trades.log, 2026-09-01 14:01:10）
# ---------------------------------------------------------------------------
TRACE = dict(
    symbol="rb0", position=1.0, current_price=3197.0, entry_price=3198.0,
    atr=30.9286, ma_price=3038.6, bars_in_position=1,
)


def _cfg(**kw) -> SimpleNamespace:
    base = dict(
        sell_min_bars=2, sell_s1_window=20, sell_s1_band_atr=0.1,
        sell_s3_target=0.10, sell_s4_bars=20, sell_s5_z=3.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _state(**kw) -> RiskState:
    base = dict(symbol="rb0", position=1.0, current_price=3197.0, entry_price=3198.0,
                atr=30.9286, bars_in_position=1, pnl_pct=0.0, realized_vol=0.0097,
                equity=94906.28, peak_equity=100000.0)
    base.update(kw)
    return RiskState(**base)


def _spike_returns(n: int = 20, spike: float = 0.027314) -> np.ndarray:
    """复刻 rb0 2026-08-31：前 19 根 ±0.4% 往复，末根 +2.731%。"""
    base = np.array([0.004 if i % 2 == 0 else -0.004 for i in range(n - 1)], dtype=float)
    return np.concatenate([base, [spike]])


def main() -> None:
    rets = _spike_returns()
    z = abs((rets[-1] - rets[-20:].mean()) / rets[-20:].std())

    section("A. 事故现场复现：S5 必须被开仓保护门拦下")
    print(f"  构造序列 z = {z:.4f}（阈值 3.0，对应实盘 08-31 的 z=3.6411）")
    sigs = detect_sell_signals(_state(bars_in_position=1), rets, np.array([]),
                               ma_price=TRACE["ma_price"], cfg=_cfg())
    check("A1 bars_in_position=1（开仓当日）不触发 S5",
          SellSignalCode.S5_VOLATILITY_SPIKE not in sigs, f"signals={[s.value for s in sigs]}")

    sigs2 = detect_sell_signals(_state(bars_in_position=2), rets, np.array([]),
                                ma_price=TRACE["ma_price"], cfg=_cfg())
    check("A2 bars_in_position=2（隔夜后）S5 恢复工作",
          SellSignalCode.S5_VOLATILITY_SPIKE in sigs2, f"signals={[s.value for s in sigs2]}")

    sigs_none = detect_sell_signals(_state(bars_in_position=1), rets, np.array([]),
                                    ma_price=None, cfg=None)
    check("A3 cfg=None 时缺省门 = 2 仍生效",
          SellSignalCode.S5_VOLATILITY_SPIKE not in sigs_none)

    section("B. 统一门 sell_min_bars 与逐信号覆写")
    # 大跌 spike：S1（价低于 MA）+ S2（放量下跌）+ S5（|z|>3）三者可同现。
    # 注意 S2 要求 `not price_up`，用正收益 spike 时 S2 天然不成立（非缺陷）。
    rets_drop = _spike_returns(spike=-0.027314)
    vols_spike = np.array([100.0] * 19 + [500.0])
    cfg3 = _cfg(sell_min_bars=3)
    blocked = [detect_sell_signals(_state(bars_in_position=b, current_price=3000.0), rets_drop,
                                   vols_spike, ma_price=4000.0, cfg=cfg3)
               for b in (1, 2)]
    check("B1 sell_min_bars=3 时 bars=1/2 全部静默（S1/S2/S5 同受统一门）",
          all(s == [] for s in blocked), f"signals={[[x.value for x in s] for s in blocked]}")
    sigs3 = detect_sell_signals(_state(bars_in_position=3, current_price=3000.0), rets_drop,
                                vols_spike, ma_price=4000.0, cfg=cfg3)
    check("B2 bars=3 时 S1/S2/S5 同时解禁",
          {s.value for s in sigs3} >= {"S1", "S2", "S5"}, f"signals={[s.value for s in sigs3]}")

    cfg_ov = _cfg(sell_min_bars=2, sell_s5_min_bars=4)
    check("B3 sell_s5_min_bars=4 覆写生效（bars=3 仍拦）",
          SellSignalCode.S5_VOLATILITY_SPIKE not in detect_sell_signals(
              _state(bars_in_position=3), rets, np.array([]), ma_price=None, cfg=cfg_ov))
    check("B4 sell_s5_min_bars=4 覆写生效（bars=4 解禁）",
          SellSignalCode.S5_VOLATILITY_SPIKE in detect_sell_signals(
              _state(bars_in_position=4), rets, np.array([]), ma_price=None, cfg=cfg_ov))

    section("C. S2 保护与健壮性")
    vols = np.array([100.0] * 19 + [500.0])
    rets_down = np.concatenate([np.zeros(19), [-0.01]])
    check("C1 S2 在 bars=1 被拦（此前无保护）",
          SellSignalCode.S2_VOL_DIVERGENCE not in detect_sell_signals(
              _state(bars_in_position=1), rets_down, vols, ma_price=None, cfg=_cfg()))
    check("C2 S2 在 bars=2 正常工作",
          SellSignalCode.S2_VOL_DIVERGENCE in detect_sell_signals(
              _state(bars_in_position=2), rets_down, vols, ma_price=None, cfg=_cfg()))
    try:
        detect_sell_signals(_state(bars_in_position=5), np.array([]), vols,
                            ma_price=None, cfg=_cfg())
        check("C3 volumes 非空 / returns 为空时不抛 IndexError", True)
    except Exception as exc:
        check("C3 volumes 非空 / returns 为空时不抛 IndexError", False,
              f"{type(exc).__name__}: {exc}")

    section("D. 回归：既有行为不得改变")
    st_s1 = _state(bars_in_position=1, current_price=3036.0, atr=40.0)
    check("D1 S1 开仓缓冲不变（bars=1 不触发）",
          SellSignalCode.S1_TREND_BREAK not in detect_sell_signals(
              st_s1, np.array([]), np.array([]), ma_price=3037.0,
              cfg=_cfg(sell_s1_band_atr=0.0)))
    check("D2 S1 解禁不变（bars=2 触发）",
          SellSignalCode.S1_TREND_BREAK in detect_sell_signals(
              _state(bars_in_position=2, current_price=3036.0, atr=40.0),
              np.array([]), np.array([]), ma_price=3037.0, cfg=_cfg(sell_s1_band_atr=0.0)))
    check("D3 S3 止盈不受保护门影响",
          SellSignalCode.S3_TARGET_REACHED in detect_sell_signals(
              _state(bars_in_position=1, pnl_pct=0.20), np.array([]), np.array([]),
              ma_price=None, cfg=_cfg()))
    check("D4 S4 时间止损不受保护门影响",
          SellSignalCode.S4_TIME_STOP in detect_sell_signals(
              _state(bars_in_position=25, pnl_pct=-0.01), np.array([]), np.array([]),
              ma_price=None, cfg=_cfg()))
    check("D5 空仓整体短路",
          detect_sell_signals(_state(position=0.0, bars_in_position=0), rets,
                              np.array([]), ma_price=TRACE["ma_price"], cfg=_cfg()) == [])

    section("E. 客观约束确认（勿当作 bug 修）")
    try:
        from hexbroker.paper.quotes import RealTimeQuoteClient

        bars = RealTimeQuoteClient().fetch_bars("rb0", "1d", 120)
        last_dt = str(bars.index[-1])[:10]
        check("E1 生产 sina 日线仍不含当日（滞后一根是客观约束）",
              last_dt < "2026-09-01", f"末根={last_dt}")
        k = float(bars["adj_close"].iloc[-1] / bars["raw_close"].iloc[-1])
        check("E2 sina 名义自洽：close==raw_close==adj_close（k=1）",
              abs(k - 1.0) < 1e-9, f"k={k}")
    except Exception as exc:
        check("E1/E2 生产行情取证", False, f"{type(exc).__name__}: {exc}")

    section("F. 生产数据未被改动")
    cache = ROOT / "artifacts" / "signals_cache18_grouped_v8_tail_ext.parquet"
    if cache.exists():
        st = cache.stat()
        print(f"  信号缓存: size={st.st_size}  mtime={st.st_mtime_ns}")
        check("F1 生产信号缓存存在且可读", st.st_size > 0)
    else:
        check("F1 生产信号缓存存在且可读", False, str(cache))

    print()
    print("=" * 78)
    print(f"验证结果：{PASS} 项通过 / {FAIL} 项失败")
    print("=" * 78)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
