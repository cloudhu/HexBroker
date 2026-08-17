"""SENTINEL Phase 3：RL 决策层最小验证（2026-08-17）。

L3 = SentinelTradingEnv（三品种组合、单边多头连续动作、状态含趋势/波动）
训练：train 段（2018-2021）PPO → OOS 段（2022-2024）用 policy 决策回测
对比规则基线（exp_ret top-30% + trend 过滤：年化 10.2%/回撤 8.8%/Sharpe 1.21）。
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
from hexbroker.feature import build_features  # noqa: E402
from hexbroker.rl.sentinel_env import SentinelTradingEnv  # noqa: E402
from hexbroker.rl.agent import train_ppo  # noqa: E402
from hexbroker.evaluation.metrics import compute_metrics  # noqa: E402
from scripts.refine_lightgbm_champion import (  # noqa: E402
    SYMBOLS, FREQ, DATA_START, DATA_END,
    build_source_plan, fetch_with_failover, compute_realized_returns,
    walk_forward_lightgbm,
)
from scripts.ablate_features import (  # noqa: E402
    load_best_params, load_global_close, align_global_to_inner,
)

REPORT_DATE = "2026-08-17"
DELIVERABLE_DIR = _ROOT / "deliverables" / "software-hexfutures-ai"
GLOBAL_CODES = ["spx", "uup"]
TRAIN_SPLIT = "2022-01-01"  # train < 2022, OOS >= 2022


def build_cfg():
    cfg = load_config()
    cfg.data.symbols = list(SYMBOLS)
    cfg.data.freq = FREQ
    cfg.data.start = DATA_START
    cfg.data.end = DATA_END
    cfg.forecast.horizon = 5
    cfg.forecast.n_mc_samples = 30
    cfg.forecast.calibration_method = "platt"
    cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
    cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
    cfg.feature.cross_params = {"global_codes": GLOBAL_CODES}
    cfg.risk.max_position_pct = 0.30
    return cfg


def equity_from_policy(policy, env: SentinelTradingEnv, dates: list) -> pd.Series:
    """用 policy 决策遍历 dates，返回权益曲线（标记真实 realized）。"""
    eq = []
    obs = env.reset()
    # 定位到 dates[0]
    while env._step < 0:
        env.step(np.zeros(env.n_actions))
    for i in range(len(dates)):
        act = np.clip(policy.act(obs), 0.0, 1.0)
        obs, _, _, _ = env.step(act)
        if env._step == 0:
            continue
        d = dates[i]
        pos = env._pos if False else act
        # 权益：现金 + 持仓未实现（简化：用实现收益累计）
        pnl_t = 0.0
        for j, sym in enumerate(env.symbols):
            r = env._realized.get((sym, d), 0.0)
            pnl_t += act[j] * env.notional_frac * r
        eq.append((d, pnl_t))
    s = pd.Series([e for _, e in eq], index=[d for d, _ in eq])
    return (1.0 + s).cumprod() * env.initial_capital


def main() -> None:
    ap = argparse.ArgumentParser(description="SENTINEL Phase 3：RL 决策层最小验证")
    ap.add_argument("--n-jobs", type=int, default=6)
    ap.add_argument("--steps", type=int, default=30_000)
    args = ap.parse_args()

    print("=" * 72)
    print(f"SENTINEL Phase 3：RL 决策层（train<{TRAIN_SPLIT}，OOS>={TRAIN_SPLIT}）")
    print("=" * 72)

    cfg0 = build_cfg()
    plan = build_source_plan()
    bars, chosen = fetch_with_failover(cfg0, plan)
    bars.validate()
    inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
    gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in GLOBAL_CODES}
    features = build_features(bars, cfg0, global_close=gc)
    realized = compute_realized_returns(bars, int(cfg0.forecast.horizon))

    # LightGBM 嵌套评估集信号（无校准泄漏；含 exp_ret）
    wf = walk_forward_lightgbm(
        cfg0, bars, features, params=load_best_params(),
        collect_models=False, splitter_overrides=None,
        n_jobs_folds=args.n_jobs, cal_split=0.5,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])

    close_parts = []
    for sym in bars.symbols:
        sub = bars.by_symbol(sym)["close"].astype(float).reset_index()
        close_parts.append(sub)
    prices = pd.concat(close_parts).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]

    # 分段
    tr_sig = sig[sig["ts"] < pd.Timestamp(TRAIN_SPLIT)]
    oos_sig = sig[sig["ts"] >= pd.Timestamp(TRAIN_SPLIT)]
    print(f"[OK] 训练信号 {len(tr_sig)} 条（{tr_sig['ts'].min().date()}~{tr_sig['ts'].max().date()}）")
    print(f"[OK] OOS 信号 {len(oos_sig)} 条（{oos_sig['ts'].min().date()}~{oos_sig['ts'].max().date()}）")

    # 训练环境 + PPO
    tr_env = SentinelTradingEnv(tr_sig, prices, cfg0)
    print(f"[OK] 训练环境 obs_dim={tr_env.obs_dim} n_actions={tr_env.n_actions}")
    policy, stats = train_ppo(tr_env, total_timesteps=args.steps, seed=0)
    print(f"[OK] PPO 训练完成：steps={stats.total_timesteps} ep_rew_mean={stats.ep_rew_mean:+.4f} "
          f"mean_reward={stats.mean_reward:+.4f}")

    # OOS 评估（逐日决策 → 权益）
    oos_env = SentinelTradingEnv(oos_sig, prices, cfg0)
    oos_dates = list(oos_env.dates)
    obs = oos_env.reset()
    eq_vals, eq_dates = [], []
    for d in oos_dates:
        a_idx = int(policy.act(obs.reshape(1, -1))[0][0])
        act = oos_env._pos_map[a_idx]
        pnl_t = 0.0
        for j, sym in enumerate(oos_env.symbols):
            r = oos_env._realized.get((sym, d), 0.0)
            pnl_t += act[j] * oos_env.notional_frac * r
        eq_vals.append(1.0 + pnl_t)
        eq_dates.append(d)
        obs, _, _, _ = oos_env.step(a_idx)
    eq = pd.Series(eq_vals, index=eq_dates).cumprod() * oos_env.initial_capital
    m = compute_metrics(eq, freq="1d")
    print("\n[OOS 回测（RL 决策）]")
    print(f"  年化={m.annual_return*100:+.2f}% 回撤={m.max_drawdown*100:.2f}% Sharpe={m.sharpe:.2f}")
    print(f"  覆盖 {len(eq)} 交易日（{eq.index.min().date()}~{eq.index.max().date()}）")

    print("\n[对比规则基线] exp_ret top-30% + trend 过滤：年化 10.2% / 回撤 8.8% / Sharpe 1.21（全样本）")
    verdict = "✅ RL 决策层可用（OOS 正收益）" if m.annual_return > 0 else "⚠️ RL OOS 负收益（需调参/更长训练）"
    print(f"[判定] {verdict}")

    report = {
        "rl_oos": {"annual_return": m.annual_return, "max_drawdown": m.max_drawdown,
                   "sharpe": m.sharpe, "n_days": int(len(eq))},
        "steps": args.steps,
        "verdict": verdict,
    }
    out = DELIVERABLE_DIR / f"sentinel-rl-phase3-{REPORT_DATE}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 报告：{out}")


if __name__ == "__main__":
    main()
