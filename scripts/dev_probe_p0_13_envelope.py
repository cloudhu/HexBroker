"""P0-13 探针：hc0/ni0 OHLC 包络校验失败定位（只读，零写入）。

复现 ``HexDataError: 存在 OHLC 包络关系被破坏的 bar``：直调
``ak.futures_main_sina``（与 ``AkshareSource`` 同路径），逐 bar 检查
``high >= max(open, close)`` 且 ``low <= min(open, close)``，打印违规
bar 的完整 OHLC，判定根因类别：
- 源数据异常（个别日期 high/low 缺失/为 0/为 NaN）
- 解析 bug（列错位 / 结算价混入 / 单位不一致）
"""
from __future__ import annotations

import sys
from pathlib import Path

import akshare as ak
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data.sources.akshare_source import AK_COLUMN_MAP  # noqa: E402

SYMS = {"hc0": "HC0", "ni0": "NI0"}


def main() -> None:
    for sym, code in SYMS.items():
        print(f"== {sym} ({code}) ==")
        raw = ak.futures_main_sina(symbol=code)
        df = raw.rename(columns=AK_COLUMN_MAP)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        bad = df[(h < pd.concat([o, c], axis=1).max(axis=1))
                 | (l > pd.concat([o, c], axis=1).min(axis=1))]
        print(f"  全历史 {len(df)} bars（{df.index.min().date()} ~ {df.index.max().date()}），"
              f"违规 {len(bad)} 根")
        for dt, row in bad.head(15).iterrows():
            print(f"    {dt.date()}  O={row['open']} H={row['high']} "
                  f"L={row['low']} C={row['close']} "
                  f"settle={row.get('settlement', '-')}")
        if len(bad) > 15:
            print(f"    ... 共 {len(bad)} 根")
        if len(bad):
            yrs = sorted({d.year for d in bad.index})
            print(f"  违规年份: {yrs}")
            # NaN / 0 值统计
            for col in ("open", "high", "low", "close"):
                n_nan = df[col].isna().sum()
                n_zero = (pd.to_numeric(df[col], errors="coerce") == 0).sum()
                print(f"  {col}: NaN={n_nan}, 0值={n_zero}")


if __name__ == "__main__":
    main()
