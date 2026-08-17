"""18 品种信号构建 + 信号缓存重建（品种池扩展 6→18）。

新增 12 品种（al/zn/ni/hc/y/p/sr/cf/j/jm/sc/ta，PandaData close_pcr 全历史）。
合约参数：
  al0 ×5 tick5 | zn0 ×5 tick5 | ni0 ×1 tick10 | hc0 ×10 tick1
  y0 ×10 tick2 | p0 ×10 tick2 | j0 ×100 tick0.5 | jm0 ×60 tick0.5
  sr0 ×10 tick1 | cf0 ×5 tick5 | ta0 ×10 tick1 | sc0 ×1000 tick0.1
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

CONTRACTS18 = {
    **{"au0": {"multiplier": 1000.0, "min_tick": 0.02},
       "ag0": {"multiplier": 15.0, "min_tick": 0.01},
       "m0": {"multiplier": 10.0, "min_tick": 1.0},
       "cu0": {"multiplier": 5.0, "min_tick": 10.0},
       "rb0": {"multiplier": 10.0, "min_tick": 1.0},
       "i0": {"multiplier": 100.0, "min_tick": 0.5}},
    **{"al0": {"multiplier": 5.0, "min_tick": 5.0},
       "zn0": {"multiplier": 5.0, "min_tick": 5.0},
       "ni0": {"multiplier": 1.0, "min_tick": 10.0},
       "hc0": {"multiplier": 10.0, "min_tick": 1.0},
       "y0": {"multiplier": 10.0, "min_tick": 2.0},
       "p0": {"multiplier": 10.0, "min_tick": 2.0},
       "j0": {"multiplier": 100.0, "min_tick": 0.5},
       "jm0": {"multiplier": 60.0, "min_tick": 0.5},
       "sr0": {"multiplier": 10.0, "min_tick": 1.0},
       "cf0": {"multiplier": 5.0, "min_tick": 5.0},
       "ta0": {"multiplier": 10.0, "min_tick": 1.0},
       "sc0": {"multiplier": 1000.0, "min_tick": 0.1}},
}

SYMBOLS18 = [
    "au0", "ag0", "m0", "cu0", "rb0", "i0",
    "al0", "zn0", "ni0", "hc0", "y0", "p0", "j0", "jm0", "sr0", "cf0", "ta0", "sc0",
]


def load_bars18() -> pd.DataFrame:
    """加载 18 品种本地 parquet → MultiIndex(symbol, datetime) df。"""
    import glob
    parts = []
    for sym in SYMBOLS18:
        for fp in sorted(glob.glob(f"data/raw/processed/{sym}/1d/*.parquet")):
            parts.append(pd.read_parquet(fp))
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index(["symbol", "datetime"]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def main() -> None:
    from hexbroker.config import load_config
    from hexbroker.data.schema import BarFrame
    from hexbroker.feature import build_features
    from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
    from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
    from scripts.sentinel_phase4_evo import load_local_bars

    cfg = load_config()
    cfg.data.symbols = SYMBOLS18
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": ["spx", "uup"]}

    bars = load_local_bars(SYMBOLS18)
    print(f"[OK] bars: {len(bars.df)} 行, {len(bars.symbols)} 品种")
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in ["spx", "uup"]}
    features = build_features(bars, cfg, global_close=gc)
    print(f"[OK] features: {features.df.shape}")
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None, n_jobs_folds=6, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    Path("artifacts").mkdir(exist_ok=True)
    sig.to_parquet("artifacts/signals_cache18.parquet", index=False)
    print(f"[OK] 18 品种信号 {len(sig)} 条已缓存 artifacts/signals_cache18.parquet")
    print(f"  品种分布: {sig['symbol'].value_counts().to_dict()}")


if __name__ == "__main__":
    main()
