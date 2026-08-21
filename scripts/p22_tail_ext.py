#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P22-1 尾折扩展研究实验（研究性变体，**非 v8 生产口径**）。

背景
----
v8 信号缓存（artifacts/signals_cache18_grouped_v8.parquet）止于 2026-06-29：
walk_forward 60 日步长 fold 网格的**末信号日 = 最后完整折边界**，新增数据需
累计 ~20-24 交易日才能跨过下一折边界 → 引擎 A 在 2026-07/08 月无信号
（a_status=cache_end）。P21 登记 P22 待办②：提前评估「引擎 A 恢复 07/08 月
信号后是否改善」——即本脚本的**尾折扩展**。

尾折扩展定义（研究用，工程可落地方案）
-------------------------------------
对每品种 **最后一个 fold**：
  1. 训练窗不变：用与 v8 完全相同的训练数据（walk_forward 网格
     train_len=250 / test_len=60 / purge=5 / embargo=2 / mode=rolling；
     label_mode=cross_z / label_pool=all / cal_split=0.5 / cal_return_all=False；
     champion HP）训练最后一折模型 —— **与 v8 末折模型逐字节一致**（脚本内校验）。
  2. 仅评估窗延长：用该模型对 [末折 test_end, 数据末端] 追加窗口出信号；
     输入特征从 test_end-lookback+1 起（因果：day t 只用 t-29..t 特征，全部
     ≤ 数据末端 2026-08-21，且模型训练数据远早于 test_end）。
  3. 校准：沿用末折已拟合的 Platt 校准器（拟合数据 ≤ 末折测试窗前 50%，
     远早于追加窗口 → 嵌套零泄漏），对追加信号应用同一校准器。
  4. 输出缓存 = **v8 原样 + 追加尾信号**（不覆盖 v8）：
     artifacts/signals_cache18_grouped_v8_tail_ext.parquet
     —— 共享窗口（≤06-29）与 v8 逐字节一致，仅在尾部新增 07/08 月信号。

⚠️ 明确标注：**这不是 v8 生产口径**（v8 折叠网格固定；延长窗是研究变体）。
  产出缓存以 _tail_ext 命名，绝不覆盖 v8。评估结论仅供决策参考，若显著优于
  v8 需 QA 复核 + 主理人终裁后才可升级生产。

Stage A  覆盖率对比 v8 vs tail_ext → artifacts/p22_tail_ext_coverage.csv
Stage B  引擎 A 单引擎 S2 + 组合 A30/B70 OOS（生产 cap 口径，同窗口）
         → artifacts/p22_tail_ext_engineA.csv

口径铁律：复利口径；OOS 2024-07-18 后；修复后 broker（完整回测
滑点1tick+费0.005%+保证金12%+CONTRACTS18）；生产 cap 口径（top_k=0.30 /
min=3 / cap=0.5 / group_map(base.yaml)）；嵌套零泄漏（校准器仅用末折测试窗
前 50% 拟合，评估窗不重叠）。

用法
----
  python scripts/p22_tail_ext.py                 # 全量（8 组）
  python scripts/p22_tail_ext.py --only precious # 单组冒烟/续跑
  python scripts/p22_tail_ext.py --skip-train    # 仅合并已有 checkpoint + 评估
  python scripts/p22_tail_ext.py --skip-eval     # 仅重建缓存
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.feature import build_features
from hexbroker.forecast.base import build_windows
from hexbroker.forecast.baselines import LightGBMForecast
from hexbroker.forecast.calibration import calibrate_signals
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.build_signals18 import CONTRACTS18
from scripts.group_modeling import LOCAL_MAP, load_fundamental_data
from scripts.group_modeling_v2 import GROUPS_V2
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import engine_b_targets
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
    seg_sharpe,
)
from scripts.p21_3_eval import OOS_SUB_BOUNDS, coverage_stats
from scripts.refine_lightgbm_champion import (
    DATA_START,
    FREQ,
    _apply_calibrator_one,
    _build_fwd_cs_panel,
    _forward_returns,
)
from scripts.sentinel_phase4_evo import load_local_bars

