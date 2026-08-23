"""P15 合约层修复：稳定校准复验（p_up）+ 整数手 sizing 修复。

立项背景（P14 定案，QA VERIFIED + D-1 修正）
--------------------------------------------
- v8 生产缓存 ``artifacts/signals_cache18_grouped_v8.parquet``（label_pool=all +
  cross_z 标签截面化）；EngineAConfig 固化（top_k=0.30 / S2 min=3 / group_cap=0.5 /
  group_map=ferrous_all）。单引擎 OOS 0.646/+9.01%、组合 A10/B90 OOS 1.614、
  OOS 截面 IC -0.064。
- P14 两项关键定位：
  1. ⚠️ QA D-1：per-fold Platt 校准（~15 样本）是噪声伪影——p_up 与 exp_ret
     rank corr 仅 0.088、斜率符号抛硬币（336 正 / 210 负块）、IS(-0.035)↔OOS(+0.1395)
     不稳定 → v10_cal p_up 的 OOS IC +0.1395 转正不可信，需稳定校准复验。
  2. ✅ 整数手 floor 根因成立：NOTIONAL_FRAC=0.20×1e6=200k 名义 → au0/cu0/i0/j0/sc0
     高价合约 OOS 全程 0 手（floor 后）；引擎仅转化信号层 edge 的 ~11-13%。

本脚本（自包含编排，两阶段）
----------------------------
Stage A（P15-1 稳定校准）：
  - A1 ``walk_forward_raw_collect``：镜像 v8 训练（同特征/同标签/同网格/同 HP），
    但以 ``cal_split=None + cal_min_samples=1e9`` 收集**每折全测试窗原始信号**
    （含 fi 折号；v8 口径下校准从不触发 → 这些就是 v8 的原始 p_up/exp_ret）。
  - A2 ``fit_growing_pool_calibration``（方案 A 池化校准 + 方案 C 斜率符号检验）：
    每品种逐折（时间序）构建**因果增长校准池** = 折 0..i 的前 cal_split 比例有效
    信号（fold i 池 ~15→450+ 样本，远大于 per-fold 的 15）；池 >= MIN_POOL(50)
    时拟合单一 Platt 校准器（复用 hexbroker PlattScaler），应用于本折评估子窗；
    池不足则保持原始 p_up（identity）。
  - 产出 ``artifacts/signals_cache18_grouped_v11_cal.parquet``（v11 稳定校准版，
    列对齐 v8；额外含 cal_pool_n/cal_slope 元数据列，供验证）。
  - 验证（p15_cal_verify.csv）：① p_up vs exp_ret rank corr（对比 v10_cal 的 0.088）
    ② p_up OOS 截面 IC（对比 v10_cal 的 +0.1395 伪影）③ |IS IC - OOS IC| 稳定性
    ④ 每折校准器斜率符号稳定性（方案 C：OOS 折正斜率占比 / 翻转次数）。

Stage B（P15-2 整数手 sizing 修复）：
  - ``engine_a_targets_cs_sized`` 实现 3 个方案（同 v8 口径，仅 sizing 参数化）：
      sizing="int"        （基线：floor 整数手，NOTIONAL_FRAC=0.20 —— 与 v8 逐字节一致）
      sizing="frac"       （方案 2：float 手数 = 名义比例；BacktestEngine/SimBroker 支持
                            小数仓，见 hexbroker/backtest/broker.py 的 float 持仓记账）
      sizing="tradable"   （方案 1：剔除 floor 后 0 手的不可交易合约后重算 top_k 选择，
                            再 floor 整数手 —— 恢复高价合约的可交易性）
      sizing="notional035"（方案 3：NOTIONAL_FRAC 0.20→0.35 调高名义后 floor 整数手）
  - 矩阵：4 个（缓存×选择变量）x 4 sizing，全部生产 cap 口径
    （显式 base.yaml + group_cap/group_map 显式传参，P13 DISCREPANCY-1 教训）。
  - 关键指标：收益转化率（各方案 OOS 复利 / frac 理想口径 OOS 复利）、
    au0/cu0/i0/j0/sc0 是否恢复可交易（OOS 做多天数）、组合 A10/B90 OOS。
  - 产出 ``artifacts/p15_sizing_compare.csv`` + ``artifacts/p15_tradability.csv``。

最终裁决 ``artifacts/p15_verdict.json``：
  - p_up 真信号确认与否（稳定校准后 OOS IC / IS-OOS 稳定性 / 斜率符号）
  - sizing 方案采纳建议（OOS Sharpe / 转化率 / 可交易性恢复）

口径铁律：复利口径；OOS 2024-07-18 后；嵌套零泄漏（增长池因果、评估子窗只用
折 0..i 校准数据）；完整回测（滑点1tick+费0.005%+保证金12%+CONTRACTS18）；
生产 cap 口径（显式 base.yaml + 显式传参）。不改 v8 缓存、不改数据文件、
不改 hexbroker 包（仅 scripts 层新增本脚本，复用既有函数/类）。

用法
----
  python scripts/p15_contract_layer.py --train-v11 --n-jobs 8     # Stage A1 训练（后台+checkpoint）
  python scripts/p15_contract_layer.py --calibrate-v11            # Stage A2 稳定校准后处理
  python scripts/p15_contract_layer.py --eval                     # Stage B 验证+sizing+裁决
  python scripts/p15_contract_layer.py --train-v11 --only-group precious   # 冒烟
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
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.feature import build_features
from hexbroker.forecast.calibration import PlattScaler
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
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
    _capped_selection,
    _resolve_cache_path,
    _resolve_group_cap,
    _resolve_group_map,
    combo_stats_row,
    engine_a_selection,
    engine_a_targets_cs,
    run_engine_row,
)
from scripts.p8_3_label_cross_section import cs_ic_summary, realized_returns
from scripts.refine_lightgbm_champion import (
    DATA_START,
    FREQ,
    _build_fwd_cs_panel,
    _forward_returns,
    _train_eval_fold,
    _lookback,
)
from scripts.sentinel_phase4_evo import load_local_bars

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V10_CAL_PATH = ART / "signals_cache18_grouped_v10_cal.parquet"
V11_RAW_PATH = ART / "signals_cache18_grouped_v11_raw.parquet"
V11_CAL_PATH = ART / "signals_cache18_grouped_v11_cal.parquet"
OUT_CAL_VERIFY = ART / "p15_cal_verify.csv"
OUT_SIZING = ART / "p15_sizing_compare.csv"
OUT_TRADABILITY = ART / "p15_tradability.csv"
OUT_SLOPES = ART / "p15_cal_slopes.csv"
OUT_VERDICT = ART / "p15_verdict.json"
TRAIN_LOG = ART / "p15_train.log"

# 口径常量（与 v8 / P13 / P14 完全一致）
LABEL_POOL = "all"            # v8 口径：全 18 品种统一截面
CAL_SPLIT = 0.5               # v8 口径：嵌套校准分割
CAL_RETURN_ALL = False        # v8 口径
MIN_SYMBOLS_S2 = 3            # P5 终裁：S2 截面 rank + min=3
COMBO_W_A = 0.10              # P9 终裁：A10/B90
COMBO_W_B = 0.90
HORIZON = 5

# P15-1 稳定校准参数
MIN_POOL = 50                 # 方案 B：校准池最小样本（>=50 才拟合校准器）
SIZINGS = ["int", "frac", "tradable", "notional035"]
SIZING_NOTIONAL = {"int": 0.20, "frac": 0.20, "tradable": 0.20, "notional035": 0.35}
HIGH_PRICE_SYMS = ["au0", "cu0", "i0", "j0", "sc0"]   # P14 定位的 floor 后 0 手合约


# ===========================================================================
# Stage A1：原始信号收集（v8 同口径，cal_split=None + cal_min_samples=1e9
#           使 per-fold 校准从不触发 → 返回每折全测试窗原始信号，带 fi 折号）
# ===========================================================================
def walk_forward_raw_collect(
    cfg,
    bars,
    features,
    params: dict | None = None,
    n_jobs_folds: int = 1,
    splitter_overrides: dict | None = None,
    label_mode: str = "cross_z",
    label_pool: str = "all",
) -> list[dict]:
    """收集每折全测试窗原始信号（p_up/exp_ret 均未校准）。

    任务构造与 ``walk_forward_lightgbm`` 并行路径逐字段一致（17 字段），仅
    ``cal_split=None + cal_min_samples=1e9`` 使 ``_calibrate_and_split`` 跳过校准、
    返回全部信号；worker 内部训练/预测逻辑与 v8 完全一致（确定性，见 P14 验证）。
    返回 records 列表（每行含 symbol/ts/p_up/exp_ret/is_effective/fi）。
    """
    from hexbroker.forecast.baselines import LightGBMForecast

    model_cls = LightGBMForecast
    horizon = int(cfg.forecast.horizon)
    lookback = _lookback(cfg)
    if params:
        for k, v in params.items():
            setattr(cfg.forecast, k, v)

    sp = splitter_overrides or dict(
        train_len=int(cfg.data.train_len), test_len=int(cfg.data.test_len),
        purge=int(cfg.data.purge), embargo=int(cfg.data.embargo), mode=str(cfg.data.mode),
    )
    splitter = WalkForwardSplitter(**sp)

    # 截面化训练标签面板（复用 refine_lightgbm_champion 原函数，保证逐字节一致）
    fwd_cs_panel = _build_fwd_cs_panel(features, bars, horizon, label_mode, label_pool)

    cal_method = str(getattr(cfg.forecast, "calibration_method", "platt"))
    cfg_dict = cfg.model_dump()
    tasks: list = []
    feat_names: list | None = None
    for sym in features.symbols:
        feat = features.df.loc[[sym]].sort_index()
        sym_df = bars.by_symbol(sym)
        close = sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
        if len(feat) < cfg.feature.normalize_window + horizon + 10:
            print(f"[WARN] {sym} 样本不足，跳过")
            continue
        fwd = _forward_returns(close, horizon)
        folds = splitter.split(feat.index)
        if not folds:
            continue
        splitter.assert_no_leakage(folds)
        if feat_names is None:
            feat_names = list(feat.columns)
        for fi, fold in enumerate(folds):
            tasks.append((sym, fi, fold.train_max_pos, fold.test_start, fold.test_end,
                          feat, close, cfg_dict, params, False, cal_method, model_cls,
                          None, CAL_RETURN_ALL, label_mode, fwd_cs_panel, int(1e9)))
    if feat_names is None:
        feat_names = []

    records: list[dict] = []
    if n_jobs_folds > 1:
        import concurrent.futures as _cf
        with _cf.ProcessPoolExecutor(max_workers=int(n_jobs_folds)) as ex:
            for sym_r, fi, recs, _imp in ex.map(_train_eval_fold, tasks):
                for r in recs:
                    r["fi"] = fi
                    records.append(r)
    else:
        for t in tasks:
            sym_r, fi, recs, _imp = _train_eval_fold(t)
            for r in recs:
                r["fi"] = fi
                records.append(r)
    return records


# ---------------------------------------------------------------------------
# Stage A1（续）：截面化标签面板复用 refine_lightgbm_champion._build_fwd_cs_panel
# ---------------------------------------------------------------------------
def build_group_signals_p15_raw(
    params: dict,
    group_syms: list[str],
    global_codes: list[str],
    n_jobs: int,
    label_mode: str,
    label_pool: str,
) -> pd.DataFrame:
    """单组训练（镜像 p14.build_group_signals_p14，但输出带 fi 的原始信号）。"""
    cfg = load_config()
    std_syms = [LOCAL_MAP[s] for s in group_syms]
    cfg.data.symbols = std_syms
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"
    cfg.forecast.horizon = HORIZON
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

    records = walk_forward_raw_collect(
        cfg, bars, features, params=params, n_jobs_folds=n_jobs,
        label_mode=label_mode, label_pool=label_pool,
    )
    sig = pd.DataFrame(records)
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] {group_syms}: {len(sig)} 条原始信号 (global={global_codes}, "
          f"fundamental={sorted(fund.keys())}, n_feat={len(features.df.columns)})")
    return sig


def train_v11_raw(
    n_jobs: int,
    only_groups: str = "",
    resume: bool = True,
) -> Path:
    """训练原始信号（GROUPS_V2 8 组，fi 折号）→ checkpoint 落盘 v11_raw.parquet。

    resume=True：若缓存已存在则跳过已完成组，每完成一组即落盘（中断可续跑）。
    """
    only = set(only_groups.split(",")) if only_groups else None
    params = load_best_params()
    t0 = time.time()
    print("=" * 88)
    print(f"[Stage A1] v11 原始信号收集（GROUPS_V2 8 组，n_jobs={n_jobs}）")
    print(f"  口径: label_mode=cross_z label_pool={LABEL_POOL} cal_split=None "
          f"(不触发 per-fold 校准) | params: {params}")
    print("=" * 88, flush=True)

    frames = []
    done_syms: set[str] = set()
    if resume and V11_RAW_PATH.exists():
        try:
            prev = pd.read_parquet(V11_RAW_PATH)
            if len(prev) and "symbol" in prev.columns:
                frames.append(prev)
                done_syms = set(prev["symbol"].unique())
                print(f"[resume] v11_raw 已有缓存 {V11_RAW_PATH.name}（{len(prev)} 条，"
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
        sig = build_group_signals_p15_raw(
            params, gcfg["syms"], gcfg["global"], n_jobs,
            "cross_z", LABEL_POOL,
        )
        print(f"  [{gname}] 耗时 {time.time()-t1:.0f}s", flush=True)
        frames.append(sig)
        ART.mkdir(exist_ok=True)
        all_sig = pd.concat(frames, ignore_index=True)
        all_sig = all_sig.drop_duplicates(subset=["symbol", "ts", "fi"], keep="last")
        all_sig.to_parquet(V11_RAW_PATH, index=False)
        print(f"[checkpoint] v11_raw 中间落盘 {len(all_sig)} 条 → {V11_RAW_PATH.name}", flush=True)

    if not frames:
        print(f"[OK] v11_raw 全部组已完成，跳过训练（缓存 {V11_RAW_PATH.name} 已完整）")
        return V11_RAW_PATH
    all_sig = pd.concat(frames, ignore_index=True)
    all_sig = all_sig.drop_duplicates(subset=["symbol", "ts", "fi"], keep="last")
    all_sig.to_parquet(V11_RAW_PATH, index=False)
    print(f"[OK] v11_raw 合并 {len(all_sig)} 条 → {V11_RAW_PATH.name} "
          f"| 品种 {sorted(all_sig['symbol'].unique())} | 总耗时 {time.time()-t0:.0f}s", flush=True)
    return V11_RAW_PATH


# ===========================================================================
# Stage A2：稳定校准后处理（方案 A 池化增长池 + 方案 C 斜率符号检验）
# ===========================================================================
def _split_fold_cal_eval(fold_recs: pd.DataFrame, cal_split: float = CAL_SPLIT):
    """把单折原始信号按时间序切成 校准子窗(前 cal_split) / 评估子窗(后 1-cal_split)。

    与 ``_calibrate_and_split`` 的 k = int(len(sigs) * cal_split) 逐字节一致：
    worker 内 predict 返回时间序，故按 ts 排序后取前 k 为校准子窗。
    """
    g = fold_recs.sort_values("ts").reset_index(drop=True)
    k = int(len(g) * cal_split)
    k = max(1, min(k, len(g) - 1))
    return g.iloc[:k], g.iloc[k:]


def _platt_pooled(p_raw: np.ndarray, y_true: np.ndarray) -> PlattScaler:
    """在池化样本上拟合单一 Platt 校准器（hexbroker PlattScaler，NLL 梯度下降）。"""
    p = np.clip(np.asarray(p_raw, dtype=float), 1e-3, 1 - 1e-3)
    y = np.asarray(y_true, dtype=float)
    scaler = PlattScaler()
    try:
        scaler.fit(p, y)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Platt 拟合失败，回退 identity：{exc}")
        return PlattScaler(a=1.0, b=0.0)
    return scaler


def fit_growing_pool_calibration(
    raw: pd.DataFrame,
    realized: pd.Series,
    min_pool: int = MIN_POOL,
    cal_split: float = CAL_SPLIT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """因果增长池校准：逐品种、逐折（时间序）拟合单池 Platt 并应用到评估子窗。

    返回 (eval_df, slopes_df)：
    - eval_df：评估子窗信号（symbol/ts/p_up_cal/p_up_raw/exp_ret/cal_pool_n/
      cal_slope/cal_intercept/is_effective_cal）——与 v8 的评估子窗覆盖一致。
    - slopes_df：每品种每折校准器元数据（pool_n/slope/intercept/n_cal/n_pos），
      供方案 C 斜率符号稳定性检验。

    因果保证：折 i 的校准池 = 折 0..i 的校准子窗（全部早于折 i 评估子窗），
    不使用任何未来信息；池样本量随折单调增长（~15 → 450+）。
    """
    raw = raw.copy()
    raw["ts"] = pd.to_datetime(raw["ts"])
    raw["realized"] = realized.reindex(pd.MultiIndex.from_arrays(
        [raw["symbol"], raw["ts"]])).to_numpy()
    raw["y"] = (raw["realized"] > 0).astype(float)
    raw["_valid"] = raw["realized"].notna()

    eval_parts: list[pd.DataFrame] = []
    slope_rows: list[dict] = []
    for sym, sym_g in raw.groupby("symbol"):
        pool_p: list[np.ndarray] = []
        pool_y: list[np.ndarray] = []
        for fi, fold_recs in sym_g.groupby("fi"):
            cal_recs, eval_recs = _split_fold_cal_eval(fold_recs, cal_split)
            # 增长池：追加本折校准子窗的有效样本
            cal_valid = cal_recs[cal_recs["_valid"]]
            if len(cal_valid):
                pool_p.append(cal_valid["p_up"].to_numpy(dtype=float))
                pool_y.append(cal_valid["y"].to_numpy(dtype=float))
            pool_p_all = np.concatenate(pool_p) if pool_p else np.array([])
            pool_y_all = np.concatenate(pool_y) if pool_y else np.array([])

            if len(pool_p_all) >= min_pool:
                scaler = _platt_pooled(pool_p_all, pool_y_all)
                slope = float(scaler.a)
                intercept = float(scaler.b)
                pool_n = int(len(pool_p_all))
                p_cal = np.clip(scaler.transform(eval_recs["p_up"].to_numpy(dtype=float)),
                                1e-6, 1 - 1e-6)
            else:
                slope, intercept, pool_n = np.nan, np.nan, int(len(pool_p_all))
                p_cal = eval_recs["p_up"].to_numpy(dtype=float)  # identity（池不足）
            out = eval_recs[["symbol", "ts", "p_up", "exp_ret"]].copy()
            out["p_up_cal"] = p_cal
            out["cal_pool_n"] = pool_n
            out["cal_slope"] = slope
            out["cal_intercept"] = intercept
            out["is_effective_cal"] = np.abs(p_cal - 0.5) > 0.05
            eval_parts.append(out)
            slope_rows.append({
                "symbol": sym, "fi": int(fi), "pool_n": pool_n,
                "slope": slope, "intercept": intercept,
                "n_cal": int(len(cal_valid)),
                "n_pos": int((cal_valid["y"] > 0).sum()),
                "first_ts": str(cal_recs["ts"].min()) if len(cal_recs) else "",
                "last_ts": str(cal_recs["ts"].max()) if len(cal_recs) else "",
            })
    eval_df = pd.concat(eval_parts, ignore_index=True).sort_values(["symbol", "ts"])
    slopes = pd.DataFrame(slope_rows)
    return eval_df, slopes


def build_v11_cache(
    n_jobs: int = 1,
    only_groups: str = "",
    resume: bool = True,
) -> Path:
    """把 v11_raw 做稳定校准后处理 → v11_cal.parquet（含真实 p_up）。"""
    t0 = time.time()
    print("=" * 88)
    print(f"[Stage A2] v11 稳定校准（增长池 pooled Platt，min_pool={MIN_POOL}）")
    print("=" * 88, flush=True)
    if not V11_RAW_PATH.exists():
        print(f"[FAIL] 缺少 {V11_RAW_PATH.name}，先运行 --train-v11")
        return V11_CAL_PATH
    raw = pd.read_parquet(V11_RAW_PATH)
    raw["ts"] = pd.to_datetime(raw["ts"])
    prices = load_prices()
    realized = realized_returns(prices)
    print(f"  raw 信号 {len(raw)} 条 | 品种 {sorted(raw['symbol'].unique())} | "
          f"realized {len(realized)} 行")

    eval_df, slopes = fit_growing_pool_calibration(raw, realized, min_pool=MIN_POOL)

    # 与 v8 对齐检查：评估子窗覆盖 + exp_ret 逐字节一致
    v8 = pd.read_parquet(V8_PATH)
    v8["ts"] = pd.to_datetime(v8["ts"])
    m = v8.merge(eval_df[["symbol", "ts", "exp_ret"]], on=["symbol", "ts"],
                 suffixes=("_v8", "_v11"))
    exp_match = float(np.isclose(m["exp_ret_v8"], m["exp_ret_v11"],
                                 rtol=1e-9, atol=1e-12).mean()) if len(m) else float("nan")
    print(f"  与 v8 对齐: 内连接 {len(m)} / v8 {len(v8)} | exp_ret 逐字节一致率 "
          f"{exp_match*100:.2f}%")

    out = eval_df.rename(columns={"p_up": "p_up_raw", "p_up_cal": "p_up"})
    out = out[["symbol", "ts", "p_up", "exp_ret", "is_effective_cal",
               "cal_pool_n", "cal_slope", "cal_intercept"]]
    out = out.rename(columns={"is_effective_cal": "is_effective"})
    out = out.sort_values(["symbol", "ts"]).reset_index(drop=True)
    ART.mkdir(exist_ok=True)
    out.to_parquet(V11_CAL_PATH, index=False)
    slopes.to_csv(OUT_SLOPES, index=False)
    print(f"[OK] v11_cal 落盘 {len(out)} 条 → {V11_CAL_PATH.name}")
    print(f"[OK] 校准器元数据 → {OUT_SLOPES.name} | 总耗时 {time.time()-t0:.0f}s")
    return V11_CAL_PATH


# ===========================================================================
# Stage B1：P15-2 sizing 方案（合约层修复）
# ===========================================================================
def engine_a_targets_cs_sized(
    prices: pd.DataFrame,
    top_k: float = TOP_K,
    min_symbols: int | None = MIN_SYMBOLS_S2,
    cache_path: Path | str | None = None,
    group_cap: float | None = None,
    group_map: dict[str, str] | None = None,
    score_col: str = "exp_ret",
    sizing: str = "int",
    notional_frac: float | None = None,
) -> pd.DataFrame:
    """引擎 A 截面 top_k 目标生成（sizing 参数化，合约层修复）。

    sizing：
      "int"        —— v8 基线：floor 整数手（NOTIONAL_FRAC=0.20），逐字节复现
                       ``engine_a_targets_cs``（直接委托，保证基线一致）。
      "frac"       —— 方案 2：float 手数 = 名义比例（BacktestEngine/SimBroker 支持
                       小数仓，float 持仓记账，见 hexbroker/backtest/broker.py）。
      "tradable"   —— 方案 1：剔除 floor 后 0 手的不可交易合约（raw_lots<1）后，
                       在可交易子集上重算 top_k 选择，再 floor 整数手。
      "notional035"—— 方案 3：NOTIONAL_FRAC 0.20→0.35（350k 名义/标的）后 floor 整数手。

    生产口径：group_cap/group_map 显式传入（P13 DISCREPANCY-1 教训），与
    ``engine_a_targets_cs`` 同解析规则（未传时读配置）。
    """
    if sizing == "int":
        return engine_a_targets_cs(
            prices, top_k, min_symbols, cache_path, group_cap, group_map, score_col
        )
    nf = notional_frac if notional_frac is not None else SIZING_NOTIONAL[sizing]
    notional = INITIAL_CAPITAL * nf
    group_map = _resolve_group_map(group_map)
    group_cap = _resolve_group_cap(group_cap)

    if sizing == "tradable":
        sig = _signal_frame(prices, cache_path, score_col, group_map)
        with np.errstate(invalid="ignore", divide="ignore"):
            raw_lots = notional / (sig["_px"] * sig["_mult"])
        sig["raw_lots"] = raw_lots.fillna(0.0)
        # 可交易合约集：raw_lots >= 1（floor 后 >=1 手）
        tradable = sig[sig["raw_lots"] >= 1.0].copy()
        sel = _cs_select(tradable, top_k, min_symbols, group_cap, group_map, score_col)
        sig["selected"] = False
        sig.loc[sel[sel].index, "selected"] = True   # 仅 sel=True 的行（sel 为 bool Series）
        sig["target"] = np.where(sig["selected"], sig["raw_lots"].astype(int), 0)
        return sig.set_index(["symbol", "ts"])[["target"]].sort_index()

    # frac / notional035：选择逻辑与 engine_a_selection 完全一致，仅 target 计算不同
    sel = engine_a_selection(
        prices, top_k, min_symbols, cache_path, group_cap, group_map, score_col
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = notional / (sel["_px"] * sel["_mult"])
    if sizing == "frac":
        sel["target"] = np.where(sel["selected"], raw_lots.fillna(0.0), 0.0)
    else:
        sel["target"] = np.where(sel["selected"], raw_lots.fillna(0.0).astype(int), 0)
    return sel.set_index(["symbol", "ts"])[["target"]].sort_index()


def _signal_frame(
    prices: pd.DataFrame,
    cache_path: Path | str | None,
    score_col: str,
    group_map: dict[str, str],
) -> pd.DataFrame:
    """读取信号缓存并对齐价格/乘数/分组（镜像 engine_a_selection 的前处理）。"""
    sig = pd.read_parquet(_resolve_cache_path(cache_path))
    sig["ts"] = pd.to_datetime(sig["ts"])
    if score_col not in sig.columns:
        raise ValueError(f"score_col={score_col!r} 不在信号缓存列中：{list(sig.columns)}")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(
        {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    )
    sig["group"] = sig["symbol"].map(lambda s: group_map.get(s, "other"))
    return sig


def _cs_select(
    sig: pd.DataFrame,
    top_k: float,
    min_symbols: int | None,
    group_cap: float | None,
    group_map: dict[str, str],
    score_col: str,
) -> pd.Series:
    """每日截面 top_k 选择（镜像 engine_a_selection 的核心逻辑，含 group_cap 风控）。

    输入 sig 必须已含 _px/_mult/group 列；返回与 sig 等长的 bool Series（selected）。
    """
    sig = sig.copy()
    sig["rank_pct"] = sig.groupby("ts")[score_col].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    base_cond = (sig["rank_pct"] >= 1.0 - top_k) & sig["_px"].notna()
    if min_symbols is not None:
        base_cond = base_cond & (sig["_day_cnt"] >= min_symbols)
    if group_cap is None:
        return base_cond
    cap = float(group_cap)
    if not (0.0 < cap < 1.0):
        raise ValueError(f"group_cap 必须在 (0,1) 区间，实际 {group_cap!r}")
    sig["selected"] = False
    elig_mask = sig["_px"].notna()
    if min_symbols is not None:
        elig_mask = elig_mask & (sig["_day_cnt"] >= min_symbols)
    elig = sig[elig_mask]
    for _ts, g in elig.groupby("ts"):
        g = g.sort_values(score_col, ascending=False)
        target_count = int((g["rank_pct"] >= 1.0 - top_k).sum())
        if target_count <= 0:
            continue
        sel_syms = _capped_selection(
            g["symbol"].tolist(), target_count, cap, group_map
        )
        sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    return sig["selected"]


# ===========================================================================
# Stage B2：重估（生产 cap 口径）+ 指标
# ===========================================================================
def load_cache(path: Path) -> pd.DataFrame:
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig


def cs_ic_on_score(sig: pd.DataFrame, realized: pd.Series, oos_start: str,
                   score_col: str) -> dict:
    """在指定打分列上计算截面 IC（cs_ic_summary 硬编码 exp_ret → 临时替换）。"""
    s = sig.copy()
    if score_col != "exp_ret":
        s["exp_ret"] = s[score_col]
    return cs_ic_summary(s, realized, oos_start)


def eval_one_row(
    cfg,
    cost,
    prices: pd.DataFrame,
    realized: pd.Series,
    ret_b: pd.Series,
    cache_path: Path,
    variant: str,
    select_col: str,
    sizing: str,
    group_cap: float | None,
    group_map: dict[str, str] | None,
    frac_oos_ret: float | None = None,
) -> list[dict]:
    """单 (variant, select_col, sizing) 的引擎 A S2 + 组合 A10/B90 行。

    frac_oos_ret：同 (cache, select_col) 的 frac 理想口径 OOS 复利（转化率分母）。
    """
    tgt_a = engine_a_targets_cs_sized(
        prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cache_path,
        group_cap=group_cap, group_map=group_map, score_col=select_col,
        sizing=sizing,
    )
    ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(cfg, cost, prices, tgt_a,
                                                           f"A-{variant}-{sizing}")
    sig = load_cache(cache_path)
    summ = cs_ic_on_score(sig, realized, OOS_START, select_col)
    oos_ic = summ["oos"]
    oos_ret_a = m_oos_a.total_return if m_oos_a else np.nan
    conv_ret = (oos_ret_a / frac_oos_ret) if (frac_oos_ret and pd.notna(oos_ret_a)
                                              and pd.notna(frac_oos_ret) and frac_oos_ret != 0) else np.nan
    rows = [{
        "variant": variant, "select_col": select_col, "sizing": sizing,
        "engine": "A-S2",
        "sharpe_full": m_a.sharpe, "ann_ret_full": m_a.annual_return,
        "maxdd_full": m_a.max_drawdown,
        "oos_sharpe": m_oos_a.sharpe if m_oos_a else np.nan,
        "oos_maxdd": m_oos_a.max_drawdown if m_oos_a else np.nan,
        "oos_ret": oos_ret_a,
        "conv_ret": conv_ret,
        "long_day_ratio": long_ratio, "oos_n": m_oos_a.n_bars if m_oos_a else 0,
        "oos_cs_ic": oos_ic["ic_mean"], "oos_cs_icir": oos_ic["icir"],
        "oos_cs_pos_ratio": oos_ic["pos_ratio"],
    }]
    for vt in (False, True):
        ra = ret_a.loc[ret_a.index.intersection(ret_b.index)]
        rb = ret_b.loc[ret_b.index.intersection(ret_a.index)]
        r = combo_stats_row(ra, rb, COMBO_W_A, vt)
        rows.append({
            "variant": variant, "select_col": select_col, "sizing": sizing,
            "engine": f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}" + ("-volY" if vt else "-volN"),
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"],
            "oos_sharpe": r["oos_sharpe"], "oos_maxdd": r["oos_maxdd"],
            "oos_ret": r["oos_ret"], "conv_ret": np.nan, "long_day_ratio": np.nan,
            "oos_n": r["oos_n"], "oos_cs_ic": oos_ic["ic_mean"],
            "oos_cs_icir": oos_ic["icir"], "oos_cs_pos_ratio": oos_ic["pos_ratio"],
        })
    print(f"  [{variant:<6} sel={select_col:<7} sizing={sizing:<10} A-S2] "
          f"OOS Sharpe={m_oos_a.sharpe:.3f} OOS 复利={oos_ret_a*100:+.2f}% "
          f"转化率={conv_ret*100 if pd.notna(conv_ret) else float('nan'):.0f}% "
          f"| OOS 截面IC={oos_ic['ic_mean']:+.4f}")
    return rows


def tradability_rows(
    prices: pd.DataFrame,
    cache_path: Path,
    select_col: str,
    sizing: str,
    group_cap: float | None,
    group_map: dict[str, str] | None,
) -> list[dict]:
    """高价合约（au0/cu0/i0/j0/sc0）在 OOS 的做多天数（target>0）。"""
    tgt = engine_a_targets_cs_sized(
        prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cache_path,
        group_cap=group_cap, group_map=group_map, score_col=select_col, sizing=sizing,
    )
    rows = []
    tgt_df = tgt.reset_index()
    tgt_df["ts"] = pd.to_datetime(tgt_df["ts"])
    oos = tgt_df[tgt_df["ts"] >= pd.Timestamp(OOS_START)]
    total_oos_days = int(oos["ts"].nunique())
    for sym in HIGH_PRICE_SYMS:
        sub = oos[oos["symbol"] == sym]
        n_long = int((sub["target"] > 0).sum())
        n_days = int(sub[sub["target"] > 0]["ts"].nunique())   # 实际做多天数
        rows.append({
            "variant": str(cache_path.stem).replace("signals_cache18_grouped_", ""),
            "select_col": select_col, "sizing": sizing, "symbol": sym,
            "oos_long_rows": n_long, "oos_long_days": n_days,
            "oos_total_days": total_oos_days,
        })
    return rows


# ===========================================================================
# Stage B3：稳定校准验证（p15_cal_verify.csv）
# ===========================================================================
def verify_stable_calibration(
    v8: pd.DataFrame,
    v10_cal: pd.DataFrame,
    v11: pd.DataFrame,
    realized: pd.Series,
) -> pd.DataFrame:
    """p_up 稳定校准验证表：rank corr / IS/OOS 截面 IC / |IS-OOS| 稳定性。

    行 = 校准变体（v8_raw / v10_cal / v11_grow）；v11_is_global 与 v11_is_symbol
    为附加稳健性诊断（IS 期增长池拟合单一/分品种校准器、OOS 因果）。
    """
    rows = []
    variants = [
        ("v8_raw", v8, "p_up"),
        ("v10_cal", v10_cal, "p_up"),
        ("v11_grow", v11, "p_up"),
    ]
    # 附加诊断：IS-only 池（ts < OOS_START）拟合 → 应用到全部评估子窗
    for name, sig, score in variants + [
        ("v11_is_global", v11, "p_up"),
        ("v11_is_symbol", v11, "p_up"),
    ]:
        s = sig.copy()
        s["ts"] = pd.to_datetime(s["ts"])
        if name in ("v11_is_global", "v11_is_symbol"):
            # 用 v11 缓存自带元数据构造 IS-only 校准变体（诊断）
            s = _apply_is_pooled_calibration(s, realized, per_symbol=(name == "v11_is_symbol"))
        rc = float(s["p_up"].corr(s["exp_ret"], method="spearman"))
        summ = cs_ic_on_score(s, realized, OOS_START, score)
        is_ic = summ["is"]["ic_mean"]
        oos_ic = summ["oos"]["ic_mean"]
        rows.append({
            "calibration": name,
            "rank_corr_pup_exp": rc,
            "ic_is": is_ic,
            "ic_oos": oos_ic,
            "abs_is_oos_diff": abs(is_ic - oos_ic),
            "ic_full": summ["full"]["ic_mean"],
            "oos_icir": summ["oos"]["icir"],
            "oos_pos_ratio": summ["oos"]["pos_ratio"],
            "oos_n_days": summ["oos"]["n_days"],
        })
    return pd.DataFrame(rows)


def _apply_is_pooled_calibration(sig: pd.DataFrame, realized: pd.Series,
                                 per_symbol: bool) -> pd.DataFrame:
    """诊断：只用 IS 期（ts < OOS_START）信号拟合 Platt，应用到全部信号。

    - per_symbol=False：全品种单一校准器（方案 A 纯池化）。
    - per_symbol=True：每品种独立校准器。
    该变体只用 IS 期信息 → OOS 评估因果、零 OOS 泄漏；作为 v11 增长池主口径的稳健性对照。
    """
    s = sig.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    s["realized"] = realized.reindex(pd.MultiIndex.from_arrays(
        [s["symbol"], s["ts"]])).to_numpy()
    is_data = s[(s["ts"] < pd.Timestamp(OOS_START)) & s["realized"].notna()]

    def _fit_and_apply(p_fit: np.ndarray, y_fit: np.ndarray,
                       p_apply: np.ndarray) -> np.ndarray:
        if len(p_fit) < MIN_POOL:
            return p_apply
        sc = _platt_pooled(p_fit, y_fit)
        return np.clip(sc.transform(p_apply), 1e-6, 1 - 1e-6)

    if per_symbol:
        out = []
        for sym, g in s.groupby("symbol"):
            g = g.copy()
            gis = is_data[is_data["symbol"] == sym]
            p_fit = gis["p_up"].to_numpy(dtype=float)
            y_fit = (gis["realized"] > 0).to_numpy(dtype=float)
            p_apply = g["p_up"].to_numpy(dtype=float)
            g["p_up"] = _fit_and_apply(p_fit, y_fit, p_apply)
            out.append(g)
        return pd.concat(out)
    p_fit = is_data["p_up"].to_numpy(dtype=float)
    y_fit = (is_data["realized"] > 0).to_numpy(dtype=float)
    s["p_up"] = _fit_and_apply(p_fit, y_fit, s["p_up"].to_numpy(dtype=float))
    return s


# ===========================================================================
# 最终裁决
# ===========================================================================
def make_verdict(cal_verify: pd.DataFrame, sizing: pd.DataFrame,
                 slopes: pd.DataFrame) -> dict:
    a = sizing[sizing["engine"] == "A-S2"]
    combo = sizing[sizing["engine"] == f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}-volN"]
    base_a = a[(a["variant"] == "v8") & (a["select_col"] == "exp_ret") & (a["sizing"] == "int")].iloc[0]
    base_combo = combo[(combo["variant"] == "v8") & (combo["select_col"] == "exp_ret")
                       & (combo["sizing"] == "int")].iloc[0]

    # ---- p_up 稳定校准裁决 ----
    cv = cal_verify.set_index("calibration")
    v10 = cv.loc["v10_cal"]
    v11g = cv.loc["v11_grow"]
    slope_stats = {}
    if len(slopes) and slopes["slope"].notna().any():
        sl = slopes[slopes["slope"].notna()].copy()
        sl["ts"] = pd.to_datetime(sl["last_ts"])
        oos_sl = sl[sl["ts"] >= pd.Timestamp(OOS_START)]
        slope_stats = {
            "n_calibrators": int(len(sl)),
            "pos_slope_frac_all": float((sl["slope"] > 0).mean()),
            "pos_slope_frac_oos": float((oos_sl["slope"] > 0).mean()) if len(oos_sl) else np.nan,
            "n_oos_calibrators": int(len(oos_sl)),
            "median_pool_n": float(sl["pool_n"].median()),
            "median_pool_n_oos": float(oos_sl["pool_n"].median()) if len(oos_sl) else np.nan,
        }
    # 方案 C：斜率符号翻转次数（相邻折符号变化）
    n_flips = 0
    if len(slopes) and slopes["slope"].notna().any():
        for sym, g in slopes[slopes["slope"].notna()].sort_values("fi").groupby("symbol"):
            signs = np.sign(g["slope"].to_numpy())
            n_flips += int((signs[1:] != signs[:-1]).sum())
    slope_stats["n_sign_flips"] = n_flips

    pup_improved = bool(v11g["ic_oos"] > v10["ic_oos"])
    pup_stable = bool(v11g["abs_is_oos_diff"] < v10["abs_is_oos_diff"])
    pup_positive_stable = bool(v11g["ic_oos"] > 0 and pup_stable)
    pup_verdict = (
        "PUP_CONFIRMED" if pup_positive_stable else
        "PUP_NO_EDGE" if (v11g["ic_oos"] <= 0 or not pup_stable) else
        "PUP_UNCLEAR"
    )
    pup_reason = (
        f"稳定校准(v11_grow) p_up OOS 截面 IC={v11g['ic_oos']:+.4f}（vs v10_cal 伪影 "
        f"{v10['ic_oos']:+.4f}），|IS-OOS|={v11g['abs_is_oos_diff']:.4f}（vs "
        f"{v10['abs_is_oos_diff']:.4f}）；rank corr p_up-vs-exp_ret={v11g['rank_corr_pup_exp']:.3f}"
        f"（vs v10_cal {v10['rank_corr_pup_exp']:.3f}）。"
        f"斜率符号：OOS 正斜率占比 {slope_stats.get('pos_slope_frac_oos', float('nan'))*100:.0f}%"
        f"（池中位 {slope_stats.get('median_pool_n_oos', float('nan')):.0f}），翻转 {n_flips} 次。"
    )

    # ---- sizing 裁决 ----
    best_rows = {}
    for variant, sel in [("v8", "exp_ret"), ("v8", "p_up"), ("v11", "exp_ret"), ("v11", "p_up")]:
        sub = a[(a["variant"] == variant) & (a["select_col"] == sel)]
        if sub.empty:
            continue
        best = sub.sort_values("oos_sharpe", ascending=False).iloc[0]
        best_rows[f"{variant}__{sel}"] = best
    # 主推荐：v11 p_up 选择下最优 sizing（若存在）
    keys = ["v11__p_up", "v8__exp_ret", "v11__exp_ret", "v8__p_up"]
    rec_key = next((k for k in keys if k in best_rows), None)
    rec = best_rows[rec_key]
    base_sh = base_a["oos_sharpe"]
    sizing_verdict = {
        "recommended": {
            "variant_select": rec_key,
            "sizing": rec["sizing"],
            "oos_sharpe": rec["oos_sharpe"],
            "oos_ret": rec["oos_ret"],
            "conv_ret": rec["conv_ret"],
        },
        "baseline_int": {"variant": "v8", "select_col": "exp_ret", "sizing": "int",
                         "oos_sharpe": base_sh, "oos_ret": base_a["oos_ret"]},
        "combo_baseline": {"variant": "v8", "select_col": "exp_ret", "sizing": "int",
                           "oos_sharpe": base_combo["oos_sharpe"]},
    }

    print("=" * 96)
    print("P15 最终裁决（复利口径；基线 v8/int/exp_ret）")
    print(f"  单引擎 OOS Sharpe 基线 {base_sh:+.3f} | 组合 A10/B90 OOS 基线 "
          f"{base_combo['oos_sharpe']:.3f}")
    print(f"  [p_up] {pup_verdict}: {pup_reason}")
    print(f"  [sizing] 推荐 {rec_key} + sizing={rec['sizing']}: "
          f"OOS Sharpe {rec['oos_sharpe']:.3f} OOS 复利 {rec['oos_ret']*100:+.2f}% "
          f"转化率 {rec['conv_ret']*100 if pd.notna(rec['conv_ret']) else float('nan'):.0f}%")
    for k, r in best_rows.items():
        print(f"    {k:<14} best_sizing={r['sizing']:<10} OOS Sharpe={r['oos_sharpe']:.3f} "
              f"OOS 复利={r['oos_ret']*100:+.2f}% 转化率="
              f"{r['conv_ret']*100 if pd.notna(r['conv_ret']) else float('nan'):.0f}%")

    # 生产引擎改善判断：推荐配置（best sizing per 主选择）必须超越 v8/int/exp_ret 基线
    # （p_up 微弱正 IC 是真实诊断发现，但若无法转化为引擎收益，则不构成生产采纳）。
    engine_improved = bool(rec["oos_sharpe"] > base_sh)
    is_pass = bool(engine_improved)
    sizing_reason = (
        f"推荐 {rec_key} + sizing={rec['sizing']}: OOS Sharpe {rec['oos_sharpe']:.3f}"
        f"{' > ' if engine_improved else ' <= '}基线 {base_sh:.3f}"
        f"（转化率 {rec['conv_ret']*100 if pd.notna(rec['conv_ret']) else float('nan'):.0f}%）"
        f"→ {'采纳' if engine_improved else '不采纳（基线 v8/int/exp_ret 保留）'}"
    )
    print(f"  [sizing] {sizing_reason}")
    return {
        "is_pass": is_pass,
        "p_up": {
            "verdict": pup_verdict,
            "reason": pup_reason,
            "cal_verify": {k: (float(v) if pd.notna(v) else None)
                           for k, v in v11g.items()},
            "v10_cal_artifact": {k: (float(v) if pd.notna(v) else None)
                                 for k, v in v10.items()},
            "slope_stats": {k: (float(v) if pd.notna(v) else None)
                            for k, v in slope_stats.items()},
        },
        "sizing": sizing_verdict,
        "sizing_reason": sizing_reason,
        "reason": (f"p_up={pup_verdict}（微弱正 IC 为真实发现，但未转化引擎收益）；"
                   f"{sizing_reason}"),
    }


# ===========================================================================
# 主流程
# ===========================================================================
def run_eval() -> None:
    t0 = time.time()
    print("=" * 96)
    print("P15 Stage B：稳定校准验证 + sizing 方案矩阵 + 最终裁决")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"生产 cap（显式 base.yaml + group_cap/group_map）")
    print("=" * 96, flush=True)

    need = {"v8": V8_PATH, "v10_cal": V10_CAL_PATH, "v11": V11_CAL_PATH}
    missing = [str(p) for n, p in need.items() if not p.exists()]
    if missing:
        print(f"[FAIL] 缺少缓存：{missing}")
        return

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

    # ---- 1) 稳定校准验证 ----
    print("-" * 96)
    print("[1] p_up 稳定校准验证（v8_raw / v10_cal / v11_grow + IS-only 诊断）")
    v8 = load_cache(V8_PATH)
    v10_cal = load_cache(V10_CAL_PATH)
    v11 = load_cache(V11_CAL_PATH)
    cal_verify = verify_stable_calibration(v8, v10_cal, v11, realized)
    ART.mkdir(exist_ok=True)
    cal_verify.to_csv(OUT_CAL_VERIFY, index=False)
    print(cal_verify.round(4).to_string(index=False))
    print(f"  [OK] → {OUT_CAL_VERIFY}")

    # 斜率符号稳定性（方案 C）
    slopes = pd.read_csv(OUT_SLOPES) if OUT_SLOPES.exists() else pd.DataFrame()
    if len(slopes):
        sl = slopes[slopes["slope"].notna()].copy()
        print(f"  校准器元数据: 共 {len(sl)} 折（池>=MIN_POOL），正斜率占比 "
              f"{100*(sl['slope']>0).mean():.0f}%，池样本中位 {sl['pool_n'].median():.0f}")

    # ---- 2) sizing 矩阵 ----
    print("-" * 96)
    print("[2] sizing 方案矩阵（4 缓存×选择变量 × 4 sizing；生产 cap 口径）")
    variants = [
        ("v8", V8_PATH, "exp_ret"),
        ("v8", V8_PATH, "p_up"),
        ("v11", V11_CAL_PATH, "exp_ret"),
        ("v11", V11_CAL_PATH, "p_up"),
    ]
    all_rows: list[dict] = []
    trad_rows: list[dict] = []
    for variant, path, select_col in variants:
        # 先算 frac 理想口径 OOS 复利（转化率分母）
        frac_tgt = engine_a_targets_cs_sized(
            prices, TOP_K, MIN_SYMBOLS_S2, cache_path=path, group_cap=group_cap,
            group_map=group_map, score_col=select_col, sizing="frac",
        )
        _r, _e, _m, _m_oos, _lr = run_engine_row(cfg, cost, prices, frac_tgt, "A-frac")
        frac_oos_ret = _m_oos.total_return if _m_oos else np.nan
        print(f"  [{variant} sel={select_col:<7}] frac 理想口径 OOS 复利 "
              f"{frac_oos_ret*100:+.2f}%（转化率分母）")
        for sizing in SIZINGS:
            all_rows.extend(eval_one_row(
                cfg, cost, prices, realized, ret_b, path, variant, select_col, sizing,
                group_cap, group_map, frac_oos_ret=frac_oos_ret,
            ))
            trad_rows.extend(tradability_rows(
                prices, path, select_col, sizing, group_cap, group_map,
            ))
    tbl = pd.DataFrame(all_rows)
    tbl.to_csv(OUT_SIZING, index=False)
    trad = pd.DataFrame(trad_rows)
    trad.to_csv(OUT_TRADABILITY, index=False)
    print(f"  [OK] → {OUT_SIZING} ({len(tbl)} 行) | {OUT_TRADABILITY} ({len(trad)} 行)")
    # 高价合约可交易性恢复（实际做多天数）
    print("  高价合约 OOS 实际做多天数（au0/cu0/i0/j0/sc0，v8/exp_ret 口径）:")
    tv = trad[(trad["variant"] == "v8") & (trad["select_col"] == "exp_ret")]
    print(tv.pivot_table(index="symbol", columns="sizing", values="oos_long_days",
                         aggfunc="first").to_string())

    # 展示核心对比
    show = tbl[tbl["engine"] == "A-S2"].copy()
    show["oos_ret_pct"] = (show["oos_ret"] * 100).round(2)
    show["conv_pct"] = (show["conv_ret"] * 100).round(0)
    print(show[["variant", "select_col", "sizing", "oos_sharpe", "oos_ret_pct",
                "conv_pct", "oos_cs_ic"]].to_string(index=False))

    # ---- 3) 最终裁决 ----
    print("-" * 96)
    verdict = make_verdict(cal_verify, tbl, slopes)
    with open(OUT_VERDICT, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    print(f"[DONE] 总耗时 {time.time()-t0:.0f}s | 校准验证 → {OUT_CAL_VERIFY} | "
          f"sizing → {OUT_SIZING} | 裁决 → {OUT_VERDICT}")
    print("=" * 96)


def main() -> None:
    ap = argparse.ArgumentParser(description="P15 合约层修复（稳定校准复验 + 整数手 sizing）")
    ap.add_argument("--train-v11", action="store_true", help="Stage A1: v11 原始信号收集（后台）")
    ap.add_argument("--calibrate-v11", action="store_true", help="Stage A2: 稳定校准后处理 → v11_cal")
    ap.add_argument("--eval", action="store_true", help="Stage B: 校准验证 + sizing + 裁决")
    ap.add_argument("--n-jobs", type=int, default=8)
    ap.add_argument("--only-group", type=str, default="", help="只训练指定组（逗号分隔，冒烟）")
    args = ap.parse_args()

    t_start = time.time()
    print("=" * 96)
    print("P15 合约层修复：稳定校准复验（P15-1）+ 整数手 sizing 修复（P15-2）")
    print("=" * 96, flush=True)

    if args.train_v11:
        train_v11_raw(args.n_jobs, args.only_group)
    if args.calibrate_v11:
        build_v11_cache(args.n_jobs, args.only_group)
    if args.eval:
        run_eval()

    if not (args.train_v11 or args.calibrate_v11 or args.eval):
        print("[FAIL] 未指定 --train-v11 / --calibrate-v11 / --eval")
        return
    print(f"[P15 DONE] 总耗时 {time.time()-t_start:.0f}s")


if __name__ == "__main__":
    main()
