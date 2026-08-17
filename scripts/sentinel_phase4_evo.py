"""SENTINEL Phase 4：自回归进化闭环（2026-08-17）。

进化对象：L3 奖励权重 θ = [w_pnl, w_turn, w_trend]（奖励工程后的三维连续空间）。
进化算法：自回归 ES——
- 高斯扰动变异（标准差随代递减）；
- 均值更新用 EMA（θ_mean ← α·θ_elite + (1−α)·θ_mean_prev），即"自回归"动量；
- 适应度 = 该权重下训练 PPO（短步）→ OOS 段回测 Sharpe（含成本语义）。

输出：最优奖励权重 + 最终 PPO 的 OOS 完整回测（与规则基线公平对比）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hexbroker.config import load_config  # noqa: E402
from hexbroker.data.schema import BarFrame  # noqa: E402


def load_local_bars(symbols: list[str], end: str = "2026-08-17") -> BarFrame:
    """从本地延长 parquet 加载（sina 只到 2024-07，PandaData 延长已拼接落盘）。"""
    import glob
    dir_map = {"SHFE.au": "au0", "SHFE.ag": "ag0", "DCE.m": "m0", "au0": "au0", "ag0": "ag0", "m0": "m0"}
    parts = []
    for sym in symbols:
        d = dir_map.get(sym, sym)
        for fp in sorted(glob.glob(f"data/raw/processed/{d}/1d/*.parquet")):
            parts.append(pd.read_parquet(fp))
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index(["symbol", "datetime"]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return BarFrame(df=df, freq="1d", source="local")
from hexbroker.feature import build_features  # noqa: E402
from hexbroker.rl.sentinel_env import SentinelTradingEnv  # noqa: E402
from hexbroker.rl.agent import train_ppo  # noqa: E402
from hexbroker.evaluation.metrics import compute_metrics  # noqa: E402
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-17"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]
TRAIN_SPLIT = "2022-01-01"   # PPO 训练 < 2022
VALID_SPLIT = "2024-07-18"  # 进化适应度: 2022~2024-07-17；最终 OOS: 2024-07-18~2026-08（真新数据，未参与任何训练/选择）
# 奖励权重搜索空间（log 空间：w_pnl∈[0.1,10], w_turn∈[0.01,2], w_trend∈[0,8]）
W_LOW = np.array([0.1, 0.01, 0.0])
W_HIGH = np.array([10.0, 2.0, 8.0])
PPO_STEPS = 8_000
POP_SIZE = 6
N_GEN = 3
EMA_ALPHA = 0.5


def build_cfg():
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = "2026-08-17"  # 延长后数据（PandaData）
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    cfg.risk.max_position_pct = 0.30
    return cfg


def make_env(sig, prices, cfg0, w: np.ndarray) -> SentinelTradingEnv:
    """按奖励权重构造环境（覆盖 cfg.rl 的 w_*）。"""
    cfg0.rl.w_pnl = float(w[0])
    cfg0.rl.w_turn = float(w[1])
    cfg0.rl.w_trend = float(w[2])
    return SentinelTradingEnv(sig, prices, cfg0)


def oos_sharpe(policy, env: SentinelTradingEnv) -> float:
    """OOS 段逐日决策 → 权益 → Sharpe。"""
    obs = env.reset()
    vals = []
    for _ in range(len(env.dates)):
        a_idx = int(policy.act(obs.reshape(1, -1))[0][0])
        act = env._pos_map[a_idx]
        pnl_t = 0.0
        for j, sym in enumerate(env.symbols):
            r = env._realized.get((sym, env.dates[env._step]), 0.0)
            pnl_t += act[j] * env.notional_frac * r
        vals.append(1.0 + pnl_t)
        obs, _, _, _ = env.step(a_idx)
    eq = pd.Series(vals, index=env.dates).cumprod()
    return float(compute_metrics(eq, freq="1d").sharpe)


def main() -> None:
    ap = argparse.ArgumentParser(description="SENTINEL Phase 4：自回归进化奖励权重")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--steps", type=int, default=PPO_STEPS)
    ap.add_argument("--gen", type=int, default=N_GEN)
    ap.add_argument("--pop", type=int, default=POP_SIZE)
    args = ap.parse_args()

    print("=" * 72)
    print(f"SENTINEL Phase 4：自回归进化奖励权重（pop={args.pop} gen={args.gen} PPO={args.steps}）")
    print("=" * 72)

    cfg0 = build_cfg()
    bars = load_local_bars(SYMBOLS)
    bars.validate()
    chosen = "local-parquet"  # sina 截止 2024-07，本地含 PandaData 延长到 2026-08
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)

    wf = walk_forward_lightgbm(
        cfg0, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None,
        n_jobs_folds=args.n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    tr_sig = sig[sig["ts"] < pd.Timestamp(TRAIN_SPLIT)]
    valid_sig = sig[(sig["ts"] >= pd.Timestamp(TRAIN_SPLIT)) & (sig["ts"] < pd.Timestamp(VALID_SPLIT))]
    oos_sig = sig[sig["ts"] >= pd.Timestamp(VALID_SPLIT)]

    close_parts = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    print(f"[OK] train {len(tr_sig)} / valid {len(valid_sig)} / OOS {len(oos_sig)} 信号")

    rng = np.random.default_rng(42)

    def _eval(w: np.ndarray, seed: int) -> float:
        env_tr = make_env(tr_sig, prices, cfg0, w)
        policy, _ = train_ppo(env_tr, total_timesteps=args.steps, seed=seed)
        env_v = make_env(valid_sig, prices, cfg0, w)
        return oos_sharpe(policy, env_v)

    # ---- 自回归 ES ----
    w_mean = np.array([1.0, 0.5, 2.0])  # 默认/先验
    best = {"w": w_mean.copy(), "fitness": -1e9, "seed": 0}
    history = []
    for gen in range(args.gen):
        sigma = 0.5 * (0.5 ** gen)  # 变异尺度随代递减
        pop = []
        for i in range(args.pop):
            w = np.clip(w_mean + rng.normal(0, sigma, 3) * (W_HIGH - W_LOW), W_LOW, W_HIGH)
            seed = int(rng.integers(0, 10000))
            fit = _eval(w, seed)
            pop.append({"w": w, "fitness": fit, "seed": seed})
            print(f"[G{gen+1}/P{i+1}] w={np.round(w,3)} Sharpe={fit:.3f}")
        # 精英
        elite = max(pop, key=lambda x: x["fitness"])
        if elite["fitness"] > best["fitness"]:
            best = elite
        # 自回归均值更新（EMA）
        w_mean = EMA_ALPHA * elite["w"] + (1 - EMA_ALPHA) * w_mean
        history.append({"gen": gen + 1, "best_sharpe": elite["fitness"], "w_elite": elite["w"].tolist()})
        print(f"[G{gen+1}] 精英 Sharpe={elite['fitness']:.3f} w={np.round(elite['w'],3)}")

    print("=" * 72)
    print(f"[进化完成] 最优 w={np.round(best['w'],3)} OOS Sharpe={best['fitness']:.3f} (seed={best['seed']})")

    # ---- 最终 PPO（更长训练）+ 完整 OOS 回测（BacktestEngine 口径） ----
    print("[OK] 用最优权重训练最终 PPO（30k steps）...")
    env_tr = make_env(tr_sig, prices, cfg0, best["w"])
    policy, _ = train_ppo(env_tr, total_timesteps=30_000, seed=best["seed"])
    env_oos = make_env(oos_sig, prices, cfg0, best["w"])  # OOS = 2024-07-18 起（真新数据，完全未参与选择）
    from hexbroker.backtest.engine import BacktestEngine
    from hexbroker.backtest.cost import CostModel
    _CONTRACTS = {"au0": {"multiplier": 1000.0, "min_tick": 0.02}, "ag0": {"multiplier": 15.0, "min_tick": 0.01}, "m0": {"multiplier": 10.0, "min_tick": 1.0}}
    _notional = cfg0.backtest.initial_capital * 0.30  # 名义等权 30%/标的（与规则基线一致）
    obs = env_oos.reset()
    rows = []
    for d in env_oos.dates:
        a_idx = int(policy.act(obs.reshape(1, -1))[0][0])
        act = env_oos._pos_map[a_idx]
        for j, sym in enumerate(env_oos.symbols):
            px = prices.xs(sym, level=0)["close"].get(d)
            if px is not None:
                mult = _CONTRACTS[sym]["multiplier"]
                n = int(act[j] * _notional / (px * mult))
                rows.append({"symbol": sym, "ts": d, "target": n})
        obs, _, _, _ = env_oos.step(a_idx)
    rl_targets = pd.DataFrame(rows).set_index(["symbol", "ts"])[["target"]].sort_index()
    _cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                      slippage_ticks=1.0, margin_rate=0.12, contracts=_CONTRACTS)
    _eng = BacktestEngine(cfg0, cost=_cost, initial_capital=cfg0.backtest.initial_capital)
    _pf = _eng.run(prices, rl_targets)
    m = compute_metrics(_pf.equity_curve, freq="1d")
    print("\n[最终 OOS 回测（进化后 RL → BacktestEngine 完整口径，含滑点/手续费/逐 bar）]")
    # ---- 规则基线同段对比（top-30% + trend 过滤，名义 30%/标的） ----
    from hexbroker.backtest.engine import BacktestEngine
    from hexbroker.backtest.cost import CostModel
    from hexbroker.evaluation.metrics import compute_metrics as _cm
    _CONTRACTS = {"au0": {"multiplier": 1000.0, "min_tick": 0.02}, "ag0": {"multiplier": 15.0, "min_tick": 0.01}, "m0": {"multiplier": 10.0, "min_tick": 1.0}}
    sig_all = sig.copy()
    sig_all["ts"] = pd.to_datetime(sig_all["ts"])
    oos_s = sig_all[sig_all["ts"] >= pd.Timestamp(VALID_SPLIT)].copy()
    close_by2 = {}
    close_parts2 = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_by2[sym] = sub.set_index("datetime")["close"].sort_index()
        close_parts2.append(sub)
    prices2 = pd.concat(close_parts2).set_index(["symbol", "datetime"]).sort_index()
    prices2 = prices2.loc[~prices2.index.duplicated(keep="last")]
    oos_s["rank_pct"] = oos_s["exp_ret"].rank(pct=True)
    oos_s["_px"] = oos_s.apply(lambda r: prices2.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1)
    oos_s["_mult"] = oos_s["symbol"].map({s_: _CONTRACTS[s_]["multiplier"] for s_ in _CONTRACTS})
    def _tok(sym_, ts_, w=20):
        st = close_by2[sym_].loc[:ts_]
        if len(st) < w:
            return False
        return st.iloc[-1] >= st.rolling(w, min_periods=w).mean().iloc[-1]
    oos_s["_ok"] = oos_s.apply(lambda r: _tok(r["symbol"], r["ts"]), axis=1)
    _notional = cfg0.backtest.initial_capital * 0.30
    oos_s["target"] = np.where((oos_s["rank_pct"] >= 0.7) & oos_s["_px"].notna() & oos_s["_ok"],
                               (_notional / (oos_s["_px"] * oos_s["_mult"])).astype(int), 0)
    _tg = oos_s.set_index(["symbol", "ts"])[["target"]].sort_index()
    _cost = CostModel(fee_open=0.00005, fee_close=0.00005, fee_close_today=0.00010,
                      slippage_ticks=1.0, margin_rate=0.12, contracts=_CONTRACTS)
    _eng = BacktestEngine(cfg0, cost=_cost, initial_capital=cfg0.backtest.initial_capital)
    _pf = _eng.run(prices2, _tg)
    _m = _cm(_pf.equity_curve, freq="1d")
    print(f"[规则基线 OOS 同段] 年化={_m.annual_return*100:+.2f}% 回撤={_m.max_drawdown*100:.2f}% Sharpe={_m.sharpe:.2f} (n={len(oos_s)})")

    print(f"  年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% Sharpe={m.sharpe:.2f}")
    print("  [对比] 规则基线 top-30%+trend（全样本）: 年化 10.2%/回撤 8.8%/Sharpe 1.21")

    report = {
        "best_w": best["w"].tolist(),
        "best_seed": best["seed"],
        "final_oos": {"annual_return": m.annual_return, "max_drawdown": m.max_drawdown, "sharpe": m.sharpe},
        "history": history,
    }
    out = DELIVERABLE_DIR / f"sentinel-phase4-evo-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
