#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P22 QA 零泄漏独立检查（扰动测试，单品种 rb0）。

复刻尾折方法（不调 p22_tail_ext.py 函数，直接使用共享流水线函数）：
  1. 构建特征（数据端 08-21，同 p22 口径）
  2. 末折训练（≤ train_max_pos）+ 校准（末折测试窗前 50%）
  3. 追加窗口预测（test_end-lookback+1 起）
  4. 扰动测试：把 2026-07-15 之后（未来）的特征全部置乱 →
     重新预测 → 验证 ts < 2026-07-15 的所有预测逐字节不变
     （未来信息不得影响过去预测）。

输出真实数字供 QA 报告贴出。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.config import load_config
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.feature import build_features
from hexbroker.forecast.base import build_windows
from hexbroker.forecast.baselines import LightGBMForecast
from hexbroker.forecast.calibration import calibrate_signals
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.group_modeling import LOCAL_MAP, load_fundamental_data
from scripts.refine_lightgbm_champion import (
    DATA_START, FREQ, _apply_calibrator_one, _build_fwd_cs_panel, _forward_returns,
)
from scripts.sentinel_phase4_evo import load_local_bars

GROUP = {"syms": ["rb0"], "global": ["spx", "t10y"]}  # ferrous_steel 组（GROUPS_V2）
SYM = "rb0"
PERTURB_AFTER = pd.Timestamp("2026-07-15")
WF = dict(train_len=250, test_len=60, purge=5, embargo=2, mode="rolling")


def setup_cfg():
    cfg = load_config("configs/base.yaml")
    cfg.data.symbols = [LOCAL_MAP[s] for s in GROUP["syms"]]
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-21"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = [
        "technical", "microstructure", "iterative", "cross", "normalize", "fundamental",
    ]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GROUP["global"]}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}
    for k, v in load_best_params().items():
        setattr(cfg.forecast, k, v)
    return cfg


def build_data(cfg):
    bars = load_local_bars([LOCAL_MAP[s] for s in GROUP["syms"]])
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GROUP["global"]}
    fund = load_fundamental_data([LOCAL_MAP[s] for s in GROUP["syms"]])
    features = build_features(bars, cfg, global_close=gc, fundamental_data=fund)
    return bars, features


def train_tail(cfg, features, bars):
    feat = features.df.loc[[SYM]].sort_index()
    sym_df = bars.by_symbol(SYM)
    close = sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
    n = len(feat)
    splitter = WalkForwardSplitter(**WF)
    folds = splitter.split(feat.index)
    fold = folds[-1]
    horizon = int(cfg.forecast.horizon)
    lookback = min(int(cfg.feature.normalize_window), 30)
    fwd = _forward_returns(close, horizon)
    fwd_panel = _build_fwd_cs_panel(features, bars, horizon, "cross_z", "all")

    t_max = fold.train_max_pos - horizon
    train_feat = feat.iloc[0 : t_max + 1]
    windows, valid_idx = build_windows(train_feat, lookback)
    y = fwd_panel[SYM].loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)

    model = LightGBMForecast(cfg, model_id=f"wf-tail-{SYM}")
    model.fit(train_feat, y)

    # 校准器：末折测试窗前 50%（与 v8 一致）
    test_feat = feat.iloc[fold.test_start : fold.test_end]
    sigs = model.predict(test_feat)
    test_ts = [s.ts for s in sigs]
    realized = fwd.loc[test_ts].to_numpy(dtype=float)
    y_true = (realized > 0).astype(float)
    valid = ~np.isnan(realized)
    k = int(len(sigs) * 0.5)
    k = max(1, min(k, len(sigs) - 1))
    cal_valid = [i for i in range(k) if valid[i]]
    scaler = None
    if len(cal_valid) >= 20:
        scaler = calibrate_signals([sigs[i] for i in cal_valid], y_true[cal_valid], "platt")
    if scaler is not None:
        for s in sigs[k:]:
            _apply_calibrator_one(s, scaler, "platt")
    return model, scaler, feat, fold, lookback


