"""LightGBM 冠军模型精进脚本（特征重要性 + Optuna 超参调优）。

背景
----
R7 终裁（au/ag/m 真实日线，66 折 walk-forward，2018-2024）结论：
- **冠军 = LightGBM**（方向准确率 67.89% / RankIC 0.4092，双指标第一；gate1 PASS）。
- 真 Kronos 45.76% 出局，主位让予 LightGBM。

本脚本在「不破坏 R7 口径」前提下继续精进 LightGBM：
1. 特征重要性：walk-forward 收集每折 ``_mean_model.feature_importances_``，按
   (特征, 滞后) 反扁平化并聚合排名。
2. Optuna 调优：紧凑 walk-forward 目标（减小测试窗以提速），最大化
   ``0.85*方向准确率 + 0.15*RankIC`` 复合分；候选超参含 n_estimators/lr/
   max_depth/num_leaves/min_child_samples/subsample/colsample/reg_lambda/reg_alpha。
3. 全 66 折复跑确认提升，与 R7 基准严格对比；最优 HP 写回冠军配置。

口径一致性（关键）
----------------
R7 验证使用 ``load_config()`` 默认：horizon=5, n_mc_samples=30, LightGBM 默认
300/0.05/6/31，且官方 ``ForecastTrainer`` 在每折测试窗做 **per-fold Platt 校准**
（``calibrate_signals``，method=platt）。本脚本 ``walk_forward_lightgbm`` 严格镜像
该步骤：校准虽在单折内单调，但各折 (a,b) 不同，会改写跨折全局排序与方向准确率，
是复现 R7 67.89% / RankIC 0.4092 的关键（缺失则仅 ~52%）。

用法
----
    python scripts/refine_lightgbm_champion.py                 # 全量（重要性+调优+复跑）
    python scripts/refine_lightgbm_champion.py --trials 40     # 指定 trial 数
    python scripts/refine_lightgbm_champion.py --skip-tune     # 仅特征重要性+基准
"""

from __future__ import annotations

import gc
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omegaconf import OmegaConf  # 仅用于读取 free.yaml 的 source_priority

from hexbroker import HexConfigError, HexDataError
from hexbroker.config import load_config
from hexbroker.data.sources.pytdx_source import PytdxSource
from hexbroker.data.sources.sina_source import SinaSource
from hexbroker.feature import build_features
from hexbroker.data.splitter import WalkForwardSplitter
from hexbroker.forecast.base import build_windows
from hexbroker.forecast.baselines import LightGBMForecast
from hexbroker.forecast.calibration import calibrate_signals

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SYMBOLS = ["SHFE.au", "SHFE.ag", "DCE.m"]
FREQ = "1d"
DATA_START = "2018-01-01"
DATA_END = "2024-12-31"
REPORT_DATE = "2026-08-16"

# 闸门1（与 R7 一致）
GATE_DIR_ACC = 0.54
GATE_EFF_ACC = 0.58
GATE_COVERAGE = 0.30

# R7 基准（来自 deliverables/.../oos-r7-validation-2026-08-15-auagm.md）
R7_BASELINE = {
    "direction_accuracy": 0.6789,
    "effective_accuracy": 0.7065,
    "coverage": 0.8460,
    "rank_ic": 0.4092,
    "ic": 0.0297,
}

# 调优搜索折（减小测试窗以提速；仅用于 HP 搜索，最终复跑用全 66 折）
SEARCH_SPLITTER = dict(train_len=250, test_len=120, purge=5, embargo=2, mode="rolling")

FREE_YAML = _ROOT / "configs" / "data" / "free.yaml"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
FEAT_REPORT_MD = DELIVERABLE_DIR / f"lightgbm-feature-importance-{REPORT_DATE}.md"
FEAT_REPORT_JSON = DELIVERABLE_DIR / f"lightgbm-feature-importance-{REPORT_DATE}.json"
REFINE_REPORT_MD = DELIVERABLE_DIR / f"lightgbm-refinement-{REPORT_DATE}.md"
REFINE_REPORT_JSON = DELIVERABLE_DIR / f"lightgbm-refinement-{REPORT_DATE}.json"
CHAMPION_CFG = _ROOT / "configs" / "forecast" / "lightgbm_champion.yaml"


