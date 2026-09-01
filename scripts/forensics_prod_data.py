"""⚠️ 取证统一取数入口（P2-4）——**所有取证脚本必须经此取 bars，禁止直读主湖 parquet**。

之所以放在 ``scripts/``（纳入版本控制）而不是 ``artifacts/_tmp/``（被 .gitignore 整体忽略）：
本模块是**防陷阱基建**。若只留在被忽略的目录，未来的工程师拿不到它，
就会重蹈「直读主湖 → 结论完全反向」的覆辙。

================================================================================
背景（2026-09-01 血泪教训，已写入 hexbroker-signal-diagnostics skill）
================================================================================
生产 bars 的真实来源链
    TradingScheduler._cached_bars
      → RealTimeQuoteClient.fetch_bars(symbol, freq, days)
        → SinaSource（**名义价**，adj/raw 恒等 k≡1.0，**且不含当日 bar**）

主湖 parquet（data/raw/processed/<sym>/1d/<year>.parquet）的 ``close`` 是
**复权价**（k≈1.18~1.20），仅用于研究/回测，**生产根本不读它**。

直读主湖做取证的后果：rb0 2026-09-01 三方比对
    主湖 close(复权)  ma20 = 3651.23   对生产 trace 偏差 +612.63   ← 错
    主湖 raw_close    ma20 = 3044.65   对生产 trace 偏差   +6.05   ← 错
    生产 sina         ma20 = 3038.60   对生产 trace 偏差   +0.00   ← 对

这两条错链直接导致 P0-2「S1 单位错配」与 P0-3「ATR 口径混用」被**错误定性**，
差点把一个本来自洽的名义价链路改坏（引入真正的口径混用）。

铁律：
1. 取 bars 用 ``load_bars()``（生产同源）；
2. 派生量（returns / volumes / ma / atr）用生产实现 ``aux_from_bars()`` / ``atr_from_bars()``，
   **禁止在取证脚本里自带本地副本**（副本会漂移，且会掩盖真实口径）；
3. 每次取证先跑 ``print_bars_banner()``，把「数据源 + 末根日期 + 复权系数」打进结果，
   让结论自带口径自证。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.quotes import RealTimeQuoteClient  # noqa: E402
from hexbroker.paper.scheduler import TradingScheduler  # noqa: E402
from hexbroker.paper.types import Quote  # noqa: E402

__all__ = [
    "load_bars",
    "aux_from_bars",
    "atr_from_bars",
    "bars_health",
    "print_bars_banner",
    "last_bar_date",
]

DEFAULT_ATR_PCT = 0.02  # 与 configs/paper.yaml risk_default_vol 兜底同量级


def load_bars(symbol: str, freq: str = "1d", days: int = 120) -> pd.DataFrame:
    """**生产同源**取 K 线：RealTimeQuoteClient → SinaSource（名义价，不含当日）。

    返回 DataFrame，索引为 datetime，列为 open/high/low/close/volume（+ raw_close/adj_close 若有）。
    """
    return RealTimeQuoteClient().fetch_bars(symbol, freq, days)


def aux_from_bars(bars: pd.DataFrame):
    """returns / volumes / ma_price —— 直接调生产实现 ``TradingScheduler._aux_from_bars``。

    ``self`` 未参与计算，传 None 安全（生产逻辑零副本）。
    """
    return TradingScheduler._aux_from_bars(None, bars)


def atr_from_bars(
    symbol: str, bars: pd.DataFrame, price: float, default_atr_pct: float = DEFAULT_ATR_PCT
) -> float:
    """ATR14 —— 直接调生产实现 ``TradingScheduler._atr_from_bars``。

    生产实现在兜底分支会读 ``self._default_atr_pct``，用 SimpleNamespace 打桩满足它，
    兜底值显式传入（不依赖调度器实例）。
    """
    stub = SimpleNamespace(_default_atr_pct=default_atr_pct)
    quote = Quote(symbol=symbol, ts=datetime.now(), price=price)
    return float(TradingScheduler._atr_from_bars(stub, symbol, bars, quote))


def bars_health(bars: pd.DataFrame) -> dict:
    """口径自检：数据源是否为名义价、末根是否为当日。

    Returns:
        dict: rows / last_date / includes_today / k_min / k_max / k_constant_one
    """
    out: dict = {
        "rows": 0 if bars is None else len(bars),
        "last_date": None,
        "includes_today": False,
        "k_min": None,
        "k_max": None,
        "k_constant_one": False,
    }
    if bars is None or bars.empty or "close" not in bars.columns:
        return out
    idx = pd.to_datetime(bars.index)
    out["last_date"] = str(idx[-1].date())
    out["includes_today"] = idx[-1].date() == datetime.now().date()
    if "raw_close" in bars.columns:
        raw = pd.to_numeric(bars["raw_close"], errors="coerce").replace(0, pd.NA).dropna()
        close = pd.to_numeric(bars["close"], errors="coerce").dropna()
        n = min(len(raw), len(close))
        if n > 0:
            k = close.iloc[-n:].to_numpy(dtype=float) / raw.iloc[-n:].to_numpy(dtype=float)
            out["k_min"] = float(k.min())
            out["k_max"] = float(k.max())
            out["k_constant_one"] = bool(abs(k - 1.0).max() < 1e-9)
    return out


def print_bars_banner(symbol: str, bars: pd.DataFrame, title: str = "口径自检") -> dict:
    """打印并返还口径自检结果——**取证脚本第一行就该调它**。"""
    h = bars_health(bars)
    print("=" * 78)
    print("【%s】symbol=%s 数据源 = RealTimeQuoteClient → SinaSource（生产同源，名义价）" % (
        title, symbol))
    print("  bars 行数 = %d | 末根日期 = %s | 含当日 bar = %s  ← sina 日线恒不含当日（客观约束）"
          % (h["rows"], h["last_date"], h["includes_today"]))
    if h["k_min"] is not None:
        print("  复权系数 k = close/raw_close ∈ [%.6f, %.6f] | k≡1（名义价，未复权）= %s"
              % (h["k_min"], h["k_max"], h["k_constant_one"]))
    else:
        print("  （bars 无 raw_close 列，跳过 k 校验；sina 源 k 恒为 1，close==raw_close==adj_close）")
    print("=" * 78)
    return h


def last_bar_date(bars: pd.DataFrame) -> Optional[str]:
    """末根日期字符串（便于日志比对）。"""
    return bars_health(bars).get("last_date")


if __name__ == "__main__":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for sym in ("rb0", "ag0", "c0"):
        df = load_bars(sym, "1d", 120)
        h = print_bars_banner(sym, df, title="自检")
        if h["rows"]:
            rets, vols, ma = aux_from_bars(df)
            atr = atr_from_bars(sym, df, price=float(df["close"].iloc[-1]))
            print("  ma20 = %s | ATR14 = %s | returns[-1] = %s" % (
                None if ma is None else round(ma, 4),
                round(atr, 4),
                None if rets is None or len(rets) == 0 else round(float(rets[-1]), 6),
            ))
        print()
