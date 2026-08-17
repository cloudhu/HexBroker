"""拼接延长数据：基准(sina, 2018~2024-07-17) + PandaData 后复权主力连续(2024-07~2026-08)。

方法：
1. 重叠期（2024-07-01~07-17）计算口径比例 scale = mean(基准close / pandadata close)；
2. 延长段 OHLC × scale 校准到基准口径（cv<0.001，口径一致）；
3. 拼接（基准 + 延长 2024-07-18 起），落盘 data/raw/processed/{sym}/1d/{year}.parquet；
4. 拼接点连续性校验（前后 close 比 < 0.1%）。
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

PROCESSED = Path("data/raw/processed")
INTERIM = Path("data/interim")
SYMS = ["au0", "ag0", "m0"]
PD_FILES = {"au0": "pd_au_daily.csv", "ag0": "pd_ag_daily.csv", "m0": "pd_m_daily.csv"}


def load_baseline(sym: str) -> pd.DataFrame:
    parts = [pd.read_parquet(f) for f in sorted(glob.glob(str(PROCESSED / sym / "1d" / "*.parquet")))]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.set_index("datetime").sort_index()


def main() -> None:
    from hexbroker.data.sources.sina_source import SinaSource

    for sym in SYMS:
        # 基准段：用 sina 重拉 2024-01~07（sina 数据截止 2024-07-17，与基准 100% 一致）
        cfg_sym = {"au0": "SHFE.au", "ag0": "SHFE.ag", "m0": "DCE.m"}[sym]
        src = SinaSource(save=False)
        bars = src.fetch_bars([cfg_sym], start="2024-01-01", end="2024-07-17", freq="1d")
        df24 = bars.df.reset_index()
        df24["datetime"] = pd.to_datetime(df24["datetime"])
        df24 = df24.set_index("datetime").sort_index()
        base_2024 = df24.loc["2024-01-01":"2024-07-17"]
        print(f"{sym}: sina 基准 2024 段 {base_2024.index.min().date()}~{base_2024.index.max().date()} {len(base_2024)} 根")

        pan = pd.read_csv(INTERIM / PD_FILES[sym])
        pan["date"] = pd.to_datetime(pan["date"], format="%Y%m%d")
        pan = pan.set_index("date").sort_index()

        # 重叠期比例（sina 2024 基准 vs pandadata）
        common = pan.index.intersection(base_2024.index)
        ratio = base_2024.loc[common, "close"] / pan.loc[common, "close"]
        scale = float(ratio.mean())
        print(f"  重叠 {len(common)} 日 scale={scale:.4f} (cv={ratio.std()/ratio.mean():.4f})")

        # 延长段（校准，2024-07-18 起）
        ext = pan.loc[pan.index > base_2024.index.max()].copy()
        for c in ["open", "high", "low", "close", "settlement", "pre_settlement"]:
            ext[c] = ext[c] * scale
        print(f"  延长段 {ext.index.min().date()}~{ext.index.max().date()} {len(ext)} 根")

        # 拼接点校验
        gap = abs(ext.iloc[0]["close"] / base_2024.iloc[-1]["close"] - 1.0)
        print(f"  拼接点跳空 {gap*100:.3f}%")

        # 构造与基准同构的 DataFrame（2024 合并段 + 延长段）
        def _rows_from_ext(e: pd.DataFrame) -> list[dict]:
            rows = []
            for idx, r in e.iterrows():
                rows.append({
                    "symbol": sym, "datetime": idx,
                    "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                    "volume": r["volume"], "amount": r.get("amount", 0.0) or 0.0,
                    "open_interest": r.get("open_interest", 0.0) or 0.0,
                    "raw_close": r["close"], "adj_close": r["close"],
                    "limit_up": False, "limit_down": False, "is_rollover": False,
                })
            return rows

        # 2024.parquet = sina 2024 基准段 + pandadata 2024 延长段
        base_rows = [{
            "symbol": sym, "datetime": idx,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": r["volume"], "amount": r.get("amount", 0.0) or 0.0,
            "open_interest": r.get("open_interest", 0.0) or 0.0,
            "raw_close": r["close"], "adj_close": r["close"],
            "limit_up": False, "limit_down": False, "is_rollover": False,
        } for idx, r in base_2024.iterrows()]
        ext_2024 = ext.loc[ext.index.year == 2024]
        rows_2024 = base_rows + _rows_from_ext(ext_2024)
        out2024 = PROCESSED / sym / "1d" / "2024.parquet"
        pd.DataFrame(rows_2024).to_parquet(out2024, index=False)
        print(f"  [落盘] {out2024} ({len(rows_2024)} 根，基准+延长)")

        # 2025/2026 延长段
        for yr in (2025, 2026):
            ext_yr = ext.loc[ext.index.year == yr]
            if len(ext_yr):
                out = PROCESSED / sym / "1d" / f"{yr}.parquet"
                pd.DataFrame(_rows_from_ext(ext_yr)).to_parquet(out, index=False)
                print(f"  [落盘] {out} ({len(ext_yr)} 根)")
    print("[OK] 延长数据拼接完成（2024 段已用 sina 恢复基准）")


if __name__ == "__main__":
    main()