ART = ROOT / "artifacts"
CKPT_DIR = ART / "p22_tail_checkpoints"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
TAIL_EXT_PATH = ART / "signals_cache18_grouped_v8_tail_ext.parquet"
COV_CSV = ART / "p22_tail_ext_coverage.csv"
EVAL_CSV = ART / "p22_tail_ext_engineA.csv"
VERDICT_JSON = ART / "p22_tail_ext_verdict.json"

OOS_START = "2024-07-18"
TOP_K = 0.30
MIN_SYMBOLS_S2 = 3
CAL_MIN_SAMPLES = 20  # 与 v8 生产默认一致

# walk_forward 网格（configs/base.yaml 默认，与 v8 构建一致）
WF_SPLITTER = dict(train_len=250, test_len=60, purge=5, embargo=2, mode="rolling")


def _setup_cfg(group_syms: list[str], global_codes: list[str]):
    """复刻 build_group_signals 的 cfg（数据端 2026-08-21），并应用 champion HP。

    cfg.data.end 在特征流水线中不参与切片（FeaturePipeline 处理全部 bars），
    显式写 2026-08-21 仅作语义标注；HP 应用方式与 walk_forward_lightgbm 相同。
    """
    cfg = load_config("configs/base.yaml")
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    cfg.data.symbols = std_syms
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-21"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    # P8-2：信号生产显式启用 fundamental（基差）特征；其余保持 v2/v4/v8 口径
    cfg.feature.transformers = [
        "technical", "microstructure", "iterative", "cross", "normalize", "fundamental",
    ]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": global_codes}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}
    for k, v in load_best_params().items():
        setattr(cfg.forecast, k, v)
    return cfg


def _build_group_data(group_syms: list[str], global_codes: list[str]):
    """组级数据构建（bars / 全局对齐 / 基本面 / 特征），与 build_group_signals 同源。"""
    cfg = _setup_cfg(group_syms, global_codes)
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    bars = load_local_bars(std_syms)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in global_codes}
    fund = load_fundamental_data(std_syms)
    features = build_features(bars, cfg, global_close=gc, fundamental_data=fund)
    return cfg, bars, features


