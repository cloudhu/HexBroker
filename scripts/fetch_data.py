"""拉取并预处理数据，落盘到本地数据湖。

示例：
    python -m scripts.fetch_data --config configs/experiment/e01_cu_daily.yaml --source csv
    python -m scripts.fetch_data --source synthetic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.config import load_config
from hexbroker.data.cleaner import Cleaner
from hexbroker.data.contract import ContractStitcher
from hexbroker.data.sources.csv_source import CsvSource
from hexbroker.data.sources.synthetic_source import SyntheticSource
from hexbroker.data.store import DataLake
from hexbroker.utils.logging import get_logger

_log = get_logger("DATA")


def main() -> None:
    ap = argparse.ArgumentParser(description="HexFutures-AI 数据拉取")
    ap.add_argument("--config", default="configs/experiment/e01_cu_daily.yaml")
    ap.add_argument("--source", default="csv", choices=["csv", "synthetic", "tqsdk", "akshare"])
    args = ap.parse_args()

    cfg = load_config(args.config)
    _log.info(f"加载配置 experiment={cfg.experiment}, source={args.source}")

    if args.source == "csv":
        src = CsvSource()
    elif args.source == "synthetic":
        src = SyntheticSource(n_bars=800, seed=cfg.seed)
    elif args.source == "tqsdk":
        from hexbroker.data.sources.tqsdk_source import TqsdkSource

        src = TqsdkSource()
    elif args.source == "akshare":
        from hexbroker.data.sources.akshare_source import AkshareSource

        src = AkshareSource()
    else:
        raise SystemExit(f"未知 source: {args.source}")

    bars = src.fetch_bars(cfg.data.symbols, cfg.data.start, cfg.data.end, cfg.data.freq)
    _log.info(f"原始数据 {bars.length} 行，品种 {bars.symbols}")

    stitched = ContractStitcher(cfg.data.main_rule, cfg.data.adjust_method).stitch(bars)
    cleaned = Cleaner().clean(stitched)

    lake = DataLake("data")
    lake.save_processed(cleaned)
    _log.info(f"已落盘到 {lake.root}/processed，共 {cleaned.length} 行")


if __name__ == "__main__":
    main()
