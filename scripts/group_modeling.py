"""分组建模：按品种组单独训练 LightGBM + 特征-品种白名单。

分组（基于归因：全球定价品种挂宏观特征，国内定价品种少挂）：
  precious  贵金属 au/ag       global=['spx','uup','t10y']  全宏观（实际利率/美元/风险偏好）
  ferrous   黑色   rb/i/hc/j/jm global=['spx','t10y']       部分宏观（黑色受国内需求+宏观）
  industrial有色   cu/al/zn/ni  global=['spx','uup']        全球定价
  agri      农产品 m/y/p/sr/cf  global=['spx']              少宏观（避免 ta/cf 被污染教训）
  chem_energy化工 ta/sc         global=['spx']              少宏观

每组建模 → 拼接 exp_ret → 全局截面排序 top30% → 完整回测对比基线。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from hexbroker.config import load_config
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.build_signals18 import SYMBOLS18
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import load_local_bars

# 分组定义（本地短名）
GROUPS = {
    "precious": {"syms": ["au0", "ag0"], "global": ["spx", "uup", "t10y"]},
    "ferrous": {"syms": ["rb0", "i0", "hc0", "j0", "jm0"], "global": ["spx", "t10y"]},
    "industrial": {"syms": ["cu0", "al0", "zn0", "ni0"], "global": ["spx", "uup"]},
    "agri": {"syms": ["m0", "y0", "p0", "sr0", "cf0"], "global": ["spx"]},
    "chem_energy": {"syms": ["ta0", "sc0"], "global": ["spx"]},
}
# 短名 → 标准名映射（load_local_bars 需要）
LOCAL_MAP = {
    "au0": "SHFE.au", "ag0": "SHFE.ag", "m0": "DCE.m", "cu0": "SHFE.cu",
    "rb0": "SHFE.rb", "i0": "DCE.i", "al0": "SHFE.al", "zn0": "SHFE.zn",
    "ni0": "SHFE.ni", "hc0": "SHFE.hc", "y0": "DCE.y", "p0": "DCE.p",
    "j0": "DCE.j", "jm0": "DCE.jm", "sr0": "CZCE.sr", "cf0": "CZCE.cf",
    "ta0": "CZCE.ta", "sc0": "INE.sc",
}


def build_group_signals(group_syms: list[str], global_codes: list[str],
                        n_jobs: int) -> pd.DataFrame:
    """单组训练：bars/features 只含组内品种，global_codes 白名单化。"""
    cfg = load_config()
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    cfg.data.symbols = std_syms
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": global_codes}

    bars = load_local_bars(std_syms)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in global_codes}
    features = build_features(bars, cfg, global_close=gc)
    wf = walk_forward_lightgbm(
        cfg, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None, n_jobs_folds=n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] {group_syms}: {len(sig)} 条信号 (global={global_codes})")
    return sig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔），空=全部")
    args = ap.parse_args()

    print("=" * 72)
    print("分组建模（组内训练 + 特征白名单）")
    print("=" * 72)
    only = set(args.only.split(",")) if args.only else None

    frames = []
    for gname, gcfg in GROUPS.items():
        if only is not None and gname not in only:
            continue
        sig = build_group_signals(gcfg["syms"], gcfg["global"], args.n_jobs)
        frames.append(sig)
    if not frames:
        print("[FAIL] 无信号产出")
        return
    all_sig = pd.concat(frames, ignore_index=True)
    Path("artifacts").mkdir(exist_ok=True)
    all_sig.to_parquet("artifacts/signals_cache18_grouped.parquet", index=False)
    print(f"[OK] 分组信号合并 {len(all_sig)} 条 → artifacts/signals_cache18_grouped.parquet")
    print(f"  品种覆盖: {sorted(all_sig['symbol'].unique())}")


if __name__ == "__main__":
    main()