def _tail_ext_for_symbol(
    sym: str,
    cfg,
    bars,
    features,
    v8_sig: pd.DataFrame,
    fwd_cs_panel: pd.DataFrame | None,
) -> tuple[list[dict], dict]:
    """单品种：末折训练（校验 v8 逐字节一致）→ 追加尾信号。

    返回 (tail_records, info)。
    """
    info: dict = {"symbol": sym, "verified_vs_v8": False, "max_diff": float("nan")}
    feat = features.df.loc[[sym]].sort_index()
    sym_df = bars.by_symbol(sym)
    close = sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
    n = len(feat)
    if n < 10:
        return [], info
    splitter = WalkForwardSplitter(**WF_SPLITTER)
    folds = splitter.split(feat.index)
    if not folds:
        return [], info
    fold = folds[-1]
    info["n_folds"] = len(folds)
    info["fold_test_start"] = str(pd.Timestamp(feat.index[fold.test_start][1]).date())
    info["fold_test_end_excl"] = str(pd.Timestamp(feat.index[fold.test_end - 1][1]).date())

    horizon = int(cfg.forecast.horizon)
    lookback = min(int(cfg.feature.normalize_window), 30)
    fwd = _forward_returns(close, horizon)

    # ---- 末折训练（与 v8 walk_forward_lightgbm 逐字节一致） ----
    t_max = fold.train_max_pos - horizon
    if t_max < lookback:
        return [], info
    train_feat = feat.iloc[0 : t_max + 1]
    windows, valid_idx = build_windows(train_feat, lookback)
    if windows.shape[0] == 0:
        return [], info
    if fwd_cs_panel is not None:
        y = fwd_cs_panel[sym].loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)
    else:
        y = fwd.loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)
    if np.isnan(y).any() or y.shape[0] < 20:
        return [], info

    model = LightGBMForecast(cfg, model_id=f"wf-tail-{sym}")
    model.fit(train_feat, y)

    # ---- 复现末折测试窗信号（校验 = v8 该折后半） ----
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
    if len(cal_valid) >= CAL_MIN_SAMPLES:
        scaler = calibrate_signals([sigs[i] for i in cal_valid], y_true[cal_valid], "platt")
    if scaler is not None:
        for s in sigs[k:]:
            _apply_calibrator_one(s, scaler, "platt")
    fold_recs: list[dict] = []
    for s in sigs[k:]:
        fold_recs.append(dict(
            symbol=sym, ts=pd.Timestamp(s.ts), p_up=float(s.p_up),
            exp_ret=float(s.exp_ret), is_effective=bool(s.is_effective),
        ))
    # 与 v8 缓存对比（同折同日期）
    v8_sub = v8_sig[(v8_sig["symbol"] == sym)]
    fold_dates = pd.DatetimeIndex([r["ts"] for r in fold_recs])
    v8_sub = v8_sub[v8_sub["ts"].isin(fold_dates)].set_index("ts").sort_index()
    if len(v8_sub) == len(fold_recs) and len(fold_recs) > 0:
        mine = pd.DataFrame(fold_recs).set_index("ts").sort_index()
        d_p = float(np.max(np.abs(mine["p_up"].to_numpy() - v8_sub["p_up"].to_numpy())))
        d_e = float(np.max(np.abs(mine["exp_ret"].to_numpy() - v8_sub["exp_ret"].to_numpy())))
        eff_ok = bool((mine["is_effective"].to_numpy() == v8_sub["is_effective"].to_numpy()).all())
        info["verified_vs_v8"] = bool(d_p < 1e-9 and d_e < 1e-9 and eff_ok and len(mine) == len(v8_sub))
        info["max_diff"] = max(d_p, d_e)
        info["n_fold_recs"] = len(fold_recs)
    else:
        info["n_fold_recs"] = len(fold_recs)
        info["note_mismatch"] = f"v8 匹配行 {len(v8_sub)} != 复现行 {len(fold_recs)}"

    # ---- 追加尾信号：输入从 test_end-lookback+1 起（因果），窗口末端覆盖 [test_end, n) ----
    if fold.test_end >= n:
        return [], info
    tail_input = feat.iloc[fold.test_end - lookback + 1 :]
    sigs_tail = model.predict(tail_input)
    tail_recs: list[dict] = []
    for s in sigs_tail:
        if scaler is not None:
            _apply_calibrator_one(s, scaler, "platt")
        tail_recs.append(dict(
            symbol=sym, ts=pd.Timestamp(s.ts), p_up=float(s.p_up),
            exp_ret=float(s.exp_ret), is_effective=bool(s.is_effective),
        ))
    info["n_tail_recs"] = len(tail_recs)
    if tail_recs:
        info["tail_first"] = str(pd.Timestamp(tail_recs[0]["ts"]).date())
        info["tail_last"] = str(pd.Timestamp(tail_recs[-1]["ts"]).date())
    return tail_recs, info


def build_tail_ext_for_group(group_name: str, gcfg: dict) -> tuple[list[dict], dict, Path]:
    """单组尾折扩展：返回 (tail_records, group_info, ckpt_path)。"""
    t0 = time.time()
    cfg, bars, features = _build_group_data(gcfg["syms"], gcfg["global"])
    v8_sig = pd.read_parquet(V8_PATH)
    v8_sig["ts"] = pd.to_datetime(v8_sig["ts"])
    fwd_cs_panel = _build_fwd_cs_panel(features, bars, int(cfg.forecast.horizon), "cross_z", "all")

    all_tail: list[dict] = []
    infos: list[dict] = []
    for sym in gcfg["syms"]:
        tail_recs, info = _tail_ext_for_symbol(sym, cfg, bars, features, v8_sig, fwd_cs_panel)
        all_tail.extend(tail_recs)
        infos.append(info)
        mark = "VERIFIED" if info.get("verified_vs_v8") else "!! NOT-VERIFIED"
        print(f"    [{sym}] folds={info.get('n_folds')} fold_end={info.get('fold_test_end_excl')} "
              f"tail={info.get('n_tail_recs')} ({info.get('tail_first')}~{info.get('tail_last')}) "
              f"[{mark}] max_diff={info.get('max_diff', float('nan')):.2e}")

    ckpt = CKPT_DIR / f"{group_name}.parquet"
    CKPT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(all_tail).to_parquet(ckpt, index=False)
    group_info = {
        "group": group_name,
        "n_tail": len(all_tail),
        "infos": infos,
        "elapsed_s": round(time.time() - t0, 1),
        "all_verified": bool(infos) and all(i.get("verified_vs_v8") for i in infos),
    }
    print(f"  [{group_name}] 尾折扩展完成：{len(all_tail)} 条追加信号 "
          f"({time.time() - t0:.0f}s, all_verified={group_info['all_verified']})")
    return all_tail, group_info, ckpt


