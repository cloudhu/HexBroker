"""P13 引擎 A 多模型融合：LightGBM + XGBoost + HistGradientBoosting。

立项背景
--------
引擎 A 生产状态（P8-4/P9 定案，QA VERIFIED）：
- v8 信号缓存 ``artifacts/signals_cache18_grouped_v8.parquet``（label_pool=all +
  cross_z 标签截面化，8,624 行 / 18 品种）；EngineAConfig 已固化
  （top_k=0.30 / S2 min=3 / group_cap / group_map）。
- 单引擎 OOS Sharpe +0.626、OOS 复利 +8.68%、全样本 0.867 / -26.1%；
  组合 A10/B90 OOS 1.612；**但 OOS 截面 IC 仍负（-0.064）**——盈利靠黑色系敞口。

P13 方向：多模型融合——LightGBM 之外引入 XGBoost + HistGradientBoosting，
同特征同标签（v8 口径）独立 walk_forward 训练 → 融合 exp_ret → 重估
引擎 A / 组合，目标改善 OOS 截面 IC 与单引擎表现。

本脚本（自包含编排）
--------------------
Stage A 训练（--train-models xgb,hgb）：
  逐组（GROUPS_V2 8 组）复用 ``walk_forward_lightgbm`` 的特征构建 / 标签
  截面化 / label_pool / walk-forward 网格 / per-fold Platt 校准，仅模型本体
  替换为 XGBoost / HGB（model_cls 参数化，见 scripts/p13_models.py）。
  产出：
    artifacts/signals_cache18_grouped_v9_xgb.parquet
    artifacts/signals_cache18_grouped_v9_hgb.parquet
  与 v8 逐字节同口径：同特征、同标签（label_pool=all + cross_z）、同
  walk_forward 网格（train_len=250/test_len=60/purge=5/embargo=2）、同分组、
  cal_split=0.5、cal_return_all=False。

Stage B 融合 + 重估（--eval-only）：
  融合（按 symbol+ts 对齐三模型 exp_ret）：
    F1 等权 rank 平均：每模型 exp_ret 按日截面 rank → 平均 → 融合 exp_ret
    F2 IC 加权：IS 段（<=2022-04-21）每模型品种内时序 IC 定权重（嵌套零泄漏）
    F3 两模型（LGB+XGB）等权：评估第三模型是否拖累
  每个融合信号 → engine_a_targets_cs（S2 min=3）→ BacktestEngine 完整回测
  → 全样本 + OOS（复利）Sharpe/复利/MaxDD + 组合 A10/B90 + OOS 截面 IC。
  落盘：
    artifacts/p13_multimodel_compare.csv （v8 vs F1/F2/F3 全指标）
    artifacts/p13_model_ic.csv           （各模型/融合的 OOS 截面 IC + 品种内时序 IC）
    artifacts/p13_verdict.json           （最终裁决）

口径铁律：复利口径评估；OOS 2024-07-18 后；嵌套零泄漏（融合权重只用 IS）；
完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）；不改 v8 缓存、
不改数据文件、不改 hexbroker 包。

用法
----
  python scripts/p13_multimodel.py --train-models xgb,hgb --n-jobs 6   # Stage A
  python scripts/p13_multimodel.py --eval-only                          # Stage B
  python scripts/p13_multimodel.py --only-group precious --train-models xgb  # 冒烟
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.feature import build_features
from scripts.ablate_features import align_global_to_inner, load_global_close
from scripts.build_signals18 import CONTRACTS18
from scripts.group_modeling import LOCAL_MAP, load_fundamental_data
from scripts.group_modeling_v2 import GROUPS_V2
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    OOS_START,
    TOP_K,
    engine_b_targets,
)
from scripts.p5_engineA_cross_section import (
    combo_stats_row,
    engine_a_targets_cs,
    run_engine_row,
)
from scripts.p8_3_label_cross_section import cs_ic_summary, realized_returns
from scripts.refine_lightgbm_champion import (
    DATA_START,
    FREQ,
    walk_forward_lightgbm,
)
from scripts.sentinel_phase4_evo import load_local_bars

from scripts.p13_models import HGBForecast, XGBoostForecast

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
XGB_PATH = ART / "signals_cache18_grouped_v9_xgb.parquet"
HGB_PATH = ART / "signals_cache18_grouped_v9_hgb.parquet"
F1_PATH = ART / "signals_cache18_grouped_v9_f1.parquet"
F2_PATH = ART / "signals_cache18_grouped_v9_f2.parquet"
F3_PATH = ART / "signals_cache18_grouped_v9_f3.parquet"
OUT_COMPARE = ART / "p13_multimodel_compare.csv"
OUT_IC = ART / "p13_model_ic.csv"
OUT_VERDICT = ART / "p13_verdict.json"

# P13 模型超参（与 v8 同口径仅限数据/特征/标签/网格；模型本体及其 HP 是实验变量）
# lightgbm：现有默认参数（与 v8 逐字节一致，用于对照，v8 缓存已存在）
# xgboost：与 LGB 对齐量级（lr 0.05 / n_estimators 200 / max_depth 6 / subsample 0.8）
# hgb（sklearn HistGradientBoostingRegressor）：max_iter 200 / lr 0.05 / max_depth 6
XGB_PARAMS = dict(
    xgb_n_estimators=200,
    xgb_lr=0.05,
    xgb_max_depth=6,
    xgb_min_child_weight=1.0,
    xgb_subsample=0.8,
    xgb_colsample_bytree=1.0,
    xgb_reg_lambda=1.0,
    xgb_reg_alpha=0.0,
    xgb_n_jobs=1,
)
HGB_PARAMS = dict(
    hgb_max_iter=200,
    hgb_lr=0.05,
    hgb_max_depth=6,
    hgb_min_samples_leaf=20,
    hgb_l2=1.0,
)

LABEL_MODE = "cross_z"        # v8 口径
LABEL_POOL = "all"            # v8 口径
CAL_SPLIT = 0.5               # v8 口径
CAL_RETURN_ALL = False        # v8 口径
IS_END = "2022-04-21"         # F2 权重只用 IS（零泄漏）
MIN_SYMBOLS_S2 = 3            # P5 终裁：S2 截面 rank + min=3
COMBO_W_A = 0.10              # P9 终裁：A10/B90
COMBO_W_B = 0.90


# ---------------------------------------------------------------------------
# Stage A：多模型 walk-forward 训练（与 build_group_signals 同口径，仅模型参数化）
# ---------------------------------------------------------------------------
def build_group_signals_multi(
    model_cls,
    params: dict,
    group_syms: list[str],
    global_codes: list[str],
    n_jobs: int,
) -> pd.DataFrame:
    """单组训练（镜像 scripts/group_modeling.build_group_signals）。

    特征/标签/网格/校准全部与 v8 一致，仅 ``model_cls`` + ``params`` 参数化。
    """
    cfg = load_config()
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    cfg.data.symbols = std_syms
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross",
                                "normalize", "fundamental"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": global_codes}
    cfg.feature.fundamental_params = {"window": 252, "min_periods": 60, "include_basis": True}

    bars = load_local_bars(std_syms)
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in global_codes}
    fund = load_fundamental_data(std_syms)
    features = build_features(bars, cfg, global_close=gc, fundamental_data=fund)

    wf = walk_forward_lightgbm(
        cfg, bars, features, params=params, model_cls=model_cls,
        collect_models=False, splitter_overrides=None, n_jobs_folds=n_jobs,
        cal_split=CAL_SPLIT, cal_return_all=CAL_RETURN_ALL,
        label_mode=LABEL_MODE, label_pool=LABEL_POOL,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] {group_syms}: {len(sig)} 条信号 (global={global_codes}, "
          f"fundamental={sorted(fund.keys())}, n_feat={len(features.df.columns)})")
    return sig


def train_model(model_type: str, n_jobs: int, only_groups: str = "", resume: bool = True) -> Path:
    """训练单个模型（8 组）→ 写信号缓存 parquet，返回路径。

    resume=True（默认）：若目标缓存已存在，跳过已完成组，仅训练缺失组
    （每完成一组即 checkpoint 落盘，中断/崩溃后可续跑，不丢已完成组）。
    """
    if model_type == "xgb":
        model_cls, params, out_path = XGBoostForecast, XGB_PARAMS, XGB_PATH
    elif model_type == "hgb":
        model_cls, params, out_path = HGBForecast, HGB_PARAMS, HGB_PATH
    else:
        raise ValueError(f"未知 model_type={model_type!r}（可选 xgb/hgb）")

    only = set(only_groups.split(",")) if only_groups else None
    t0 = time.time()
    print("=" * 88)
    print(f"[Stage A] 训练 {model_type.upper()}（GROUPS_V2 8 组，n_jobs={n_jobs}）")
    print(f"  口径: label_mode={LABEL_MODE} label_pool={LABEL_POOL} "
          f"cal_split={CAL_SPLIT} cal_return_all={CAL_RETURN_ALL}")
    print(f"  params: {params}")
    print("=" * 88)

    frames = []
    done_syms: set[str] = set()
    if resume and out_path.exists():
        try:
            prev = pd.read_parquet(out_path)
            if len(prev) and "symbol" in prev.columns:
                frames.append(prev)
                done_syms = set(prev["symbol"].unique())
                print(f"[resume] {model_type}: 已有缓存 {out_path.name}（{len(prev)} 条，"
                      f"已完成品种 {sorted(done_syms)}）→ 只训练缺失组")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] resume 读取缓存失败，从头训练：{exc}")
            frames = []

    for gname, gcfg in GROUPS_V2.items():
        if only is not None and gname not in only:
            continue
        if only is None and done_syms.issuperset(set(gcfg["syms"])):
            print(f"[skip] {gname} 已完成（品种 {gcfg['syms']}）")
            continue
        t1 = time.time()
        sig = build_group_signals_multi(model_cls, params, gcfg["syms"], gcfg["global"], n_jobs)
        print(f"  [{gname}] 耗时 {time.time()-t1:.0f}s")
        frames.append(sig)
        # 每完成一组 checkpoint 落盘（崩溃/中断可续跑，不丢已完成组）
        ART.mkdir(exist_ok=True)
        all_sig = pd.concat(frames, ignore_index=True)
        all_sig = all_sig.drop_duplicates(subset=["symbol", "ts"], keep="last")
        all_sig.to_parquet(out_path, index=False)
        print(f"[checkpoint] {model_type} 中间落盘 {len(all_sig)} 条 → {out_path.name}")

    if not frames:
        print(f"[OK] {model_type}: 全部组已完成，跳过训练（缓存 {out_path.name} 已完整）")
        return out_path
    all_sig = pd.concat(frames, ignore_index=True)
    all_sig = all_sig.drop_duplicates(subset=["symbol", "ts"], keep="last")
    all_sig.to_parquet(out_path, index=False)
    print(f"[OK] {model_type} 信号合并 {len(all_sig)} 条 → {out_path.name} "
          f"| 品种 {sorted(all_sig['symbol'].unique())} | 总耗时 {time.time()-t0:.0f}s")
    return out_path


# ---------------------------------------------------------------------------
# Stage B：融合
# ---------------------------------------------------------------------------
def load_cache(path: Path) -> pd.DataFrame:
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig


def per_day_rank_frame(sig: pd.DataFrame) -> pd.Series:
    """每模型 exp_ret 按日截面 rank(pct=True) → MultiIndex(symbol, ts) 序列。"""
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s["_r"] = s.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    return s.set_index(["symbol", "ts"])["_r"].sort_index()


def _attach_realized(sig: pd.DataFrame, realized: pd.Series) -> pd.DataFrame:
    """给信号表对齐已实现收益 → MultiIndex(symbol, ts) + realized。"""
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s = s.set_index(["symbol", "ts"]).sort_index()
    s["realized"] = realized.reindex(s.index)
    return s


def ic_weights_is(
    caches: dict[str, pd.DataFrame],
    realized: pd.Series,
    is_end: str = IS_END,
) -> dict[str, dict[str, float]]:
    """IS 段每模型品种内时序 Spearman IC → 权重（嵌套零泄漏）。

    对每个品种：w[model] = max(ic, 0) 后按模型归一化（和为 1）；
    全部 IC <= 0 或 NaN → 等权。权重只用 IS（<= is_end），OOS 零泄漏。
    """
    names = list(caches.keys())
    syms = sorted({s for c in caches.values() for s in c["symbol"].unique()})
    weights: dict[str, dict[str, float]] = {}
    for sym in syms:
        ic_by_model: dict[str, float] = {}
        for n in names:
            sub = caches[n][caches[n]["symbol"] == sym].copy()
            sub["ts"] = pd.to_datetime(sub["ts"])
            sub = sub[sub["ts"] <= pd.Timestamp(is_end)]
            if len(sub) < 3:
                ic_by_model[n] = np.nan
                continue
            sub["realized"] = realized.reindex(
                pd.MultiIndex.from_arrays([[sym] * len(sub), sub["ts"]])
            ).to_numpy(dtype=float)
            sub = sub.dropna(subset=["realized"])
            if len(sub) < 3:
                ic_by_model[n] = np.nan
                continue
            ic = sub["exp_ret"].corr(sub["realized"], method="spearman")
            ic_by_model[n] = float(ic) if pd.notna(ic) else np.nan
        vals = [max(0.0, v) for v in ic_by_model.values() if pd.notna(v)]
        total = float(sum(vals))
        if total <= 0:
            weights[sym] = {n: 1.0 / len(names) for n in names}
        else:
            weights[sym] = {}
            for n in names:
                v = ic_by_model.get(n, np.nan)
                weights[sym][n] = (max(0.0, v) / total) if pd.notna(v) else 0.0
    return weights


def fuse_equal(frames: dict[str, pd.Series], names: list[str]) -> pd.DataFrame:
    """F1/F3 等权 rank 平均 → [symbol, ts, exp_ret]。"""
    df = pd.DataFrame({n: frames[n] for n in names}).dropna()
    out = pd.DataFrame({
        "symbol": df.index.get_level_values(0),
        "ts": df.index.get_level_values(1),
        "exp_ret": df.mean(axis=1).to_numpy(dtype=float),
    })
    return out


def fuse_ic(
    frames: dict[str, pd.Series],
    caches: dict[str, pd.DataFrame],
    names: list[str],
    realized: pd.Series,
    is_end: str = IS_END,
) -> pd.DataFrame:
    """F2 IC 加权融合：权重 = IS 品种内时序 IC（见 ic_weights_is）。

    融合分 = Σ_m w[m, sym] * rank_m(sym, ts)（rank 尺度与 F1 一致）。
    """
    df = pd.DataFrame({n: frames[n] for n in names}).dropna()
    weights = ic_weights_is(caches, realized, is_end)
    rows = []
    for (sym, ts), row in df.iterrows():
        ws = weights.get(sym)
        if ws is None:
            val = float(row.mean())
        else:
            val = float(sum(row[n] * ws[n] for n in names))
        rows.append((sym, ts, val))
    out = pd.DataFrame(rows, columns=["symbol", "ts", "exp_ret"])
    return out


def build_fused_caches(realized: pd.Series) -> dict[str, Path]:
    """构造 F1/F2/F3 融合缓存 → 写 parquet，返回 {variant: path}。"""
    caches = {"v8": load_cache(V8_PATH), "xgb": load_cache(XGB_PATH), "hgb": load_cache(HGB_PATH)}
    for n, c in caches.items():
        print(f"  [cache {n}] {len(c)} 行 | ts {c['ts'].min().date()} ~ {c['ts'].max().date()} | "
              f"品种 {c['symbol'].nunique()}")
    frames = {n: per_day_rank_frame(c) for n, c in caches.items()}

    # 对齐校验：三模型 (symbol, ts) 集合是否一致（同网格同 cal_split → 应一致）
    base_idx = set(frames["v8"].index)
    for n in ("xgb", "hgb"):
        idx = set(frames[n].index)
        inter = len(base_idx & idx)
        print(f"  [align {n}] v8∩{n}={inter} | v8-only={len(base_idx-idx)} | {n}-only={len(idx-base_idx)}")

    f1 = fuse_equal(frames, ["v8", "xgb", "hgb"])
    f3 = fuse_equal(frames, ["v8", "xgb"])
    f2 = fuse_ic(frames, caches, ["v8", "xgb", "hgb"], realized)

    outs = {"F1": (f1, F1_PATH), "F2": (f2, F2_PATH), "F3": (f3, F3_PATH)}
    for variant, (df, path) in outs.items():
        ART.mkdir(exist_ok=True)
        df.to_parquet(path, index=False)
        print(f"  [fused {variant}] {len(df)} 行 → {path.name}")
    return {"F1": F1_PATH, "F2": F2_PATH, "F3": F3_PATH}


# ---------------------------------------------------------------------------
# Stage B：重估（引擎 A 单引擎 + 组合 A10/B90 + OOS 截面 IC）
# ---------------------------------------------------------------------------
def per_symbol_oos_ts_ic(sig: pd.DataFrame, realized: pd.Series, oos_start: str) -> list[dict]:
    """OOS 品种内时序 Spearman(exp_ret, realized)。"""
    s = _attach_realized(sig, realized)
    s = s[s.index.get_level_values(1) >= pd.Timestamp(oos_start)]
    s = s.dropna(subset=["realized"])
    rows = []
    for sym, g in s.groupby(level=0):
        if len(g) >= 3:
            ic = g["exp_ret"].corr(g["realized"], method="spearman")
            rows.append({"symbol": sym, "ic": float(ic) if pd.notna(ic) else np.nan, "n": int(len(g))})
        else:
            rows.append({"symbol": sym, "ic": np.nan, "n": int(len(g))})
    return rows


def evaluate_variant(
    cfg,
    cost,
    prices: pd.DataFrame,
    realized: pd.Series,
    ret_b: pd.Series,
    variant: str,
    cache_path: Path,
    group_cap: float | None = None,
    group_map: dict[str, str] | None = None,
) -> list[dict]:
    """单个变体的重估行（A-S2 / A10B90-N / A10B90-Y）。

    group_cap / group_map：P13 QA 复核（DISCREPANCY-1）——必须显式传生产配置
    （load_config("configs/base.yaml") 的 backtest.engine_a 值），因为
    engine_a_targets_cs 内部的 _resolve_* 用无 path 的 load_config() 读不到
    base.yaml（group_cap=None → 无 cap + GROUPS_V2 默认组，非生产口径）。
    """
    sig = load_cache(cache_path)
    tgt_a = engine_a_targets_cs(
        prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cache_path,
        group_cap=group_cap, group_map=group_map,
    )
    ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(cfg, cost, prices, tgt_a, f"A-{variant}")
    summ = cs_ic_summary(sig, realized, OOS_START)
    oos_ic = summ["oos"]
    rows = [{
        "variant": variant, "engine": "A-S2",
        "sharpe_full": m_a.sharpe, "ann_ret_full": m_a.annual_return,
        "maxdd_full": m_a.max_drawdown,
        "oos_sharpe": m_oos_a.sharpe if m_oos_a else np.nan,
        "oos_maxdd": m_oos_a.max_drawdown if m_oos_a else np.nan,
        "oos_ret": m_oos_a.total_return if m_oos_a else np.nan,
        "long_day_ratio": long_ratio, "oos_n": m_oos_a.n_bars if m_oos_a else 0,
        "oos_cs_ic": oos_ic["ic_mean"], "oos_cs_icir": oos_ic["icir"],
        "oos_cs_pos_ratio": oos_ic["pos_ratio"],
    }]
    for vt in (False, True):
        # 组合对齐：两序列交集（次要1修复：rb 应与 ret_a 取交集，非 ret_b∩ret_b no-op）
        ra = ret_a.loc[ret_a.index.intersection(ret_b.index)]
        rb = ret_b.loc[ret_b.index.intersection(ret_a.index)]
        r = combo_stats_row(ra, rb, COMBO_W_A, vt)
        rows.append({
            "variant": variant,
            "engine": f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}" + ("-volY" if vt else "-volN"),
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"],
            "oos_sharpe": r["oos_sharpe"], "oos_maxdd": r["oos_maxdd"],
            "oos_ret": r["oos_ret"], "long_day_ratio": np.nan,
            "oos_n": r["oos_n"], "oos_cs_ic": oos_ic["ic_mean"],
            "oos_cs_icir": oos_ic["icir"], "oos_cs_pos_ratio": oos_ic["pos_ratio"],
        })
    print(f"  [{variant:<4} A-S2] Sharpe={m_a.sharpe:.3f} 年化={m_a.annual_return*100:+.1f}% "
          f"MaxDD={m_a.max_drawdown*100:.1f}% | OOS Sharpe={m_oos_a.sharpe:.3f} "
          f"OOS 复利={m_oos_a.total_return*100:+.2f}% OOS MaxDD={m_oos_a.max_drawdown*100:.1f}% "
          f"| OOS 截面IC={oos_ic['ic_mean']:+.4f} (ICIR={oos_ic['icir']:+.3f})")
    return rows


def ic_table_rows(variants: dict[str, Path], realized: pd.Series) -> list[dict]:
    """各模型/融合的 OOS 截面 IC + 品种内时序 IC → 长表行。"""
    rows = []
    for variant, path in variants.items():
        sig = load_cache(path)
        summ = cs_ic_summary(sig, realized, OOS_START)
        for seg in ("full", "is", "oos"):
            st = summ[seg]
            rows.append({
                "variant": variant, "ic_type": "cs", "segment": seg, "symbol": "ALL",
                "ic": st["ic_mean"], "icir": st["icir"], "pos_ratio": st["pos_ratio"],
                "n": st["n_days"],
            })
        rows.append({
            "variant": variant, "ic_type": "pooled", "segment": "full", "symbol": "ALL",
            "ic": summ["pooled_rank_ic"], "icir": np.nan, "pos_ratio": np.nan, "n": int(len(sig)),
        })
        for r in per_symbol_oos_ts_ic(sig, realized, OOS_START):
            rows.append({
                "variant": variant, "ic_type": "ts", "segment": "oos", "symbol": r["symbol"],
                "ic": r["ic"], "icir": np.nan, "pos_ratio": np.nan, "n": r["n"],
            })
    return rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P13 引擎 A 多模型融合（LGB+XGB+HGB）")
    ap.add_argument("--train-models", type=str, default="",
                    help="逗号分隔要训练的模型（xgb/hgb），空=不训练")
    ap.add_argument("--eval-only", action="store_true",
                    help="只做融合+重估（需 v8/xgb/hgb 缓存已存在）")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--only-group", type=str, default="",
                    help="只训练指定组（逗号分隔，冒烟用）")
    args = ap.parse_args()

    t_start = time.time()
    print("=" * 96)
    print("P13 引擎 A 多模型融合（LightGBM + XGBoost + HistGradientBoosting）")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"label_mode={LABEL_MODE} label_pool={LABEL_POOL} cal_split={CAL_SPLIT} "
          f"cal_return_all={CAL_RETURN_ALL}")
    print("=" * 96)

    # ---- Stage A: 训练 ----
    if args.train_models:
        for mt in [m.strip() for m in args.train_models.split(",") if m.strip()]:
            if mt not in ("xgb", "hgb"):
                print(f"[WARN] 跳过未知模型 {mt!r}（可选 xgb/hgb）")
                continue
            train_model(mt, args.n_jobs, args.only_group)

    if not args.eval_only and not args.train_models:
        print("[FAIL] 未指定 --train-models 或 --eval-only")
        return

    # ---- Stage B: 融合 + 重估 ----
    need = {"v8": V8_PATH, "xgb": XGB_PATH, "hgb": HGB_PATH}
    missing = [str(p) for n, p in need.items() if not p.exists()]
    if missing:
        print(f"[FAIL] 缺少缓存：{missing}（先运行 --train-models xgb,hgb）")
        return
    # 完整性校验：v8 与 xgb/hgb 必须同覆盖（同品种集合 + 同行数），防中断残留缓存混入
    v8_syms = set(pd.read_parquet(V8_PATH, columns=["symbol"])["symbol"].unique())
    v8_n = len(pd.read_parquet(V8_PATH, columns=["symbol"]))
    for n, p in need.items():
        df = pd.read_parquet(p, columns=["symbol"])
        syms = set(df["symbol"].unique())
        print(f"  [cover {n}] {len(syms)} 品种 {len(df)} 行 | 缺 {sorted(v8_syms - syms)} | "
              f"与 v8 同集={syms == v8_syms} 同行数={len(df) == v8_n}")
        if syms != v8_syms or len(df) != v8_n:
            print(f"[FAIL] {n} 缓存不完整（品种集合/行数 ≠ v8），请先补全训练（--train-models {n}）")
            return

    print("-" * 96)
    print("[Stage B] 环境：prices / 引擎 B / realized（生产口径：显式 base.yaml + group_cap/group_map）")
    # P13 QA 复核（DISCREPANCY-1）：必须显式加载 configs/base.yaml 并以参数传入
    # engine_a_targets_cs——其内部 _resolve_* 用无 path load_config() 读不到
    # base.yaml 的 group_cap=0.5 / ferrous_all（否则评估落在无 cap + GROUPS_V2 口径）。
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    ea_cfg = cfg.backtest.engine_a
    group_cap = ea_cfg.group_cap if hasattr(ea_cfg, "group_cap") else None
    group_map = ea_cfg.group_map if hasattr(ea_cfg, "group_map") else None
    print(f"  生产口径: group_cap={group_cap!r} group_map={len(group_map) if group_map else 0} 键"
          f"（ferrous_all 合并={set(group_map or {}).issuperset({'i0','j0','jm0','rb0','hc0'})}）")
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    realized = realized_returns(prices)
    print(f"  prices {len(prices)} 行 {prices.index.get_level_values(0).nunique()} 品种 | "
          f"realized {len(realized)} 行")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]

    print("-" * 96)
    print("[Stage B] 融合信号构造（F1 等权 / F2 IC加权 / F3 LGB+XGB）")
    fused = build_fused_caches(realized)

    variants = {"v8": V8_PATH, "xgb": XGB_PATH, "hgb": HGB_PATH, **fused}

    print("-" * 96)
    print("[Stage B] 重估：引擎 A S2 + 组合 A10/B90 + OOS 截面 IC（生产口径）")
    rows: list[dict] = []
    for variant, path in variants.items():
        rows.extend(evaluate_variant(cfg, cost, prices, realized, ret_b, variant, path,
                                     group_cap=group_cap, group_map=group_map))

    tbl = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    tbl.to_csv(OUT_COMPARE, index=False)
    print(f"  [OK] → {OUT_COMPARE}")

    print("-" * 96)
    print("[Stage B] 模型/融合 IC 明细（OOS 截面 IC + 品种内时序 IC）")
    ic_rows = ic_table_rows(variants, realized)
    ic_tbl = pd.DataFrame(ic_rows)
    ic_tbl.to_csv(OUT_IC, index=False)
    print(f"  [OK] → {OUT_IC}")
    cs_pivot = ic_tbl[ic_tbl["ic_type"] == "cs"].pivot_table(
        index="variant", columns="segment", values="ic"
    )[["full", "is", "oos"]]
    print(cs_pivot.round(4).to_string())
    ts_pivot = ic_tbl[ic_tbl["ic_type"] == "ts"].pivot_table(
        index="variant", columns="symbol", values="ic"
    )
    print("  OOS 品种内时序 IC（各品种）：")
    print(ts_pivot.round(4).to_string())

    # ---- 最终裁决 ----
    verdict = make_verdict(tbl, ic_tbl)
    with open(OUT_VERDICT, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    print("=" * 96)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s | 对比表 → {OUT_COMPARE} | "
          f"IC → {OUT_IC} | 裁决 → {OUT_VERDICT}")
    print("=" * 96)


def make_verdict(tbl: pd.DataFrame, ic_tbl: pd.DataFrame) -> dict:
    """基于复利口径对比表给出裁决（如实报告，含无改善情形）。"""
    a = tbl[tbl["engine"] == "A-S2"].set_index("variant")
    combo = tbl[tbl["engine"] == f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}-volN"].set_index("variant")
    base_a = a.loc["v8", "oos_sharpe"]
    base_combo = combo.loc["v8", "oos_sharpe"]
    base_ic = a.loc["v8", "oos_cs_ic"]
    print("=" * 96)
    print("最终裁决（复利口径；基线 v8）")
    print(f"  单引擎 OOS Sharpe 基线 {base_a:+.3f} | 组合 A10/B90 OOS 基线 {base_combo:.3f} "
          f"| OOS 截面 IC 基线 {base_ic:+.4f}")
    rows = []
    for v in ("F1", "F2", "F3"):
        rows.append({
            "variant": v,
            "delta_oos_sharpe": a.loc[v, "oos_sharpe"] - base_a,
            "delta_combo_oos_sharpe": combo.loc[v, "oos_sharpe"] - base_combo,
            "delta_oos_cs_ic": a.loc[v, "oos_cs_ic"] - base_ic,
            "oos_sharpe": a.loc[v, "oos_sharpe"],
            "combo_oos_sharpe": combo.loc[v, "oos_sharpe"],
            "oos_cs_ic": a.loc[v, "oos_cs_ic"],
        })
    d = pd.DataFrame(rows)
    for _, r in d.iterrows():
        print(f"  [{r['variant']}] ΔOOS Sharpe={r['delta_oos_sharpe']:+.3f} "
              f"Δ组合={r['delta_combo_oos_sharpe']:+.3f} Δ截面IC={r['delta_oos_cs_ic']:+.4f}")

    # 择优（预提交的多准则规则，与任务"关键指标"一致）：
    #   1) OOS 截面 IC 改善（P13 核心目标）
    #   2) 单引擎 OOS Sharpe 不降（基线 +0.626）
    #   3) 组合 A10/B90 OOS Sharpe 不降（基线 1.612）
    # 同时满足三者 → 采纳；无三者全满足时 → 回退取 IC 改善最大的方案。
    ic_improved = [v for v in ("F1", "F2", "F3") if a.loc[v, "oos_cs_ic"] > base_ic]
    all3 = [v for v in ic_improved
            if a.loc[v, "oos_sharpe"] > base_a and combo.loc[v, "oos_sharpe"] > base_combo]
    if all3:
        best = max(all3, key=lambda v: a.loc[v, "oos_cs_ic"])
        adopted = best
        is_pass = True
        reason = (f"{best} 同时改善三项关键指标：OOS 截面 IC "
                  f"{base_ic:+.4f} → {a.loc[best, 'oos_cs_ic']:+.4f}，单引擎 OOS Sharpe "
                  f"{base_a:+.3f} → {a.loc[best, 'oos_sharpe']:+.3f}，组合 A10/B90 OOS "
                  f"{base_combo:.3f} → {combo.loc[best, 'oos_sharpe']:.3f}")
    elif ic_improved:
        # 生产口径下无方案同时改善三项关键指标 → 不采纳（IC 改善不足以弥补 Sharpe 下降）
        best = max(ic_improved, key=lambda v: a.loc[v, "oos_cs_ic"])
        adopted = None
        is_pass = False
        reason = (f"生产口径（group_cap=0.5 + ferrous_all）下无融合方案同时改善三项关键指标："
                  f"IC 虽有改善（{base_ic:+.4f} → {a.loc[best, 'oos_cs_ic']:+.4f}，最大者为 {best}），"
                  f"但单引擎 OOS Sharpe（基线 {base_a:+.3f}）与组合 A10/B90（基线 {base_combo:.3f}）"
                  f"全部融合均未超过基线 → 不采纳；收益瓶颈不在模型多样性")
    else:
        best = None
        is_pass = False
        reason = (f"三个融合方案的 OOS 截面 IC 均未改善（基线 {base_ic:+.4f}）——"
                  f"瓶颈不在模型多样性；若单引擎/组合仍有提升可作为辅助证据")
    return {
        "is_pass": is_pass,
        "adopted_fusion": adopted,
        "baseline": {"oos_sharpe_a": float(base_a), "combo_oos_sharpe": float(base_combo),
                     "oos_cs_ic": float(base_ic)},
        "details": {v: {k: (float(r[k]) if pd.notna(r[k]) else None)
                        for k in ("delta_oos_sharpe", "delta_combo_oos_sharpe",
                                  "delta_oos_cs_ic", "oos_sharpe", "combo_oos_sharpe", "oos_cs_ic")}
                    for v, r in d.set_index("variant").iterrows()},
        "reason": reason,
    }


if __name__ == "__main__":
    main()