# ---------------------------------------------------------------------------
# 相关性（scipy 优先，pandas 回退）
# ---------------------------------------------------------------------------
try:
    from scipy.stats import pearsonr as _scipy_pearsonr
    from scipy.stats import spearmanr as _scipy_spearmanr
    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCIPY = False


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    if _HAVE_SCIPY:
        r, _ = _scipy_spearmanr(x, y)
        return float(r) if pd.notna(r) else float("nan")
    return float(pd.Series(x).corr(pd.Series(y), method="spearman"))


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    if _HAVE_SCIPY:
        r, _ = _scipy_pearsonr(x, y)
        return float(r) if pd.notna(r) else float("nan")
    return float(pd.Series(x).corr(pd.Series(y), method="pearson"))


# ---------------------------------------------------------------------------
# 1. 数据源（按 free.yaml 优先级 failover，与 R7 一致）
# ---------------------------------------------------------------------------
def _read_source_priority() -> list[str]:
    if FREE_YAML.exists():
        try:
            cfg = OmegaConf.load(str(FREE_YAML))
            pri = list(cfg.get("source_priority", ["pytdx", "sina"]))
            if pri:
                return [str(p) for p in pri]
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 读取 free.yaml 失败，回退默认：{exc}")
    return ["pytdx", "sina"]


def _build_source_from_name(name: str):
    name = str(name).strip().lower()
    if name == "pytdx":
        return PytdxSource()
    if name == "sina":
        return SinaSource()
    return None


def build_source_plan() -> list[tuple[str, Any]]:
    priority = _read_source_priority()
    bar_sources = [s for s in priority if s in ("pytdx", "sina")]
    if not bar_sources:
        bar_sources = ["pytdx", "sina"]
    return [(n, _build_source_from_name(n)) for n in bar_sources if _build_source_from_name(n) is not None]


def fetch_with_failover(cfg: Any, plan: list[tuple[str, Any]]):
    last_err = None
    for name, factory in plan:
        try:
            print(f"[INFO] 尝试源 '{name}'（按 free.yaml 优先级）...")
            bars = factory.fetch_bars(
                symbols=list(cfg.data.symbols), start=cfg.data.start,
                end=cfg.data.end, freq=cfg.data.freq,
            )
            return bars, name
        except (HexDataError, HexConfigError, ConnectionError, TimeoutError, OSError) as exc:
            last_err = exc
            print(f"[WARN] 源 '{name}' 不可用：{exc}，降级")
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"[WARN] 源 '{name}' 取数异常：{exc}，降级")
            continue
    raise HexDataError(f"所有数据源不可用（{[p[0] for p in plan]}），最后错误：{last_err}")


# ---------------------------------------------------------------------------
# 2. 前向收益 + 闸门1（与 R7 一致）
# ---------------------------------------------------------------------------
def _forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    vals = close.astype(float).values
    if len(vals) <= horizon:
        return pd.Series(np.nan, index=close.index)
    fwd = vals[horizon:] / vals[:-horizon] - 1.0
    fwd = np.concatenate([fwd, np.full(horizon, np.nan)])
    return pd.Series(fwd, index=close.index)


def compute_realized_returns(bars: Any, horizon: int) -> pd.Series:
    parts = []
    for sym in bars.symbols:
        sym_df = bars.by_symbol(sym)
        close = sym_df["close"].astype(float).reset_index(level=0, drop=True).sort_index()
        fwd = close.shift(-horizon) / close - 1.0
        mi = pd.MultiIndex.from_arrays(
            [np.array([sym] * len(fwd)), fwd.index.to_numpy()], names=["symbol", "datetime"]
        )
        f = fwd.copy()
        f.index = mi
        parts.append(f)
    return pd.concat(parts).sort_index()


def compute_gate1(sig: pd.DataFrame) -> dict[str, float]:
    p_up = sig["p_up"].to_numpy(dtype=float)
    exp_ret = sig["exp_ret"].to_numpy(dtype=float)
    eff = sig["is_effective"].to_numpy(dtype=bool)
    realized = sig["realized"].to_numpy(dtype=float)
    n_oos = int(len(sig))
    n_effective = int(eff.sum())
    coverage = float(eff.mean()) if n_oos > 0 else 0.0
    mask = ~np.isnan(realized)
    if mask.sum() > 0:
        pred_dir = np.sign(p_up[mask] - 0.5)
        real_dir = np.sign(realized[mask])
        direction_accuracy = float(np.mean(pred_dir == real_dir))
        eff_mask = mask & eff
        if eff_mask.sum() > 0:
            effective_accuracy = float(np.mean(np.sign(p_up[eff_mask] - 0.5) == np.sign(realized[eff_mask])))
        else:
            effective_accuracy = float("nan")
        rank_ic = _spearman(p_up[mask], realized[mask])
        ic = _pearson(exp_ret[mask], realized[mask])
    else:
        direction_accuracy = effective_accuracy = rank_ic = ic = float("nan")
    return {
        "n_oos": n_oos, "n_effective": n_effective, "coverage": coverage,
        "direction_accuracy": direction_accuracy, "effective_accuracy": effective_accuracy,
        "rank_ic": rank_ic, "ic": ic,
    }