def predict_tail(model, scaler, feat, fold, lookback):
    tail_input = feat.iloc[fold.test_end - lookback + 1 :]
    sigs_tail = model.predict(tail_input)
    recs = []
    for s in sigs_tail:
        if scaler is not None:
            _apply_calibrator_one(s, scaler, "platt")
        recs.append(dict(symbol=SYM, ts=pd.Timestamp(s.ts), p_up=float(s.p_up),
                         exp_ret=float(s.exp_ret), is_effective=bool(s.is_effective)))
    return pd.DataFrame(recs)


def main():
    cfg = setup_cfg()
    bars, features = build_data(cfg)
    model, scaler, feat, fold, lookback = train_tail(cfg, features, bars)

    base = predict_tail(model, scaler, feat, fold, lookback)
    base = base.set_index("ts").sort_index()
    print(f"[{SYM}] 尾折 fold_end={pd.Timestamp(feat.index[fold.test_end-1][1]).date()} "
          f"| tail {len(base)} 行 {base.index.min().date()}~{base.index.max().date()}")

    # ---- 扰动：PERTURB_AFTER 之后的特征全部置乱 ----
    feat_p = feat.copy()
    num_cols = feat_p.select_dtypes(include=[np.number]).columns
    mask = pd.to_datetime(feat_p.index.get_level_values(1)) >= PERTURB_AFTER
    rng = np.random.default_rng(12345)
    feat_p.loc[mask, num_cols] = rng.standard_normal((int(mask.sum()), len(num_cols))) * 1e3

    perturbed = predict_tail(model, scaler, feat_p, fold, lookback)
    perturbed = perturbed.set_index("ts").sort_index()

    common = base.index.intersection(perturbed.index)
    before = common[common < PERTURB_AFTER]
    after = common[common >= PERTURB_AFTER]
    d_before_p = np.max(np.abs(base.loc[before, "p_up"].to_numpy() - perturbed.loc[before, "p_up"].to_numpy()))
    d_before_e = np.max(np.abs(base.loc[before, "exp_ret"].to_numpy() - perturbed.loc[before, "exp_ret"].to_numpy()))
    eff_before = bool((base.loc[before, "is_effective"].to_numpy() == perturbed.loc[before, "is_effective"].to_numpy()).all())
    d_after_p = float(np.max(np.abs(base.loc[after, "p_up"].to_numpy() - perturbed.loc[after, "p_up"].to_numpy())))
    print(f"\n扰动点: {PERTURB_AFTER.date()}（之后特征置乱）")
    print(f"  ts < {PERTURB_AFTER.date()}（应逐字节不变）: {len(before)} 行 | "
          f"p_up max|diff|={d_before_p:.3e} | exp_ret max|diff|={d_before_e:.3e} | is_effective 全等={eff_before}")
    print(f"  ts >= {PERTURB_AFTER.date()}（允许改变）: {len(after)} 行 | p_up max|diff|={d_after_p:.3e}")
    ok = (d_before_p == 0.0) and (d_before_e == 0.0) and eff_before
    print(f"\n  ==> 零泄漏（未来扰动不影响过去预测）: {ok}")

    # ---- 与 tail_ext 缓存对照（rb0 尾行逐字节一致？） ----
    te = pd.read_parquet(ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet")
    te["ts"] = pd.to_datetime(te["ts"])
    te_rb = te[te["symbol"] == SYM].set_index("ts").sort_index()
    mine = base
    common2 = mine.index.intersection(te_rb.index)
    d_p = np.max(np.abs(mine.loc[common2, "p_up"].to_numpy() - te_rb.loc[common2, "p_up"].to_numpy()))
    d_e = np.max(np.abs(mine.loc[common2, "exp_ret"].to_numpy() - te_rb.loc[common2, "exp_ret"].to_numpy()))
    print(f"\n独立复现 rb0 尾行 vs tail_ext 缓存：共享 {len(common2)} 行 | "
          f"p_up max|diff|={d_p:.3e} | exp_ret max|diff|={d_e:.3e} | 一致={d_p==0.0 and d_e==0.0}")


if __name__ == "__main__":
    main()
