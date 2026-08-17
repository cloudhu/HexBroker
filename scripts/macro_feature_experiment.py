"""品种级特征工程实验：为贵金属组补宏观定价特征（t10y 美债10Y=实际利率代理）。

方案：global_codes 从 ['spx','uup'] 扩展至 ['spx','uup','t10y']——
add_cross_global 自动为每品种生成 f_xr_t10y_{ratio,mom,vol}（实际利率比值/动量/波动）。

验证：重建 18 品种信号 → 对比新旧 exp_ret IC（重点看 au0/ag0 是否从负转正）
     → 完整回测对比。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.config import load_config
from hexbroker.data.schema import BarFrame
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.build_signals18 import SYMBOLS18
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import load_local_bars


def run_pipeline(global_codes: list[str], out_cache: str, n_jobs: int = 6):
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS18)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": global_codes}

    bars = load_local_bars(SYMBOLS18)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in global_codes}
    features = build_features(bars, cfg, global_close=gc)
    print(f"[OK] 特征帧 {features.df.shape}（global={global_codes}）")
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None, n_jobs_folds=n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    Path("artifacts").mkdir(exist_ok=True)
    sig.to_parquet(out_cache, index=False)
    print(f"[OK] 信号 {len(sig)} 条 → {out_cache}")
    return sig


def ic_by_symbol(sig: pd.DataFrame) -> pd.DataFrame:
    """各品种 exp_ret 与 realized5 的 RankIC（嵌套评估集内）。"""
    from scipy import stats
    # realized5
    import glob
    closes = {}
    for sym in SYMBOLS18:
        parts = [pd.read_parquet(f) for f in sorted(glob.glob(f"data/raw/processed/{sym}/1d/*.parquet"))]
        df = pd.concat(parts, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"])
        closes[sym] = df.set_index("datetime")["close"].sort_index()
    close_df = pd.DataFrame(closes).sort_index()
    real5 = close_df.shift(-5) / close_df - 1.0
    rows = []
    for sym in SYMBOLS18:
        sub = sig[sig["symbol"] == sym].copy()
        sub["r5"] = sub["ts"].map(lambda t: real5.loc[t, sym] if t in real5.index else np.nan)
        sub = sub.dropna(subset=["r5"])
        if len(sub) >= 100:
            ic = stats.spearmanr(sub["exp_ret"], sub["r5"]).statistic
        else:
            ic = np.nan
        rows.append({"symbol": sym, "n": len(sub), "rankic": float(ic)})
    return pd.DataFrame(rows)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    args = ap.parse_args()

    print("=" * 72)
    print("品种级特征实验：global_codes 扩展 t10y（实际利率）")
    print("=" * 72)

    # 基线（现有 spx+uup）
    sig_base = run_pipeline(["spx", "uup"], "artifacts/signals_cache18_base.parquet", args.n_jobs)
    # 实验（+t10y）
    sig_exp = run_pipeline(["spx", "uup", "t10y"], "artifacts/signals_cache18_t10y_full.parquet", args.n_jobs)

    ic_base = ic_by_symbol(sig_base).set_index("symbol")
    ic_exp = ic_by_symbol(sig_exp).set_index("symbol")
    cmp = pd.DataFrame({"base_ic": ic_base["rankic"], "t10y_ic": ic_exp["rankic"]})
    cmp["delta"] = cmp["t10y_ic"] - cmp["base_ic"]
    print("\n=== 各品种 exp_ret IC 对比（base vs +t10y） ===")
    print(cmp.sort_values("delta", ascending=False).round(4).to_string())
    print(f"\n改善品种数: {(cmp['delta'] > 0).sum()}/{len(cmp)}")
    print(f"贵金属: au0 {cmp.loc['au0','base_ic']:+.4f}→{cmp.loc['au0','t10y_ic']:+.4f} "
          f"| ag0 {cmp.loc['ag0','base_ic']:+.4f}→{cmp.loc['ag0','t10y_ic']:+.4f}")


if __name__ == "__main__":
    main()