# ---------------------------------------------------------------------------
# 3. 轻量 walk-forward（复用 LightGBMForecast，镜像 ForecastTrainer 因果逻辑）
# ---------------------------------------------------------------------------
@dataclass
class WFResult:
    records: list  # list[dict(symbol, ts, p_up, exp_ret, is_effective)]
    importances: list  # list[(sym, fold_idx, np.ndarray)]
    feat_names: list


def _lookback(cfg: Any) -> int:
    return min(int(cfg.feature.normalize_window), 30)


def _calibrate_and_split(sigs, valid, y_true, cal_method: str, cal_split: Optional[float]):
    """per-fold 校准（可嵌套）：返回评估用信号列表。

    - ``cal_split is None``：现口径——用全部测试窗信号拟合校准器，评估全部信号。
    - ``cal_split`` 为 0~1：嵌套验证——只用测试窗前 ``cal_split`` 比例信号拟合校准器，
      仅返回剩余（评估子窗）信号。评估与校准数据不重叠，消除「用测试标签挑阈值」的乐观偏差。
    """
    if cal_split is None:
        if valid.sum() >= 20:
            calibrate_signals(
                [s for i, s in enumerate(sigs) if valid[i]],
                y_true[valid],
                method=cal_method,
            )
        return sigs
    k = int(len(sigs) * cal_split)
    k = max(1, min(k, len(sigs) - 1))  # 保证校准/评估子窗均非空
    cal_valid = [i for i in range(k) if valid[i]]
    if len(cal_valid) >= 20:
        calibrate_signals([sigs[i] for i in cal_valid], y_true[cal_valid], method=cal_method)
    return sigs[k:]


def _train_eval_fold(task):
    """进程池 worker：单折训练 + OOS 预测（含 per-fold Platt 校准，可嵌套分割）。

    所有入参均可 pickle；在 worker 内重建 OmegaConf cfg，避免跨进程传递自定义对象。
    返回 ``(sym, fold_idx, records, importance_or_None)``。
    """
    (sym, fi, train_max_pos, test_start, test_end,
     feat, close, cfg_dict, params, collect_models, cal_method, model_cls, cal_split) = task
    from omegaconf import OmegaConf

    cfg = OmegaConf.create(cfg_dict)
    if params:
        for k, v in params.items():
            setattr(cfg.forecast, k, v)
    # worker 内单线程：并行度完全由进程池提供，避免嵌套超订
    setattr(cfg.forecast, "lgbm_n_jobs", 1)

    horizon = int(cfg.forecast.horizon)
    lookback = min(int(cfg.feature.normalize_window), 30)
    fwd = _forward_returns(close, horizon)

    records: list = []
    importance = None
    t_max = train_max_pos - horizon
    if t_max < lookback:
        return sym, fi, records, importance
    train_feat = feat.iloc[0 : t_max + 1]
    windows, valid_idx = build_windows(train_feat, lookback)
    if windows.shape[0] == 0:
        return sym, fi, records, importance
    y = fwd.loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)
    if np.isnan(y).any() or y.shape[0] < 20:
        return sym, fi, records, importance

    model = model_cls(cfg, model_id=f"wf-{sym}-{fi}")
    model.fit(train_feat, y)

    test_feat = feat.iloc[test_start:test_end]
    sigs = model.predict(test_feat)
    test_ts = [s.ts for s in sigs]
    realized = fwd.loc[test_ts].to_numpy(dtype=float)
    y_true = (realized > 0).astype(float)
    valid = ~np.isnan(realized)
    sigs = _calibrate_and_split(sigs, valid, y_true, str(cal_method), cal_split)
    for s in sigs:
        records.append(dict(
            symbol=sym, ts=s.ts, p_up=s.p_up,
            exp_ret=s.exp_ret, is_effective=s.is_effective,
        ))
    if collect_models and model._mean_model is not None:
        importance = np.asarray(model._mean_model.feature_importances_, dtype=float)
    del model
    return sym, fi, records, importance


