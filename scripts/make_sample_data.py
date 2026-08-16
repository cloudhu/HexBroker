"""生成离线样例数据（确定性合成），写入 ``data/sample/{symbol}.csv``。

用于 ``--source csv`` 路径离线开发，无需任何外部数据源。
品种：SHFE.cu / SHFE.rb / INE.sc，约 800 根日线。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data.sources.synthetic_source import SyntheticSource

SYMBOLS = ["SHFE.cu", "SHFE.rb", "INE.sc"]
OUT_DIR = Path("data/sample")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = SyntheticSource(n_bars=800, seed=42, rollover_every=200)
    bf = src.fetch_bars(SYMBOLS, "2018-01-01", "2024-12-31", "1d")
    for sym in SYMBOLS:
        grp = bf.by_symbol(sym).reset_index()
        grp["date"] = grp["datetime"].dt.strftime("%Y-%m-%d")
        grp = grp[
            ["date", "open", "high", "low", "close", "volume", "amount", "open_interest"]
        ]
        grp.to_csv(OUT_DIR / f"{sym}.csv", index=False)
        print(f"已写入 {OUT_DIR / (sym + '.csv')} ({len(grp)} 行)")


if __name__ == "__main__":
    main()
