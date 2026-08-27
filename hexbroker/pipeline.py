"""端到端研究管线（§3.7 / §8.9 T05）：一条命令出报告。

流程：数据 → 特征 → walk-forward 预测（OOS 信号落 SignalStore）→ 4 基线回测 →
RL（风控 in-the-loop）→ 进化（Optuna 预测/RL 超参 + EXAMM 神经进化 + 漂移检测）→
诊断（DSR/PBO + Q5 双闸门）→ 报告落盘。

用法::

    python -m hexbroker.pipeline --demo                      # 合成数据全链路（≤5 分钟）
    python -m hexbroker.pipeline --config configs/experiment/e01_cu_daily.yaml
    python -m hexbroker.pipeline --demo --skip-rl --skip-evolution
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .config import load_config
from .data.sources.csv_source import CsvSource
from .data.sources.synthetic_source import SyntheticSource
from .feature import build_features
from .forecast.signal_store import SignalStore
from .forecast.trainer import ForecastTrainer
from .utils.logging import init_logging, get_logger

_log = get_logger("PLN")


# ---------------------------------------------------------------------------
# 数据与信号
# ---------------------------------------------------------------------------
def _build_source(cfg, source: Optional[str]) -> Any:
    src = source or cfg.data.source
    if src == "synthetic":
        return SyntheticSource(n_bars=800, seed=int(cfg.seed))
    if src == "csv":
        return CsvSource()
    raise ValueError(f"未知数据源：{src}")


def _load_global_context(cfg, barframe) -> dict[str, pd.Series]:
    """按 feature.cross_params.global_codes 加载外盘数据并做时差安全对齐。

    无 global_codes 配置时返回空 dict（不加载、不报错）。
    """
    gcodes = (getattr(cfg.feature, "cross_params", {}) or {}).get("global_codes") or []
    if not gcodes:
        return {}
    from hexbroker.feature.global_ref import align_global_to_inner, load_global_close

    inner_dates = barframe.df.index.get_level_values("datetime").unique().sort_values()
    return {
        code: align_global_to_inner(load_global_close(code), inner_dates)
        for code in gcodes
    }


def _signal_eval(barframe, features, cfg, model_name: str, store_dir: str) -> tuple[pd.DataFrame, dict]:
    """walk-forward 训练并落 OOS 信号，返回 (signals_df, train_result_dict)。

    P0-3：train_result 追加 ``fingerprints``（SignalStore 四层指纹 sidecar 列表），
    trainer 内部已计算四层指纹并随 ``store.put`` 落盘。
    """
    store = SignalStore(store_dir)
    trainer = ForecastTrainer(cfg, store, model_name=model_name)
    result = trainer.run(barframe, features)
    signals = store.get_frame(model_id=result.model_id)
    return signals, {
        "model_id": result.model_id,
        "n_folds": result.n_folds,
        "n_oos_signals": result.n_oos_signals,
        "calibration_errors": [float(x) for x in result.calibration_errors],
        "fingerprints": store.fingerprints(model_id=result.model_id),
    }


def _prices_df(barframe) -> pd.DataFrame:
    df = barframe.df.copy()
    keep = [c for c in ["open", "high", "low", "close", "volume", "amount", "open_interest"] if c in df.columns]
    return df[keep]


def _contract_scale(cfg, prices: pd.DataFrame) -> int:
    """由本金/保证金/价格估算可持有最大合约数（用于基线与 RL 同杠杆对比）。"""
    btc = cfg.backtest
    px = prices["close"].astype(float)
    mean_px = float(px.mean()) if len(px) else 1000.0
    margin_per_ct = mean_px * btc.multiplier * btc.margin_rate
    return max(1, int(btc.initial_capital * cfg.risk.max_position_pct / max(margin_per_ct, 1e-9)))


def _forward_returns(prices: pd.DataFrame, horizon: int) -> pd.Series:
    """每个 bar 的未来 horizon 收益（仅用于评估信号有效性，不进入训练）。"""
    rows = []
    for sym in prices.index.get_level_values(0).unique():
        sub = prices.xs(sym, level=0)["close"].astype(float).sort_index()
        fwd = sub.shift(-horizon) / sub - 1.0
        fwd.index = pd.MultiIndex.from_product([[sym], fwd.index], names=["symbol", "datetime"])
        rows.append(fwd)
    return pd.concat(rows).sort_index()


# ---------------------------------------------------------------------------
# 信号质量（T02 闸门 1）
# ---------------------------------------------------------------------------
def _signal_metrics(signals: pd.DataFrame, fwd: pd.Series) -> dict:
    """方向准确率 / 有效信号准确率(带 coverage) / RankIC / Brier / 校准误差。"""
    cols = ["p_up", "vol_hat", "conf"]
    if "is_effective" in signals.columns:
        cols.append("is_effective")
    df = signals[cols].copy()
    df["y"] = (fwd.reindex(df.index) > 0).astype(float)
    df = df.dropna()
    if len(df) == 0:
        return {"dir_acc": 0.0, "eff_acc": 0.0, "coverage": 0.0, "rank_ic": 0.0, "brier": 0.25, "calib_err": 1.0, "n": 0}
    p = df["p_up"].clip(1e-6, 1 - 1e-6)
    y = df["y"]
    dir_acc = float(np.mean(((p - 0.5) > 0) == (y > 0.5)))
    eff = df["is_effective"] if "is_effective" in df.columns else (abs(p - 0.5) > 0.05)
    cov = float(eff.mean())
    eff_sub = df[eff]
    eff_acc = float(np.mean(((eff_sub["p_up"] - 0.5) > 0) == (eff_sub["y"] > 0.5))) if len(eff_sub) else 0.0
    ric = float(pd.Series(p.to_numpy()).corr(pd.Series(y.to_numpy()), method="spearman") or 0.0)
    brier = float(np.mean((p - y) ** 2))
    calib_err = float(abs(p.mean() - y.mean()))
    return {
        "dir_acc": dir_acc, "eff_acc": eff_acc, "coverage": cov,
        "rank_ic": ric, "brier": brier, "calib_err": calib_err, "n": int(len(df)),
    }


# ---------------------------------------------------------------------------
# 回测与基线
# ---------------------------------------------------------------------------
def _backtest(cfg, prices: pd.DataFrame, targets: pd.DataFrame) -> dict:
    from .backtest.engine import BacktestEngine
    from .evaluation.metrics import compute_metrics

    eng = BacktestEngine(cfg)
    pf = eng.run(prices, targets)
    eq = pf.equity_curve
    m = compute_metrics(eq, freq=str(cfg.data.freq))
    return {**m.to_dict(), "equity": eq}


def _run_baselines(cfg, prices, signals, scale: int) -> dict[str, dict]:
    from .evaluation.baseline import BaselineStrategy

    out = {}
    for name in ("buy_hold", "dual_ma", "macd", "signal_threshold"):
        kwargs = {"contract_scale": scale} if name != "signal_threshold" else {"contract_scale": scale}
        tgt = BaselineStrategy(name).generate(prices, signals, **kwargs)
        m = _backtest(cfg, prices, tgt)
        out[name] = {"metrics": m, "targets": tgt}
    return out


# ---------------------------------------------------------------------------
# RL（T04）
# ---------------------------------------------------------------------------
def _run_rl(cfg, prices, signals, total_steps: int, scale: int, reward_overrides: Optional[dict] = None) -> dict:
    from .rl.futures_env import FuturesTradingEnv, check_env, assert_env_has_no_model_dependency
    from .rl.train import train, policy_rollout

    assert_env_has_no_model_dependency(FuturesTradingEnv)
    symbols = list(signals.index.get_level_values(0).unique())
    target_frames: list[pd.DataFrame] = []
    stats = None
    t0 = time.time()
    for sym in symbols:
        env = FuturesTradingEnv(signals, prices, cfg, symbol=sym)
        if reward_overrides:
            for k, v in reward_overrides.items():
                setattr(env, k, float(v))
        check_env(env)
        policy, stats, env = train(env, cfg, total_timesteps=total_steps)
        tgt = policy_rollout(policy, env)
        if tgt is not None and len(tgt):
            target_frames.append(tgt)
    t_train = time.time() - t0

    if target_frames:
        targets = pd.concat(target_frames).sort_index()
    else:
        targets = pd.DataFrame(
            columns=["target"],
            index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "datetime"]),
        )
    # 训练-回测一致性：环境记录的目标合约数帧交给回测引擎重放
    bt = _backtest(cfg, prices, targets)
    # 与基线同杠杆对比：signal_threshold 用同一 scale（不再写死 1），口径可比
    thr = _run_baselines(cfg, prices, signals, scale=scale)["signal_threshold"]["metrics"]
    better = (
        bt["calmar"] > thr["calmar"] or abs(bt["max_drawdown"]) < abs(thr["max_drawdown"])
    )
    return {
        "metrics": bt,
        "stats": stats,
        "train_seconds": t_train,
        "better_than_threshold": bool(better),
        "targets": targets,
        "equity": bt.pop("equity"),
        "n_symbols": len(symbols),
    }

# ---------------------------------------------------------------------------
# 进化（T05）
# ---------------------------------------------------------------------------
def _run_evolution(cfg, signals, prices, fwd, rl_total_steps: int = 1500, scale: Optional[int] = None) -> dict:
    from .evolution.drift import DriftDetector
    from .evolution.examm_engine import evolve
    from .evolution.optuna_engine import run_forecast_optimization, run_rl_optimization

    out: dict[str, Any] = {}

    # --- 漂移检测（外层 C 触发条件）---
    det = DriftDetector(threshold=float(cfg.evolution.drift_psi_threshold), baseline_len=60)
    drift_events = []
    for sym in signals.index.get_level_values(0).unique():
        sub = signals.xs(sym, level=0)[["p_up", "vol_hat"]]
        evs = det.detect(sub)
        drift_events.extend([{"feature": e.feature, "psi": round(e.psi, 4), "triggered": e.triggered} for e in evs])
    out["drift"] = {"n_events": len(drift_events), "n_triggered": sum(1 for e in drift_events if e["triggered"]), "events": drift_events[:10]}

    # --- 内层 A：预测/阈值超参（在 OOS 信号上快速评估，不重训）---
    def _sig_obj(params, trial_number):
        thr = params.get("effective_threshold", 0.05)
        long_thr = params.get("long_thr", 0.5 + thr)
        short_thr = params.get("short_thr", 0.5 - thr)
        df = signals[["p_up", "conf"]].copy()
        df["y"] = (fwd.reindex(df.index) > 0).astype(float)
        df = df.dropna()
        p = df["p_up"].clip(1e-6, 1 - 1e-6)
        y = df["y"].to_numpy()
        eff = abs(p - 0.5) > thr
        sub = df[eff]
        da = float(np.mean(((sub["p_up"] - 0.5) > 0) == (sub["y"] > 0.5))) if len(sub) else 0.5
        ric = float(pd.Series(p.to_numpy()).corr(pd.Series(y), method="spearman") or 0.0)
        brier = float(np.mean((p.to_numpy() - y) ** 2))
        return {"dir_acc": da, "rank_ic": ric, "brier": brier}

    try:
        n_trials = max(3, int(cfg.evolution.n_trials) // 3)
        study = run_forecast_optimization(
            _sig_obj, n_trials=n_trials,
            storage="sqlite:///artifacts/optuna/forecast_study.db",
            seed=int(cfg.seed), sharpe_aware=bool(getattr(cfg.evolution, "sharpe_aware", False)),
        )
        out["optuna_forecast"] = {
            "n_trials": len(study.trials), "best_value": float(study.best_value),
            "best_params": study.best_params,
        }
    except Exception as e:  # 进化失败不阻断管线
        out["optuna_forecast"] = {"error": str(e)}

    # --- 内层 B：RL 奖励权重 NSGA-II（短训快速评估）---
    def _rl_obj(params, trial_number):
        try:
            res = _run_rl(cfg, prices, signals, total_steps=rl_total_steps, scale=scale, reward_overrides=params)
            m = res["metrics"]
            return {"sharpe": m["sharpe"], "win_rate": m["win_rate"], "max_drawdown": m["max_drawdown"]}
        except Exception:
            return {"sharpe": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}

    try:
        n_trials = max(2, min(5, int(cfg.evolution.n_trials) // 4))
        study2 = run_rl_optimization(
            _rl_obj, n_trials=n_trials,
            storage="sqlite:///artifacts/optuna/rl_study.db", seed=int(cfg.seed),
        )
        out["optuna_rl"] = {
            "n_trials": len(study2.trials),
            "best_values": [float(v) for v in study2.best_trials[0].values] if study2.best_trials else None,
            "best_params": dict(study2.best_trials[0].params) if study2.best_trials else None,
        }
    except Exception as e:
        out["optuna_rl"] = {"error": str(e)}

    # --- 外层 C：EXAMM 风格神经进化（小网络，<1M 参数）---
    try:
        df = signals[["p_up", "exp_ret", "vol_hat"]].dropna().head(600)
        seq = 10
        arr = df.to_numpy(dtype=float)
        arr = (arr - arr.mean(axis=0)) / (arr.std(axis=0) + 1e-8)
        X = np.stack([arr[i : i + seq] for i in range(len(arr) - seq)])
        y = np.array([float((p - 0.5) > 0) for p in df["p_up"].to_numpy()[seq:]])
        y = 2.0 * y - 1.0
        n = len(X)
        tr = slice(0, int(n * 0.7))
        va = slice(int(n * 0.7), n)
        res = evolve(
            X[tr], y[tr], X[va], y[va],
            n_generations=max(3, int(getattr(cfg.evolution, "examm_n_generations", 6))),
            n_islands=int(getattr(cfg.evolution, "examm_n_islands", 4)),
            in_dim=3, out_dim=1, seed=int(cfg.seed),
        )
        out["examm"] = {
            "best_fitness": round(float(res.best_fitness), 6) if res.best else None,
            "n_generations": res.n_generations,
            "n_params": res.n_params,
            "cell": res.best.cell if res.best else None,
            "island_migrations": res.island_migrations,
            "under_max_params": bool(res.n_params <= cfg.evolution.examm_max_params),
        }
    except Exception as e:
        out["examm"] = {"error": str(e)}

    return out


# ---------------------------------------------------------------------------
# 诊断与闸门（T05 闸门 2 / PBO / DSR）
# ---------------------------------------------------------------------------
def _pbo(cfg, prices, signals, n_splits: int = 20) -> float:
    """轻量 PBO：随机 train/test 切分下，策略测试 Sharpe 低于训练 Sharpe 的比例。"""
    try:
        fwd = _forward_returns(prices, int(cfg.forecast.horizon))
        df = signals[["p_up"]].copy()
        df["y"] = (fwd.reindex(df.index) > 0).astype(float)
        df = df.dropna()
        if len(df) < 80:
            return 0.5
        rng = np.random.default_rng(7)
        worse = 0
        for _ in range(n_splits):
            idx = np.arange(len(df))
            rng.shuffle(idx)
            cut = int(len(idx) * 0.6)
            for tag, part in (("tr", idx[:cut]), ("te", idx[cut:])):
                sub = df.iloc[part]
                # 用阈值策略 Sharpe 近似
                p = sub["p_up"].to_numpy()
                y = sub["y"].to_numpy()
                dirs = np.where(p > 0.55, 1.0, np.where(p < 0.45, -1.0, 0.0))
                rets = dirs * (2 * y - 1)  # 方向命中近似收益
                s = float(rets.mean() / (rets.std() + 1e-9) * np.sqrt(len(rets)))
                if tag == "tr":
                    s_tr = s
                else:
                    worse += int(s < s_tr)
        return float(worse / n_splits)
    except Exception:
        return 0.5


def _dsr(sharpe: float, n_obs: int, n_strategies: int) -> float:
    from .evaluation.stats import deflated_sharpe_ratio

    return deflated_sharpe_ratio(sharpe, n_obs, n_strategies)


def _gate_report(cfg, signals, fwd, baselines: dict, rl: Optional[dict], prices: pd.DataFrame) -> dict:
    sm = _signal_metrics(signals, fwd)
    gate1 = {
        "dir_acc_ge_54": bool(sm["dir_acc"] >= 0.54),
        "eff_acc_ge_58": bool(sm["eff_acc"] >= 0.58 and sm["coverage"] >= 0.30),
        "calib_lt_05": bool(sm["calib_err"] < 0.05),
    }
    gate1["pass"] = bool(all(gate1.values()))

    # 候选策略 = RL 与纯信号阈值中的较优者（RL 不优则诊断而不放行）
    use_rl = rl is not None and bool(rl.get("better_than_threshold", False))
    cand = rl if use_rl else {"metrics": baselines["signal_threshold"]["metrics"]}
    m = cand["metrics"]
    beats = {}
    for name, b in baselines.items():
        if name == "signal_threshold" and not use_rl:
            continue  # 候选本身就是信号阈值，不与自己比
        bm = b["metrics"]
        beats[name] = bool(m["sharpe"] > bm["sharpe"] and m["calmar"] > bm["calmar"])
    pbo = _pbo(cfg, prices, signals)
    dsr = _dsr(m["sharpe"], m["n_bars"], n_strategies=5)
    gate2 = {
        "sharpe": m["sharpe"], "calmar": m["calmar"], "max_drawdown": m["max_drawdown"],
        "beats_all_baselines": bool(all(beats.values()) if beats else False),
        "pbo_lt_05": bool(pbo < 0.5), "dsr_gt_0": bool(dsr > 0),
    }
    gate2["pass"] = bool(gate2["beats_all_baselines"] and gate2["pbo_lt_05"] and gate2["dsr_gt_0"])
    weak_band = bool(0.54 <= sm["dir_acc"] < 0.58)
    return {
        "signal": sm,
        "gate1": gate1,
        "gate2": gate2,
        "pbo": pbo,
        "dsr": dsr,
        "weak_signal_band": weak_band,
        "candidate": "rl" if use_rl else "signal_threshold",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_pipeline(cfg, *, source=None, model=None, store_dir=None, skip_rl=False,
                 skip_evolution=False, rl_steps: Optional[int] = None,
                 report_dir: Optional[str] = None,
                 enable_dual_caliber: bool = True) -> dict:
    t0 = time.time()
    init_logging("pipeline", Path("artifacts"))

    src = _build_source(cfg, source)
    barframe = src.fetch_bars(list(cfg.data.symbols), start=cfg.data.start, end=cfg.data.end, freq=cfg.data.freq)
    # 外盘上下文（时差安全对齐）——冠军配置含 spx/uup global 特征，生产路径必须注入
    global_close = _load_global_context(cfg, barframe)
    features = build_features(barframe, cfg, global_close=global_close)
    prices = _prices_df(barframe)
    model_name = model or cfg.forecast.name

    store_dir = store_dir or tempfile.mkdtemp(prefix="signals_")
    signals, train_info = _signal_eval(barframe, features, cfg, model_name, store_dir)
    fwd = _forward_returns(prices, int(cfg.forecast.horizon))

    scale = _contract_scale(cfg, prices)
    baselines = _run_baselines(cfg, prices, signals, scale)

    rl = None
    if not skip_rl and len(signals) >= 100:
        rl = _run_rl(cfg, prices, signals, total_steps=rl_steps or int(cfg.rl.total_timesteps), scale=scale)
    evolution = None
    if not skip_evolution:
        evolution = _run_evolution(cfg, signals, prices, fwd, scale=scale)
    gates = _gate_report(cfg, signals, fwd, baselines, rl, prices)

    # P0-1 双口径对照（Q2 裁决：默认输出进生产报告，--no-dual-caliber 可关；只增不改）
    dual_caliber = None
    if enable_dual_caliber:
        try:
            from .backtest.execution import run_dual_caliber

            use_rl_cand = rl is not None and bool(rl.get("better_than_threshold", False))
            cand_targets = (
                rl["targets"] if use_rl_cand else baselines["signal_threshold"]["targets"]
            )
            dual_caliber = run_dual_caliber(prices, cand_targets, cfg)
        except Exception as e:
            dual_caliber = {"error": str(e)}

    # P0-4 bootstrap 绩效区间（并列输出，不参与闸门判定；DSR/PBO 逻辑零改动）
    bootstrap = None
    try:
        from .evaluation.bootstrap import bootstrap_metrics_ci, bootstrap_report_block

        use_rl_cand = rl is not None and bool(rl.get("better_than_threshold", False))
        cand_eq = (
            rl["equity"] if use_rl_cand else baselines["signal_threshold"]["metrics"]["equity"]
        )
        bcfg = cfg.backtest.bootstrap
        boot_seed = bcfg.seed if bcfg.seed is not None else int(cfg.seed)
        boot_res = bootstrap_metrics_ci(
            cand_eq, freq=str(cfg.data.freq),
            block_len=int(bcfg.block_len), n_boot=int(bcfg.n_boot),
            seed=int(boot_seed), by_symbol=bool(bcfg.by_symbol),
        )
        bootstrap = bootstrap_report_block(boot_res)
    except Exception as e:
        bootstrap = {"error": str(e)}

    summary = {
        "run_id": _run_id(cfg),
        "config": cfg.model_dump(),
        "train": train_info,
        "signals": gates["signal"],
        "baselines": {k: {kk: vv for kk, vv in v["metrics"].items() if kk != "equity"} for k, v in baselines.items()},
        "rl": None if rl is None else {
            "metrics": {k: v for k, v in rl["metrics"].items() if k != "equity"},
            "stats": rl["stats"], "train_seconds": rl["train_seconds"],
            "better_than_threshold": rl["better_than_threshold"],
        },
        "evolution": evolution,
        "gates": gates,
        # P0 新增三块（既有 key 一律不动，旧消费方按既有 key 读取不受影响）
        "dual_caliber": dual_caliber,
        "fingerprints": train_info.get("fingerprints") or [],
        "bootstrap": bootstrap,
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    # 落盘报告
    rdir = Path(report_dir or "artifacts/reports")
    rdir.mkdir(parents=True, exist_ok=True)
    jp = rdir / f"report_{summary['run_id']}.json"
    jp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = _render_markdown(summary)
    mp = rdir / f"report_{summary['run_id']}.md"
    mp.write_text(md, encoding="utf-8")
    summary["report_path"] = str(mp)
    summary["json_path"] = str(jp)
    return summary


def _run_id(cfg) -> str:
    import datetime

    return f"{cfg.experiment}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _render_markdown(summary: dict) -> str:
    g = summary["gates"]
    s = g["signal"]
    lines = [
        "# HexFutures-AI 端到端研究报告",
        f"- run_id: `{summary['run_id']}` ｜ 耗时 {summary['elapsed_seconds']}s",
        "",
        "## 1. 信号有效性（T02 闸门 1）",
        f"- 方向准确率 dir_acc: **{s['dir_acc']*100:.2f}%** (需≥54%)",
        f"- 有效信号准确率 eff_acc: **{s['eff_acc']*100:.2f}%** (需≥58%, coverage {s['coverage']*100:.1f}% ≥30%)",
        f"- RankIC: {s['rank_ic']:.4f} ｜ Brier: {s['brier']:.4f} ｜ 校准误差: {s['calib_err']:.4f} (需<0.05)",
        f"- 闸门1: **{'✅ PASS' if g['gate1']['pass'] else '⛔ FAIL'}**（非噪声证明，非盈利承诺）",
        "",
        "## 2. 基线对比（含成本）",
    ]
    for name, m in summary["baselines"].items():
        lines.append(f"- {name}: Sharpe {m['sharpe']:.3f} ｜ Calmar {m['calmar']:.3f} ｜ MaxDD {m['max_drawdown']*100:.2f}% ｜ 胜率 {m['win_rate']*100:.1f}%")
    rl = summary.get("rl")
    if rl:
        m = rl["metrics"]
        lines += [
            "",
            "## 3. RL（风控 in-the-loop）",
            f"- Sharpe {m['sharpe']:.3f} ｜ Calmar {m['calmar']:.3f} ｜ MaxDD {m['max_drawdown']*100:.2f}% ｜ 胜率 {m['win_rate']*100:.1f}%",
            f"- 引擎: {rl['stats'].get('engine')} ｜ 训练 {rl['train_seconds']:.1f}s ｜ 优于纯信号阈值: {'✅' if rl['better_than_threshold'] else '⛔'}",
            f"- PPO 统计: {json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in rl['stats'].items() if k != 'extra'}, ensure_ascii=False)}",
        ]
    lines += [
        "",
        "## 4. 进化层",
        f"- 漂移事件: {summary['evolution'].get('drift', {}).get('n_events', 0)} 条, 触发 {summary['evolution'].get('drift', {}).get('n_triggered', 0)} 条" if summary.get("evolution") else "- 已跳过",
    ]
    if summary.get("evolution"):
        ef = summary["evolution"].get("optuna_forecast", {})
        er = summary["evolution"].get("optuna_rl", {})
        ex = summary["evolution"].get("examm", {})
        lines.append(f"- Optuna(预测): {ef.get('n_trials', 0)} trials, best={ef.get('best_value')}")
        lines.append(f"- Optuna(RL): {er.get('n_trials', 0)} trials, best={er.get('best_values')}")
        lines.append(f"- EXAMM: gen={ex.get('n_generations')}, params={ex.get('n_params')} (<1M: {ex.get('under_max_params')}), cell={ex.get('cell')}")
    lines += [
        "",
        "## 5. 可交付性（T05 闸门 2）",
        f"- 候选策略: **{g['candidate']}** ｜ Sharpe {g['gate2']['sharpe']:.3f} ｜ Calmar {g['gate2']['calmar']:.3f} ｜ MaxDD {g['gate2']['max_drawdown']*100:.2f}%",
        f"- 跑赢全部 4 基线: {'✅' if g['gate2']['beats_all_baselines'] else '⛔'} ｜ PBO {g['pbo']:.3f} (<0.5: {'✅' if g['pbo'] < 0.5 else '⛔'}) ｜ DSR {g['dsr']:.3f} (>0: {'✅' if g['dsr'] > 0 else '⛔'})",
        f"- 闸门2: **{'✅ PASS（可交付）' if g['gate2']['pass'] else '⛔ FAIL（不可交付）'}**",
        f"- 弱信号带(54–58%): {'⚠️ 是——仅可作辅助信号、RL 应切保守档' if g['weak_signal_band'] else '否'}",
        "",
        "## 6. 结论与风险",
        "- 闸门1 只证明信号非噪声；闸门2（含成本 Sharpe/Calmar 跑赢基线 + PBO + DSR）才是唯一交付判据。",
        "- 本报告是研究型输出，非投资建议；实盘需经 CTP 受控骨架并完成穿透式监管报备。",
        "",
    ]

    # ---- P0 新增章节（追加在既有章节之后，既有章节零改动）----
    dc = summary.get("dual_caliber")
    if dc and "error" not in dc:
        lines += [
            "",
            "## 7. 撮合双口径对照（P0-1）",
            f"- 同 bar 成交: Sharpe {dc['same_bar']['sharpe']:.3f} ｜ Calmar {dc['same_bar']['calmar']:.3f} ｜ "
            f"MaxDD {dc['same_bar']['max_drawdown']*100:.2f}% ｜ 胜率 {dc['same_bar']['win_rate']*100:.1f}%",
            f"- next_bar 成交: Sharpe {dc['next_bar']['sharpe']:.3f} ｜ Calmar {dc['next_bar']['calmar']:.3f} ｜ "
            f"MaxDD {dc['next_bar']['max_drawdown']*100:.2f}% ｜ 胜率 {dc['next_bar']['win_rate']*100:.1f}%",
            f"- Δ%: Sharpe {dc['delta_pct'].get('sharpe')} ｜ Calmar {dc['delta_pct'].get('calmar')} ｜ "
            f"MaxDD {dc['delta_pct'].get('max_drawdown')} ｜ 胜率 {dc['delta_pct'].get('win_rate')}",
            f"- {dc['note']}",
        ]
    fps = summary.get("fingerprints") or []
    if fps:
        lines += ["", "## 8. 四层指纹（P0-3）"]
        for fp in fps[:10]:
            lines.append(
                f"- {fp.get('model_id')}/{fp.get('train_end')}: "
                f"data={fp.get('data_version')} feature={fp.get('feature_version')} "
                f"model={fp.get('model_version')} param={fp.get('param_hash')} config={fp.get('config_version')}"
            )
    bs = summary.get("bootstrap")
    if bs and "error" not in bs:
        lines += [
            "",
            "## 9. Bootstrap 绩效区间（P0-4）",
            f"- Sharpe: {bs['sharpe']['point']:.3f} [95% CI {bs['sharpe']['ci_low']:.3f}–{bs['sharpe']['ci_high']:.3f}]",
            f"- Calmar: {bs['calmar']['point']:.3f} [95% CI {bs['calmar']['ci_low']:.3f}–{bs['calmar']['ci_high']:.3f}]",
            f"- MaxDD: {bs['max_drawdown']['point']*100:.2f}% [95% CI {bs['max_drawdown']['ci_low']*100:.2f}%–{bs['max_drawdown']['ci_high']*100:.2f}%]",
            f"- 参数: block_len={bs['params']['block_len']} n_boot={bs['params']['n_boot']} "
            f"seed={bs['params']['seed']} by_symbol={bs['params']['by_symbol']}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="HexFutures-AI 端到端研究管线")
    ap.add_argument("--config", type=str, default=None, help="实验 yaml")
    ap.add_argument("--demo", action="store_true", help="合成数据 + ARTransformer fallback，≤5 分钟")
    ap.add_argument("--source", type=str, default=None, help="synthetic / csv")
    ap.add_argument("--model", type=str, default=None, help="ar_transformer/tcn/gru/lightgbm/kronos")
    ap.add_argument("--skip-rl", action="store_true")
    ap.add_argument("--skip-evolution", action="store_true")
    ap.add_argument("--rl-steps", type=int, default=None, help="RL 训练步数（demo 建议 20000）")
    ap.add_argument("--report-dir", type=str, default=None)
    ap.add_argument("--no-dual-caliber", action="store_true", help="关闭双口径对照（默认输出，Q2 裁决）")
    ap.add_argument("--i-understand-the-risk", action="store_true", help="仅实盘骨架使用，声明理解风险")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.demo:
        cfg.data.source = "synthetic"
        cfg.data.symbols = ["SHFE.cu", "SHFE.rb", "INE.sc"]
        args.model = args.model or "ar_transformer"
        args.rl_steps = args.rl_steps or 20_000

    summary = run_pipeline(
        cfg, source=args.source, model=args.model, skip_rl=args.skip_rl,
        skip_evolution=args.skip_evolution, rl_steps=args.rl_steps,
        report_dir=args.report_dir, enable_dual_caliber=not args.no_dual_caliber,
    )
    print("=" * 64)
    print(f"报告已生成: {summary['report_path']}")
    print(f"JSON: {summary['json_path']}")
    print("=" * 64)


if __name__ == "__main__":
    main()