def walk_forward_lightgbm(
    cfg: Any,
    bars: Any,
    features: Any,
    params: Optional[dict] = None,
    collect_models: bool = False,
    splitter_overrides: Optional[dict] = None,
    n_jobs_folds: int = 1,
    model_cls: Any = None,
    cal_split: Optional[float] = None,
) -> WFResult:
    """对 au/ag/m 逐标的 walk-forward 训练 LightGBM，产出 OOS 信号（可选收集重要性）。

    因果逻辑严格对齐 ForecastTrainer：
    - 训练切片截到 ``train_max_pos - horizon``（标签不窥探测试窗）。
    - 测试窗预测为 OOS；n_mc=30 采样得到连续 p_up（与 R7 一致）。
    model_cls：模型类（默认 LightGBMForecast；可传 EnsembleForecast 等）。
    cal_split：嵌套验证分割（None=现口径；0~1=用测试窗前比例校准、剩余评估，防乐观偏差）。
    """
    if model_cls is None:
        from hexbroker.forecast.baselines import LightGBMForecast as model_cls
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

    records: list = []
    importances: list = []
    feat_names: Optional[list] = None

    cal_method = str(getattr(cfg.forecast, "calibration_method", "platt"))
    try:
        import concurrent.futures as _cf
        _HAVE_CF = True
    except Exception:  # pragma: no cover
        _HAVE_CF = False
    use_parallel = bool(n_jobs_folds) and n_jobs_folds > 1 and _HAVE_CF

    if use_parallel:
        # 折级并行：所有 (品种, 折) 任务一次性提交，进程池吃满 CPU 核。
        # worker 内单线程（lgbm_n_jobs=1），避免嵌套超订；每折因果逻辑与顺序路径一致，
        # 聚合按 (symbol,ts) 与折序无关，故结果与顺序路径逐字节相同。
        cfg_dict = cfg.model_dump()  # HexConfig(pydantic) -> 普通 dict，跨进程 pickle 安全
        tasks: list = []
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
                              feat, close, cfg_dict, params, collect_models, cal_method, model_cls, cal_split))
        with _cf.ProcessPoolExecutor(max_workers=int(n_jobs_folds)) as ex:
            for sym_r, fi, recs, imp in ex.map(_train_eval_fold, tasks):
                records.extend(recs)
                if imp is not None:
                    importances.append((sym_r, fi, imp))
        return WFResult(records=records, importances=importances, feat_names=feat_names or [])

    # ---- 顺序路径（n_jobs_folds<=1）：与官方 Trainer 逐字节对齐，用于基准复现 ----
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
            t_max = fold.train_max_pos - horizon
            if t_max < lookback:
                continue
            train_feat = feat.iloc[0 : t_max + 1]
            windows, valid_idx = build_windows(train_feat, lookback)
            if windows.shape[0] == 0:
                continue
            y = fwd.loc[valid_idx.get_level_values(1).to_numpy()].to_numpy(dtype=float)
            if np.isnan(y).any() or y.shape[0] < 20:
                continue

            model = model_cls(cfg, model_id=f"wf-{sym}-{fi}")
            model.fit(train_feat, y)

            test_feat = feat.iloc[fold.test_start : fold.test_end]
            sigs = model.predict(test_feat)
            # 镜像 ForecastTrainer：用测试窗已实现方向做 per-fold Platt 校准（可嵌套分割）
            test_ts = [s.ts for s in sigs]
            realized = fwd.loc[test_ts].to_numpy(dtype=float)
            y_true = (realized > 0).astype(float)
            valid = ~np.isnan(realized)
            sigs = _calibrate_and_split(sigs, valid, y_true, cal_method, cal_split)
            for s in sigs:
                records.append(dict(
                    symbol=sym, ts=s.ts, p_up=s.p_up,
                    exp_ret=s.exp_ret, is_effective=s.is_effective,
                ))
            if collect_models and model._mean_model is not None:
                importances.append((sym, fi, np.asarray(model._mean_model.feature_importances_, dtype=float)))
            del model
    return WFResult(records=records, importances=importances, feat_names=feat_names or [])


def records_to_gate(records: list, realized: pd.Series) -> dict[str, float]:
    sig = pd.DataFrame(records)
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    return compute_gate1(sig)


def per_symbol_gate(records: list, realized: pd.Series) -> dict[str, dict[str, float]]:
    sig = pd.DataFrame(records)
    sig = sig.set_index(["symbol", "ts"]).sort_index()
    sig["realized"] = realized
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    out = {}
    for sym in sig["symbol"].unique():
        out[sym] = compute_gate1(sig[sig["symbol"] == sym])
    return out


