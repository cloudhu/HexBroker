"""预测层训练脚本（§3.2 / §8.4）。

用法示例
--------
    python -m scripts.run_forecast --demo
    python -m scripts.run_forecast --source csv --symbols SHFE.cu SHFE.rb
    python -m scripts.run_forecast --model lightgbm --store-dir artifacts/signals

流程：数据 → 特征 → walk-forward 训练 → OOS 信号落盘 SignalStore。
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from hexbroker.config import load_config
from hexbroker.data.sources.csv_source import CsvSource
from hexbroker.data.sources.synthetic_source import SyntheticSource
from hexbroker.feature import build_features
from hexbroker.forecast.signal_store import SignalStore
from hexbroker.forecast.trainer import ForecastTrainer
from hexbroker.utils.logging import init_logging


def _build_source(cfg, args):
    src = args.source or cfg.data.source
    if src == "synthetic":
        return SyntheticSource(
            n_bars=getattr(args, "n_bars", 800),
            seed=int(cfg.seed),
        )
    if src == "csv":
        return CsvSource()
    raise ValueError(f"未知数据源：{src}")


def main() -> None:
    ap = argparse.ArgumentParser(description="HexFutures-AI 预测层训练")
    ap.add_argument("--config", type=str, default=None, help="实验 yaml 配置")
    ap.add_argument("--demo", action="store_true", help="使用合成数据 + ARTransformer fallback")
    ap.add_argument("--source", type=str, default=None, help="csv / synthetic")
    ap.add_argument("--symbols", nargs="*", default=None, help="标的列表")
    ap.add_argument("--model", type=str, default=None, help="ar_transformer/tcn/gru/lightgbm/kronos")
    ap.add_argument("--store-dir", type=str, default=None, help="SignalStore 落盘目录")
    ap.add_argument("--n-bars", type=int, default=800)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.demo:
        cfg.data.source = "synthetic"
        args.model = args.model or "ar_transformer"
    if args.symbols:
        cfg.data.symbols = list(args.symbols)
    model_name = args.model or cfg.forecast.name

    init_logging("forecast", Path("artifacts"))
    src = _build_source(cfg, args)
    bars = src.fetch_bars(list(cfg.data.symbols), start=cfg.data.start, end=cfg.data.end, freq=cfg.data.freq)

    features = build_features(bars, cfg)

    store_dir = args.store_dir or tempfile.mkdtemp(prefix="signals_")
    store = SignalStore(store_dir)
    trainer = ForecastTrainer(cfg, store, model_name=model_name)
    result = trainer.run(bars, features)

    print("=" * 60)
    print(f"预测训练完成")
    print(f"  模型        : {model_name}")
    print(f"  model_id    : {result.model_id}")
    print(f"  walk-forward: {result.n_folds} 折")
    print(f"  OOS 信号数 : {result.n_oos_signals}")
    print(f"  SignalStore: {store_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
