"""反思诊断：OOS 段 Q0 空头侧真实结构分解。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from hexbroker.config import load_config
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import SYMBOLS6, load_local_bars


def main() -> None:
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS6)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": ["spx", "uup"]}
    bars = load_local_bars(SYMBOLS6)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in ["spx", "uup"]}
    features = build_features(bars, cfg, global_close=gc)
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None, n_jobs_folds=1, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])

    # 向量化 5 日实际收益
    closes = {}
    for sym in bars.symbols:
        closes[sym] = bars.by_symbol(sym)["close"].astype(float)
    close_df = pd.DataFrame(closes).sort_index()
    real_df = close_df.shift(-5) / close_df - 1.0

    rows = []
    for _, r in sig.iterrows():
        if r["ts"] in real_df.index and r["symbol"] in real_df.columns:
            v = real_df.loc[r["ts"], r["symbol"]]
            if pd.notna(v):
                rows.append((r["symbol"], r["ts"], r["exp_ret"], float(v)))
    df = pd.DataFrame(rows, columns=["symbol", "ts", "exp_ret", "real5"])
    oos = df[df["ts"] >= pd.Timestamp("2024-07-18")].copy()
    print(f"OOS 段 {len(oos)} 信号（{oos['ts'].min().date()}~{oos['ts'].max().date()}）")
    oos["rank_pct"] = oos["exp_ret"].rank(pct=True)
    oos["q"] = pd.cut(oos["rank_pct"], [0, 0.2, 0.4, 0.6, 0.8, 1.0], labels=[0, 1, 2, 3, 4]).astype(int)
    print("分位桶实际 5 日收益：")
    for q, sub in oos.groupby("q"):
        print(f"  Q{q}: n={len(sub):3d} 实际={sub['real5'].mean()*100:+.3f}% 上涨率={(sub['real5']>0).mean()*100:.0f}%")
    m = oos.groupby("q")["real5"].mean()
    print(f"Q4-Q0 价差={m.iloc[-1]*100-m.iloc[0]*100:+.3f}% | Q0(做空侧)={m.iloc[0]*100:+.3f}% → 做空 Q0 期望 {-m.iloc[0]*100:.3f}%/5日")
    print()
    print("各品种 OOS 段 base rate：")
    for sym, sub in oos.groupby("symbol"):
        print(f"  {sym}: n={len(sub):3d} 平均={sub['real5'].mean()*100:+.3f}% 上涨率={(sub['real5']>0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