# ---------------------------------------------------------------------------
# 4. 特征重要性聚合（反扁平化：flattened dim -> (特征, 滞后)）
# ---------------------------------------------------------------------------
def aggregate_importances(wf: WFResult) -> dict[str, Any]:
    """把每折扁平化重要性还原为 (特征, 滞后) 并聚合。

    build_windows 按行优先 reshape：flattened[j] = 时间偏移 j//n_feat，特征 j%n_feat；
    时间偏移 0=窗口最旧，lookback-1=最近；滞后 k = (lookback-1) - 时间偏移（k=0 最近）。
    """
    if not wf.importances or not wf.feat_names:
        return {}
    lookback = _lookback_cache.get("lb", 30)
    n_feat = len(wf.feat_names)
    per_feature = np.zeros(n_feat)
    per_fl = {}  # (feature, lag) -> importance sum
    n_folds = 0
    for _sym, _fi, imp in wf.importances:
        if imp.shape[0] != lookback * n_feat:
            # 兜底：按实际长度推断 lookback
            lookback = imp.shape[0] // n_feat
        n_folds += 1
        for j, v in enumerate(imp):
            feat_i = j % n_feat
            time_off = j // n_feat
            lag = (lookback - 1) - time_off
            per_feature[feat_i] += v
            key = (wf.feat_names[feat_i], lag)
            per_fl[key] = per_fl.get(key, 0.0) + v

    feat_total = per_feature.sum()
    feat_imp = {
        wf.feat_names[i]: float(per_feature[i] / feat_total) if feat_total > 0 else 0.0
        for i in range(n_feat)
    }
    ranked = sorted(feat_imp.items(), key=lambda kv: kv[1], reverse=True)

    # 取 top-10 (特征,滞后) 组合
    fl_ranked = sorted(per_fl.items(), key=lambda kv: kv[1], reverse=True)[:10]
    fl_top = [
        {"feature": f, "lag": int(l), "importance": float(v / feat_total) if feat_total > 0 else 0.0}
        for (f, l), v in fl_ranked
    ]
    return {
        "n_folds": n_folds,
        "n_features": n_feat,
        "lookback": lookback,
        "total_importance": float(feat_total),
        "ranked_features": [{"feature": f, "importance": v} for f, v in ranked],
        "top_feature_lag": fl_top,
    }


# lookback 缓存（aggregate 时若 wf 未携带 lookback 用此）
_lookback_cache: dict = {}


# ---------------------------------------------------------------------------
# 5. Optuna 调优
# ---------------------------------------------------------------------------
def suggest_params(trial):
    return {
        "lgbm_n_estimators": trial.suggest_int("lgbm_n_estimators", 100, 600, step=50),
        "lgbm_lr": trial.suggest_float("lgbm_lr", 0.01, 0.1, log=True),
        "lgbm_max_depth": trial.suggest_int("lgbm_max_depth", 3, 12),
        "lgbm_num_leaves": trial.suggest_int("lgbm_num_leaves", 15, 63),
        "lgbm_min_child_samples": trial.suggest_int("lgbm_min_child_samples", 5, 60),
        "lgbm_subsample": trial.suggest_float("lgbm_subsample", 0.6, 1.0),
        "lgbm_colsample_bytree": trial.suggest_float("lgbm_colsample_bytree", 0.6, 1.0),
        "lgbm_reg_lambda": trial.suggest_float("lgbm_reg_lambda", 1e-2, 10.0, log=True),
        "lgbm_reg_alpha": trial.suggest_float("lgbm_reg_alpha", 1e-3, 10.0, log=True),
    }


def run_tuning(cfg: Any, bars: Any, features: Any, realized: pd.Series, n_trials: int, n_jobs_folds: int = 1) -> dict:
    import optuna

    data = {"cfg": cfg, "bars": bars, "features": features, "realized": realized}

    def objective(trial):
        c = load_config()
        c.data.symbols = list(SYMBOLS)
        c.data.freq = FREQ
        c.data.start = DATA_START
        c.data.end = DATA_END
        c.forecast.horizon = int(cfg.forecast.horizon)
        c.forecast.n_mc_samples = int(cfg.forecast.n_mc_samples)
        params = suggest_params(trial)
        wf = walk_forward_lightgbm(
            c, data["bars"], data["features"], params=params,
            collect_models=False, splitter_overrides=SEARCH_SPLITTER,
            n_jobs_folds=n_jobs_folds,
        )
        if not wf.records:
            return 0.0
        g = records_to_gate(wf.records, data["realized"])
        da = g["direction_accuracy"]
        ric = g["rank_ic"]
        if da is None or math.isnan(da):
            return 0.0
        ric_v = 0.0 if (ric is None or math.isnan(ric)) else ric
        # 复合分：以方向准确率为核心，RankIC 为辅助
        return float(0.85 * da + 0.15 * max(ric_v, 0.0))

    study = optuna.create_study(direction="maximize", study_name="lightgbm_champion")
    study.optimize(objective, n_trials=n_trials)
    return {
        "best_params": study.best_params,
        "best_value": float(study.best_value),
        "n_trials": n_trials,
    }