def merge_tail_ext() -> Path:
    """合并 8 组 checkpoint 尾信号 → tail_ext 缓存（v8 原样 + 追加尾信号）。"""
    v8 = pd.read_parquet(V8_PATH)
    v8["ts"] = pd.to_datetime(v8["ts"])
    frames: list[pd.DataFrame] = []
    problems: list[str] = []
    for gname in GROUPS_V2:
        ckpt = CKPT_DIR / f"{gname}.parquet"
        if not ckpt.exists():
            problems.append(f"缺少 {gname} checkpoint")
            continue
        tail = pd.read_parquet(ckpt)
        if len(tail):
            tail["ts"] = pd.to_datetime(tail["ts"])
            frames.append(tail)
    if problems:
        raise SystemExit(f"[FAIL] 缺少 checkpoint：{problems}（先运行本脚本）")

    tail_all = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "ts", "p_up", "exp_ret", "is_effective"]
    )
    # 追加尾信号不得与该品种 v8 行重叠（逐品种校验：ts > 该品种 v8 末信号日）
    v8_max_per_sym = v8.groupby("symbol")["ts"].max().rename("v8_max")
    if len(tail_all):
        chk = tail_all.merge(v8_max_per_sym, left_on="symbol", right_index=True, how="left")
        overlap = int((chk["ts"] <= chk["v8_max"].fillna(pd.Timestamp.min)).sum())
        if overlap:
            bad = chk[chk["ts"] <= chk["v8_max"].fillna(pd.Timestamp.min)]
            raise SystemExit(
                f"[FAIL] 追加尾信号与该品种 v8 行重叠 {overlap} 行"
                f"（首例: {bad.iloc[0]['symbol']} {pd.Timestamp(bad.iloc[0]['ts']).date()} "
                f"<= v8_max {pd.Timestamp(bad.iloc[0]['v8_max']).date()}）"
            )
        tail_all = tail_all.sort_values(["symbol", "ts"])

    out = pd.concat([v8, tail_all], ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"])
    out = out.sort_values(["symbol", "ts"]).reset_index(drop=True)
    ART.mkdir(exist_ok=True)
    out.to_parquet(TAIL_EXT_PATH, index=False)
    print(f"[OK] tail_ext 缓存 {len(out)} 条（v8 {len(v8)} + 尾 {len(tail_all)}）→ {TAIL_EXT_PATH}")
    print(f"  末信号日 v8 {pd.Timestamp(v8['ts'].max()).date()} → "
          f"tail_ext {pd.Timestamp(out['ts'].max()).date()}")
    return TAIL_EXT_PATH


def run_coverage() -> None:
    """Stage A：覆盖率对比 v8 vs tail_ext。"""
    print("-" * 72)
    print("[Stage A] 覆盖率对比 v8 vs tail_ext")
    df = pd.concat(
        [coverage_stats(V8_PATH), coverage_stats(TAIL_EXT_PATH)], ignore_index=True
    )
    ART.mkdir(exist_ok=True)
    df.to_csv(COV_CSV, index=False)
    cols = ["cache", "n_signals", "n_days", "avg_symbols_per_day", "n_symbols",
            "ts_start", "ts_end", "last_signal_date"]
    print(df[cols].to_string(index=False))
    print("\n  2026-07 月（fold 截断真空段）:")
    for _, r in df.iterrows():
        sig = pd.read_parquet(ART / f"signals_cache18_grouped_{r['cache']}.parquet")
        sig["ts"] = pd.to_datetime(sig["ts"])
        jul = sig[(sig["ts"] >= "2026-07-01") & (sig["ts"] < "2026-08-01")]
        aug = sig[sig["ts"] >= "2026-08-01"]
        print(f"  [{r['cache']}] 2026-07 信号 {len(jul)} 行 / {jul['ts'].dt.date.nunique() if len(jul) else 0} 日 | "
              f"2026-08 信号 {len(aug)} 行 / {aug['ts'].dt.date.nunique() if len(aug) else 0} 日")
    print(f"\n  [OK] → {COV_CSV}")


def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def run_eval() -> None:
    """Stage B：引擎 A 单引擎 + 组合 A30/B70（v8 vs tail_ext，生产 cap 口径，同窗口）。"""
    print("-" * 72)
    print("[Stage B] 引擎 A 单引擎 + 组合重估（v8 vs tail_ext，生产 cap 口径，同窗口）")
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    combo = cfg.backtest.combo
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"  prices: {len(prices)} 行 | 口径: 滑点1tick+费0.005%+保证金12%+CONTRACTS18")
    print(f"  引擎A: top_k={ea.top_k} min={ea.min_symbols} cap={ea.group_cap}")
    print(f"  引擎B: win={eb.win} thr={eb.thr} | 组合: A{combo.w_engine_a}/B{combo.w_engine_b}")

    tgt_b = engine_b_targets(prices, win=eb.win, thr=eb.thr)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    rows: list[dict] = []
    tgt_frames: dict[str, pd.DataFrame] = {}
    for cache_label, cache_path in (("v8", V8_PATH), ("tail_ext", TAIL_EXT_PATH)):
        print(f"\n  --- cache={cache_label} ---")
        tgt_a = engine_a_targets_cs(
            prices, top_k=ea.top_k, min_symbols=ea.min_symbols,
            cache_path=cache_path, group_cap=ea.group_cap,
            group_map=ea.group_map, score_col="exp_ret",
        )
        tgt_frames[cache_label] = tgt_a
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt_a, f"A-S2-{cache_label}")
        n_long = int((tgt_a["target"] > 0).sum())
        seg1 = seg_sharpe(eq, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
        seg2 = seg_sharpe(eq, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
        rows.append({
            "cache": cache_label, "kind": "engineA_single", "config": "S2",
            "min_symbols": MIN_SYMBOLS_S2,
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "oos_seg1_sharpe": seg1, "oos_seg2_sharpe": seg2,
            "long_day_ratio": long_ratio, "n_long_rows": n_long,
            "oos_n": m_oos.n_bars if m_oos else 0,
        })
        print(f"    A-S2({cache_label}): 全样本 {m.sharpe:.3f} | OOS {m_oos.sharpe:.3f} "
              f"(seg1 {seg1:.3f}/seg2 {seg2:.3f}) | OOS ret {m_oos.total_return*100:+.1f}% | "
              f"做多行 {n_long}")

        ra, rb = _align(ret, ret_b)
        r = combo_stats_row(ra, rb, float(combo.w_engine_a), bool(combo.vol_target))
        rows.append({
            "cache": cache_label, "kind": "combo", "config": f"A{combo.w_engine_a:.0f}B{combo.w_engine_b:.0f}",
            "min_symbols": MIN_SYMBOLS_S2,
            "w_a": r["w_a"], "w_b": r["w_b"], "vol_target": r["vol_target"],
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"], "oos_sharpe": r["oos_sharpe"],
            "oos_maxdd": r["oos_maxdd"], "oos_ret": r["oos_ret"],
            "oos_seg1_sharpe": np.nan, "oos_seg2_sharpe": np.nan,
            "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r["oos_n"],
        })
        print(f"    combo A30/B70: 全样本 {r['sharpe_full']:.3f} | OOS {r['oos_sharpe']:.3f} "
              f"| OOS ret {r['oos_ret']*100:+.1f}%")

    ret_b_pure, eq_b_pure, _, _, _ = run_engine_row(cfg, cost, prices, tgt_b, "B")
    ra_ref, rb_ref = _align(ret_b_pure, ret_b_pure)
    r_pure = combo_stats_row(ra_ref, rb_ref, 1.0, False)
    rows.append({
        "cache": "ref", "kind": "engineB", "config": "pureB",
        "min_symbols": "", "w_a": 1.0, "w_b": 0.0, "vol_target": False,
        "sharpe_full": r_pure["sharpe_full"], "ann_ret_full": r_pure["ann_ret_full"],
        "maxdd_full": r_pure["maxdd_full"], "oos_sharpe": r_pure["oos_sharpe"],
        "oos_maxdd": r_pure["oos_maxdd"], "oos_ret": r_pure["oos_ret"],
        "oos_seg1_sharpe": seg_sharpe(eq_b_pure, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1]),
        "oos_seg2_sharpe": seg_sharpe(eq_b_pure, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1]),
        "long_day_ratio": np.nan, "n_long_rows": np.nan, "oos_n": r_pure["oos_n"],
    })
    print(f"    pureB 参考: 全样本 {r_pure['sharpe_full']:.3f} | OOS {r_pure['oos_sharpe']:.3f}")

    df = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    df.to_csv(EVAL_CSV, index=False)
    print(f"\n  [OK] → {EVAL_CSV}")

    # ---- 对比摘要 + 关键判断 ----
    verdict: dict = {}

    # ---- 同窗口校验（双层）----
    v8s = pd.read_parquet(V8_PATH)
    v8s["ts"] = pd.to_datetime(v8s["ts"])
    tes = pd.read_parquet(TAIL_EXT_PATH)
    tes["ts"] = pd.to_datetime(tes["ts"])
    v8_max_sym = v8s.groupby("symbol")["ts"].max().rename("v8_max")
    tes_chk = tes.merge(v8_max_sym, left_on="symbol", right_index=True, how="left")
    shared_te = tes_chk[tes_chk["ts"] <= tes_chk["v8_max"]][["symbol", "ts", "p_up", "exp_ret", "is_effective"]]
    merged = shared_te.merge(
        v8s[["symbol", "ts", "p_up", "exp_ret", "is_effective"]],
        on=["symbol", "ts"], suffixes=("_te", "_v8"), how="left",
    )
    n_shared = len(merged)
    n_match = int(merged["p_up_v8"].notna().sum())
    p_up_same = bool((merged["p_up_te"].to_numpy() == merged["p_up_v8"].to_numpy()).all()) if n_match else False
    exp_same = bool((merged["exp_ret_te"].to_numpy() == merged["exp_ret_v8"].to_numpy()).all()) if n_match else False
    print(f"  同窗口校验(逐品种共享行)：tail_ext 共享行 {n_shared} = v8 行 {n_match} "
          f"| p_up 逐行一致 {p_up_same} | exp_ret 逐行一致 {exp_same}")
    verdict["shared_rows_identical"] = bool(n_shared == n_match and p_up_same and exp_same)

    # (2) 引擎 A targets ≤2026-06-29：差异仅来自 4 品种（au0/ag0/m0/sc0）末折更早
    #     → 尾折在其 06-22..06-29 新增行；其余日期 targets 逐日一致。
    if "v8" in tgt_frames and "tail_ext" in tgt_frames:
        def _pre(fr: pd.DataFrame) -> pd.DataFrame:
            return fr[pd.to_datetime(fr.index.get_level_values(1)) <= pd.Timestamp("2026-06-29")]
        pre_v8, pre_te = _pre(tgt_frames["v8"]), _pre(tgt_frames["tail_ext"])
        pre_te = pre_te[~pre_te.index.isin(pre_v8.index)]  # 仅 tail_ext 新增行
        if len(pre_te):
            extra_dates = sorted(set(pd.to_datetime(pre_te.index.get_level_values(1)).strftime("%Y-%m-%d")))
            extra_syms = sorted(pre_te.index.get_level_values(0).unique())
            print(f"  同窗口 targets 差异（≤2026-06-29）：tail_ext 新增 {len(pre_te)} 行，"
                  f"来自 {extra_syms}，日期 {extra_dates[:8]}{'...' if len(extra_dates) > 8 else ''}")
        else:
            print("  同窗口 targets（≤2026-06-29）：tail_ext 与 v8 完全一致")
        verdict["shared_window_target_delta_rows"] = int(len(pre_te))
        if not len(pre_te):
            verdict["shared_window_targets_identical"] = True

    print("\n  === v8 vs tail_ext 对比摘要 ===")
    pivot = df[df["cache"].isin(["v8", "tail_ext"])].copy()
    s2 = pivot[(pivot["kind"] == "engineA_single") & (pivot["config"] == "S2")].set_index("cache")
    cb = pivot[pivot["kind"] == "combo"].set_index("cache")
    if "tail_ext" in s2.index and "v8" in s2.index:
        d_sh = s2.loc["tail_ext", "oos_sharpe"] - s2.loc["v8", "oos_sharpe"]
        d_ret = s2.loc["tail_ext", "oos_ret"] - s2.loc["v8", "oos_ret"]
        d_cb = cb.loc["tail_ext", "oos_sharpe"] - cb.loc["v8", "oos_sharpe"]
        verdict.update({
            "engineA_s2_oos_sharpe_v8": float(s2.loc["v8", "oos_sharpe"]),
            "engineA_s2_oos_sharpe_tail_ext": float(s2.loc["tail_ext", "oos_sharpe"]),
            "engineA_s2_oos_sharpe_delta": float(d_sh),
            "engineA_s2_oos_ret_v8": float(s2.loc["v8", "oos_ret"]),
            "engineA_s2_oos_ret_tail_ext": float(s2.loc["tail_ext", "oos_ret"]),
            "engineA_s2_oos_ret_delta": float(d_ret),
            "combo_oos_sharpe_v8": float(cb.loc["v8", "oos_sharpe"]),
            "combo_oos_sharpe_tail_ext": float(cb.loc["tail_ext", "oos_sharpe"]),
            "combo_oos_sharpe_delta": float(d_cb),
            "candidate": bool(d_sh > 0 and d_cb >= 0),
            "judgement": (
                "tail_ext 改善引擎 A OOS → 登记'引擎 A 信号恢复候选'（需 QA 复核 + 主理人终裁）"
                if (d_sh > 0 and d_cb >= 0) else
                "tail_ext 未显著改善/更差 → 如实报告（06-29 后引擎 A 无信号未必是坏事）"
            ),
        })
        print(f"  === 关键判断：引擎 A S2 OOS Sharpe {s2.loc['v8','oos_sharpe']:.3f} → "
              f"{s2.loc['tail_ext','oos_sharpe']:.3f}（Δ {d_sh:+.3f}）===")
        print(f"  === 组合 A30/B70 OOS Sharpe {cb.loc['v8','oos_sharpe']:.3f} → "
              f"{cb.loc['tail_ext','oos_sharpe']:.3f}（Δ {d_cb:+.3f}）===")
        print(f"  → {verdict['judgement']}")
    else:
        verdict.update({"judgement": "tail_ext 缺失，无法对比"})
        print("  === 关键判断：tail_ext 缺失，无法对比 ===")
    VERDICT_JSON.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="P22-1 尾折扩展研究实验（非 v8 生产口径）")
    ap.add_argument("--only", type=str, default="", help="只跑指定组（逗号分隔）")
    ap.add_argument("--skip-train", action="store_true", help="跳过重建，仅合并 checkpoint + 评估")
    ap.add_argument("--skip-eval", action="store_true", help="仅重建缓存，跳过评估")
    args = ap.parse_args()

    print("=" * 72)
    print("P22-1 尾折扩展研究实验（研究性变体，非 v8 生产口径）")
    print("  方案：末折训练窗不变（与 v8 逐字节一致）+ 仅延长评估窗至 2026-08-21")
    print("  输出：signals_cache18_grouped_v8_tail_ext.parquet（绝不覆盖 v8）")
    print("=" * 72)

    only = set(args.only.split(",")) if args.only else None

    if not args.skip_train:
        infos_all: list[dict] = []
        for gname, gcfg in GROUPS_V2.items():
            if only is not None and gname not in only:
                continue
            ckpt = CKPT_DIR / f"{gname}.parquet"
            if ckpt.exists():
                print(f"[SKIP] {gname}: checkpoint 已存在（{ckpt.name}；如需重跑请删除）")
                continue
            _, ginfo, _ = build_tail_ext_for_group(gname, gcfg)
            infos_all.append(ginfo)
        if not only:
            merge_tail_ext()
        else:
            print(f"[提示] 仅重建单组（{sorted(only)}），未合并 tail_ext 缓存；"
                  f"跑完全部 8 组后再合并。")
    else:
        if not only:
            merge_tail_ext()
        else:
            raise SystemExit("[FAIL] --skip-train 与 --only 不兼容（合并需全部 8 组）")

    if not args.skip_eval:
        if not only and TAIL_EXT_PATH.exists():
            run_coverage()
            run_eval()
        elif not TAIL_EXT_PATH.exists():
            print("[FAIL] tail_ext 缓存不存在，无法评估")
        else:
            print("[提示] 单组模式跳过评估（待全量合并后执行）")

    print("\n[DONE] P22-1 尾折扩展研究实验完成。")


if __name__ == "__main__":
    main()