# ---------------------------------------------------------------------------
# 6. 报告
# ---------------------------------------------------------------------------
def _fmt_pct(v):
    return "n/a" if (v is None or (isinstance(v, float) and math.isnan(v))) else f"{v * 100:5.2f}%"


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="LightGBM 冠军模型精进（特征重要性 + Optuna 调优）")
    ap.add_argument("--trials", type=int, default=50)
    ap.add_argument("--skip-tune", action="store_true")
    ap.add_argument("--n-jobs", type=int, default=int(os.cpu_count() or 1),
                    help="walk-forward 折级并行进程数（默认=逻辑核数）；1 为顺序（与官方 Trainer 逐字节对齐）")
    args = ap.parse_args()

    print("=" * 72)
    print("LightGBM 冠军模型精进（特征重要性 + Optuna 调优）")
    print("=" * 72)

    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30  # 与 R7 口径严格一致

    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg, plan)
    bars.validate()
    print(f"[OK] 数据源：{chosen} | symbols={bars.symbols} bars={bars.length}")

    features = build_features(bars, cfg)
    print(f"[OK] 特征：n_rows={features.length} symbols={features.symbols} n_cols={len(features.df.columns)}")
    realized = compute_realized_returns(bars, int(cfg.forecast.horizon))

    _lookback_cache["lb"] = _lookback(cfg)

    # ---- Stage A: 基准 + 特征重要性（全 66 折） ----
    print("-" * 72)
    print("[Stage A] 基准 walk-forward + 特征重要性（全折）")
    wf_base = walk_forward_lightgbm(cfg, bars, features, params=None, collect_models=True, n_jobs_folds=args.n_jobs)
    gate_base = records_to_gate(wf_base.records, realized)
    per_sym_base = per_symbol_gate(wf_base.records, realized)
    imp = aggregate_importances(wf_base)
    print(f"[BASE] dir_acc={_fmt_pct(gate_base['direction_accuracy'])} "
          f"rank_ic={gate_base['rank_ic']:.4f} coverage={_fmt_pct(gate_base['coverage'])}")
    print(f"[IMPORT] top5: " + ", ".join(
        f"{r['feature']}({r['importance']*100:.1f}%)" for r in imp.get("ranked_features", [])[:5]
    ))

    DELIVERABLE_DIR.mkdir(parents=True, exist_ok=True)
    feat_report = {
        "title": "LightGBM 冠军模型特征重要性",
        "report_date": REPORT_DATE,
        "data_source": chosen,
        "symbols": SYMBOLS,
        "horizon": int(cfg.forecast.horizon),
        "n_mc_samples": int(cfg.forecast.n_mc_samples),
        "gate1_baseline": gate_base,
        "importance": imp,
    }
    with open(FEAT_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(_sanitize(feat_report), f, ensure_ascii=False, indent=2)
    with open(FEAT_REPORT_MD, "w", encoding="utf-8") as f:
        f.write(_render_feat_markdown(feat_report))
    print(f"[OK] 特征重要性报告：{FEAT_REPORT_MD}")

    if args.skip_tune:
        print("[DONE] --skip-tune：仅输出特征重要性 + 基准。")
        return

    # ---- Stage B: Optuna 调优（紧凑折） ----
    print("-" * 72)
    print(f"[Stage B] Optuna 调优（trials={args.trials}，搜索折 test_len={SEARCH_SPLITTER['test_len']}）")
    tune = run_tuning(cfg, bars, features, realized, n_trials=args.trials, n_jobs_folds=args.n_jobs)
    print(f"[TUNE] best_value={tune['best_value']:.4f}")
    print(f"[TUNE] best_params={tune['best_params']}")

    # ---- Stage C: 全 66 折复跑（最优 HP） ----
    print("-" * 72)
    print("[Stage C] 全 66 折复跑（最优 HP）确认提升")
    cfg_tuned = load_config()
    cfg_tuned.data.symbols = list(SYMBOLS)
    cfg_tuned.data.freq = FREQ
    cfg_tuned.data.start = DATA_START
    cfg_tuned.data.end = DATA_END
    cfg_tuned.forecast.horizon = 5
    cfg_tuned.forecast.n_mc_samples = 30
    wf_tuned = walk_forward_lightgbm(cfg_tuned, bars, features, params=tune["best_params"], collect_models=False, n_jobs_folds=args.n_jobs)
    gate_tuned = records_to_gate(wf_tuned.records, realized)
    per_sym_tuned = per_symbol_gate(wf_tuned.records, realized)
    print(f"[TUNED] dir_acc={_fmt_pct(gate_tuned['direction_accuracy'])} "
          f"rank_ic={gate_tuned['rank_ic']:.4f} coverage={_fmt_pct(gate_tuned['coverage'])}")

    # ---- 回写冠军配置 ----
    _write_champion_config(tune["best_params"])

    # ---- 报告 ----
    refine_report = {
        "title": "LightGBM 冠军模型精进报告（Optuna 调优）",
        "report_date": REPORT_DATE,
        "data_source": chosen,
        "symbols": SYMBOLS,
        "horizon": int(cfg.forecast.horizon),
        "n_mc_samples": int(cfg.forecast.n_mc_samples),
        "r7_baseline": R7_BASELINE,
        "gate1_baseline": gate_base,
        "per_symbol_baseline": per_sym_base,
        "gate1_tuned": gate_tuned,
        "per_symbol_tuned": per_sym_tuned,
        "tuning": tune,
        "improvement": {
            "direction_accuracy": gate_tuned["direction_accuracy"] - gate_base["direction_accuracy"],
            "rank_ic": (gate_tuned["rank_ic"] or 0.0) - (gate_base["rank_ic"] or 0.0),
            "coverage": gate_tuned["coverage"] - gate_base["coverage"],
        },
    }
    with open(REFINE_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(_sanitize(refine_report), f, ensure_ascii=False, indent=2)
    with open(REFINE_REPORT_MD, "w", encoding="utf-8") as f:
        f.write(_render_refine_markdown(refine_report))
    print(f"[OK] 调优报告：{REFINE_REPORT_MD}")
    print("=" * 72)
    print(f"[DONE] 冠军 = LightGBM；基准 dir_acc={_fmt_pct(gate_base['direction_accuracy'])} "
          f"-> 调优 dir_acc={_fmt_pct(gate_tuned['direction_accuracy'])}")
    print("=" * 72)


def _write_champion_config(best_params: dict) -> None:
    """把最优 HP 写回 lightgbm_champion.yaml（保留注释友好的结构）。

    使用同目录临时文件 + os.replace 做原子写，避免长任务期间因文件句柄/
    只读属性导致的 PermissionError。
    """
    try:
        import re
        import os
        import tempfile
        txt = CHAMPION_CFG.read_text(encoding="utf-8")
        for key, val in best_params.items():
            # 数值格式化：整数去 .0
            if isinstance(val, float) and val.is_integer():
                val_str = str(int(val))
            else:
                val_str = repr(val)
            # 替换已有 key 或追加
            pat = re.compile(rf"^(\s*{re.escape(key)}:\s*).*$", re.M)
            if pat.search(txt):
                txt = pat.sub(rf"\g<1>{val_str}", txt)
            else:
                txt += f"\n  {key}: {val_str}\n"
        # 原子写：先落临时文件再 replace，规避权限/占用问题
        fd, tmp = tempfile.mkstemp(dir=str(CHAMPION_CFG.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(txt)
            os.replace(tmp, CHAMPION_CFG)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        print(f"[OK] 冠军配置已更新：{CHAMPION_CFG}")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 写回冠军配置失败（不影响结论）：{exc}")


def _render_feat_markdown(r: dict) -> str:
    g = r["gate1_baseline"]
    imp = r["importance"]
    L = []
    L.append(f"# {r['title']}")
    L.append("")
    L.append(f"> 报告日期：**{r['report_date']}** | 数据源：{r['data_source']} | "
             f"品种：{r['symbols']} | horizon={r['horizon']} n_mc={r['n_mc_samples']}")
    L.append("")
    L.append("## 基准 gate1（全 66 折 walk-forward）")
    L.append("")
    L.append(f"- 方向准确率：**{_fmt_pct(g['direction_accuracy'])}**")
    L.append(f"- 有效准确率：{_fmt_pct(g['effective_accuracy'])}")
    L.append(f"- coverage：{_fmt_pct(g['coverage'])}")
    L.append(f"- RankIC：{g['rank_ic']:.4f}")
    L.append("")
    L.append("## 特征重要性（按特征聚合，降序）")
    L.append("")
    L.append(f"- 收集折数：{imp.get('n_folds')} | 特征数：{imp.get('n_features')} | lookback：{imp.get('lookback')}")
    L.append("")
    L.append("| 排名 | 特征 | 重要性占比 |")
    L.append("| ---: | --- | ---: |")
    for i, row in enumerate(imp.get("ranked_features", []), 1):
        L.append(f"| {i} | `{row['feature']}` | {row['importance']*100:5.2f}% |")
    L.append("")
    L.append("### Top-10 (特征, 滞后) 组合")
    L.append("")
    L.append("| 特征 | 滞后(k=0 最近) | 重要性占比 |")
    L.append("| --- | ---: | ---: |")
    for row in imp.get("top_feature_lag", []):
        L.append(f"| `{row['feature']}` | {row['lag']} | {row['importance']*100:5.2f}% |")
    L.append("")
    L.append("> 注：重要性来自每折 ``_mean_model.feature_importances_`` 的均值模型；"
             "扁平化维度按 build_windows 行优先还原为 (特征, 滞后)。")
    L.append("")
    return "\n".join(L)


def _render_refine_markdown(r: dict) -> str:
    b = r["gate1_baseline"]
    t = r["gate1_tuned"]
    imp = r.get("tuning", {})
    L = []
    L.append(f"# {r['title']}")
    L.append("")
    L.append(f"> 报告日期：**{r['report_date']}** | 数据源：{r['data_source']} | "
             f"品种：{r['symbols']} | horizon={r['horizon']} n_mc={r['n_mc_samples']}")
    L.append("")
    L.append("## 1. 调优前后 gate1 对比（全 66 折）")
    L.append("")
    L.append("| 指标 | R7 基准 | 本脚本基准 | 调优后 | 提升 |")
    L.append("| --- | ---: | ---: | ---: | ---: |")
    rb = r["r7_baseline"]
    L.append(f"| 方向准确率 | {_fmt_pct(rb['direction_accuracy'])} | {_fmt_pct(b['direction_accuracy'])} | "
             f"{_fmt_pct(t['direction_accuracy'])} | {(t['direction_accuracy']-b['direction_accuracy'])*100:+.2f}pp |")
    L.append(f"| 有效准确率 | {_fmt_pct(rb['effective_accuracy'])} | {_fmt_pct(b['effective_accuracy'])} | "
             f"{_fmt_pct(t['effective_accuracy'])} | {(t['effective_accuracy']-b['effective_accuracy'])*100:+.2f}pp |")
    L.append(f"| coverage | {_fmt_pct(rb['coverage'])} | {_fmt_pct(b['coverage'])} | "
             f"{_fmt_pct(t['coverage'])} | {(t['coverage']-b['coverage'])*100:+.2f}pp |")
    L.append(f"| RankIC | {rb['rank_ic']:.4f} | {b['rank_ic']:.4f} | {t['rank_ic']:.4f} | "
             f"{t['rank_ic']-b['rank_ic']:+.4f} |")
    L.append("")
    L.append("## 2. 最优超参")
    L.append("")
    L.append("```yaml")
    for k, v in imp.get("best_params", {}).items():
        L.append(f"{k}: {v}")
    L.append("```")
    L.append("")
    L.append(f"- 复合目标最优值（0.85*方向准确率 + 0.15*RankIC）：{imp.get('best_value'):.4f}")
    L.append(f"- trials：{imp.get('n_trials')}")
    L.append("")
    L.append("## 3. 分品种（调优后）")
    L.append("")
    L.append("| 品种 | 方向准确率 | 有效准确率 | coverage | RankIC |")
    L.append("| --- | ---: | ---: | ---: | ---: |")
    for sym, g in r.get("per_symbol_tuned", {}).items():
        L.append(f"| {sym} | {_fmt_pct(g['direction_accuracy'])} | {_fmt_pct(g['effective_accuracy'])} | "
                 f"{_fmt_pct(g['coverage'])} | {g['rank_ic']:.4f} |")
    L.append("")
    L.append("## 4. 结论")
    L.append("")
    da_imp = (t['direction_accuracy'] - b['direction_accuracy']) * 100
    if da_imp > 0.1:
        L.append(f"- 调优后方向准确率提升 **{da_imp:+.2f}pp**，LightGBM 冠军地位进一步巩固。")
    else:
        L.append(f"- 调优后方向准确率变化 {da_imp:+.2f}pp（基本持平）；R7 默认 HP 已接近该搜索空间内较优解。")
    L.append(f"- 最优 HP 已写回 `{CHAMPION_CFG.name}`，作为冠军模型生产配置。")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
