"""P5 引擎 A 跨时间 rank 修复：按日横截面 rank + 稀疏策略对比 + 组合重估。

背景
----
QA 发现 ``scripts/p3_combo_backtest.engine_a_targets`` 用**全表 rank**（7952 行 / 976 天
一次性排序）计算 ``rank_pct``，而信号缓存 ``artifacts/signals_cache18_grouped_v2.parquet``
是 walk_forward 嵌套子窗输出，**覆盖率极度不均**：

  - 2019-2021：日均 12.7~13.2 品种/天（71%~74% 天数 >=15 品种）
  - 2022+：日均 5.7~8.0 品种/天；2024 年 5.7、2025 年 5.9、2026 年仅 2.2 品种/天
  - 52.5% 的天数 <5 品种，27.4% 的天数 <3 品种（2022+ 更严重）

全表 rank 的后果：早期 17 品种的天和后期 2 品种的天混在一起排序，top30% 阈值被早期
高 exp_ret 主导，后期稀疏天的信号被系统性低估/高估——不是真正的"每日截面 top30%"。

本脚本（独立于 p3，不改动任何既有函数）：
  1. 新增 ``engine_a_targets_cs``：按日横截面 rank（``groupby(ts).rank(pct=True)``）
  2. 稀疏处理策略对比（核心实验）：
       S1 无最小品种数限制 / S2 min=3 / S3 min=5 / S4 min=8（当日品种数不足则空仓）
     与旧版（全表 rank）单引擎对比 —— 完整 BacktestEngine 口径
     （滑点 1tick + 手续费 0.005% + 保证金 12% + CONTRACTS18，INITIAL_CAPITAL=1e6）
  3. 以 **OOS 段稳定性**（结构性修复，非参数调优）选择稀疏策略：
       S1-S4 OOS Sharpe 差异大(>=0.15) → 选 OOS 最优者；差异小 → 选 S2 中位数稳健
  4. 用选中策略重跑 P4-1 组合网格（A∈{0.25,0.30,0.35,0.40}，B=1-A，引擎B固定 win252/thr0.7）
     与 P4-1 基线（A25/B75 OOS Sharpe 0.940）及修复前组合对比
  5. 落盘：
       artifacts/p5_engineA_strategy_compare.csv （S1-S4 vs 旧版 单引擎对比）
       artifacts/p5_combo_retune.csv               （修复后组合网格）

口径铁律：嵌套零泄漏（稀疏策略选择只用 OOS 稳定性，参数 top_k/名义与 P3/P4 一致）；
OOS 完全留出 2024-07-18 后；不修改 hexbroker 包与既有数据文件。

用法：
  python scripts/p5_engineA_cross_section.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import (
    BASIS_THR,
    BASIS_WIN,
    NOTIONAL_FRAC,
    OOS_START,
    TOP_K,
    engine_a_targets,
    engine_b_targets,
)

ART = ROOT / "artifacts"
SIGNALS_PATH = ART / "signals_cache18_grouped_v2.parquet"

IS_END = "2022-04-21"                     # IS 段终点（仅参考，不用于稀疏策略选择）
W_A_GRID = [0.25, 0.30, 0.35, 0.40]       # P4-1 权重网格
VOL_TARGET = 0.175                        # 组合层波动率目标（17.5% 年化，同 P4-1）
VOL_HALFLIFE = 10
VOL_SCALE_CAP = 1.5
OOS_SUB_BOUNDS = [                        # OOS 分段（稳定性诊断用）
    ("2024-07-18", "2025-06-30"),
    ("2025-07-01", None),
]
DECISION_SPREAD = 0.15                    # S1-S4 OOS Sharpe 差异"大"的阈值


# ---------------------------------------------------------------------------
# 1. 引擎 A（修复版）：按日横截面 rank
# ---------------------------------------------------------------------------
def _resolve_cache_path(cache_path: Path | str | None) -> Path:
    """解析信号缓存路径：显式传入优先；None 时读生产配置（P8-4 固化：v8）。

    向后兼容：显式传入 v2/v3 等路径时原样使用（旧脚本不受影响）；
    配置读取失败（异常兜底）时回退 v2 基线路径，保证默认行为不崩溃。
    """
    if cache_path is not None:
        return Path(cache_path)
    try:
        raw = load_config().backtest.engine_a.signal_cache
    except Exception:
        raw = str(SIGNALS_PATH)
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _default_group_map() -> dict[str, str]:
    """默认分组映射：复用 GROUPS_V2（8 组），未覆盖品种归 'other'。"""
    from scripts.group_modeling_v2 import GROUPS_V2

    m: dict[str, str] = {}
    for gname, gcfg in GROUPS_V2.items():
        for s in gcfg["syms"]:
            m[s] = gname
    return m


def _resolve_group_map(group_map: dict[str, str] | None) -> dict[str, str]:
    """解析分组映射：显式传入优先；None 时读生产配置（P10-1 新增 group_map 字段）。

    向后兼容：显式传入的自定义映射（如 P9 的 ferrous 合并组）原样使用；
    配置未启用（``cfg.backtest.engine_a.group_map`` 为 None）时回退
    :func:`_default_group_map`（GROUPS_V2 默认 8 组）；配置读取异常同样回退。
    """
    if group_map is not None:
        return group_map
    try:
        raw = load_config().backtest.engine_a.group_map
    except Exception:
        raw = None
    if raw:
        return dict(raw)
    return _default_group_map()


def _resolve_group_cap(group_cap: float | None) -> float | None:
    """解析单组敞口上限：显式传入优先；None 时读生产配置（P10-1）。

    配置也未启用（``cfg.backtest.engine_a.group_cap`` 为 None）→ 返回 None
    （不启用，向后兼容）；配置读取异常同样返回 None。
    """
    if group_cap is not None:
        return group_cap
    try:
        raw = load_config().backtest.engine_a.group_cap
    except Exception:
        raw = None
    return raw


def _capped_selection(
    ranked: list[str],
    target_count: int,
    cap: float,
    group_map: dict[str, str],
) -> list[str]:
    """按 exp_ret 降序候选序列施加单组敞口上限 cap 后的选中列表（P9-2 风控）。

    基线 = 前 ``target_count`` 个（与无 cap 的 top_k 等量，保持名义投放可比）；
    剔除阶段：超限组且组内 >=2 只时，从尾部（最低 exp_ret）逐只剔除直到各组合规；
    替补阶段：按 exp_ret 降序扫描其余候选，加入不会使任何组占比超上限的品种，
    直到补满 ``target_count`` 或无可替补（此时空仓差额）。

    稀疏日（整日仅 1 只候选）允许保留该只，避免风控导致空仓退化。
    """
    if target_count <= 0 or not ranked:
        return []
    selected = list(ranked[:target_count])
    counts: dict[str, int] = {}
    for s in selected:
        g = group_map.get(s, "other")
        counts[g] = counts.get(g, 0) + 1

    def over_cap(g: str, n: int, total: int) -> bool:
        return total > 0 and n / total > cap

    # 剔除阶段
    changed = True
    while changed and len(selected) > 1:
        changed = False
        for i in range(len(selected) - 1, -1, -1):
            g = group_map.get(selected[i], "other")
            if over_cap(g, counts[g], len(selected)) and counts[g] >= 2:
                counts[g] -= 1
                selected.pop(i)
                changed = True
                break

    # 替补阶段
    sel_set = set(selected)
    for s in ranked:
        if len(selected) >= target_count:
            break
        if s in sel_set:
            continue
        g = group_map.get(s, "other")
        if (counts.get(g, 0) + 1) / (len(selected) + 1) <= cap:
            selected.append(s)
            counts[g] = counts.get(g, 0) + 1
            sel_set.add(s)
    return selected


def engine_a_selection(
    prices: pd.DataFrame,
    top_k: float = TOP_K,
    min_symbols: int | None = None,
    cache_path: Path | str | None = None,
    group_cap: float | None = None,
    group_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """引擎 A 每日选中明细（rank / 价格 / 分组 / 选中标志）。

    与 ``engine_a_targets_cs`` 共用同一套截面 rank + px 对齐逻辑，供
    敞口统计（P9-2）与 target 生成复用，避免口径分叉。

    参数
    ----
    prices : MultiIndex(symbol, datetime) + close 列。
    top_k : 每日截面做多分位阈值（默认 0.30）。
    min_symbols : 当日信号品种数不足该值时整日空仓；None 无限制。
    cache_path : 信号缓存 parquet 路径；None 读生产配置。
    group_cap : 单组敞口上限（0,1)；None 读生产配置（P10-1），配置未启用则不限制
                （P9-2 默认向后兼容）。
    group_map : symbol→group 映射；None 读生产配置（P10-1），配置未启用则用
                GROUPS_V2 默认 8 组。
                控制实验可传自定义映射（如把 ferrous_raw+ferrous_steel
                合并为单一 'ferrous' 组，实现"黑色系敞口上限"）。

    返回
    ----
    DataFrame[symbol, ts, exp_ret, rank_pct, _day_cnt, _px, _mult, group, selected]。
    """
    sig = pd.read_parquet(_resolve_cache_path(cache_path))
    sig["ts"] = pd.to_datetime(sig["ts"])
    # P10-1：group_map / group_cap 未显式传入时读生产配置（configs/base.yaml 启用
    # group_cap=0.5 + ferrous_all 合并映射）；配置 None → 回退 GROUPS_V2 / 不启用。
    group_map = _resolve_group_map(group_map)
    group_cap = _resolve_group_cap(group_cap)

    # 每日截面 rank：rank(pct=True, ascending=True) 返回 [0,1] 分位，越大越强
    sig["rank_pct"] = sig.groupby("ts")["exp_ret"].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")

    # 价格对齐（同 p3 实现）
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(
        {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    )
    sig["group"] = sig["symbol"].map(lambda s: group_map.get(s, "other"))

    base_cond = (sig["rank_pct"] >= 1.0 - top_k) & sig["_px"].notna()
    if min_symbols is not None:
        # 稀疏日过滤：当日品种数不足 → 整日空仓
        base_cond = base_cond & (sig["_day_cnt"] >= min_symbols)

    if group_cap is None:
        sig["selected"] = base_cond
    else:
        cap = float(group_cap)
        if not (0.0 < cap < 1.0):
            raise ValueError(f"group_cap 必须在 (0,1) 区间，实际 {group_cap!r}")
        sig["selected"] = False
        elig_mask = sig["_px"].notna()
        if min_symbols is not None:
            elig_mask = elig_mask & (sig["_day_cnt"] >= min_symbols)
        elig = sig[elig_mask]
        for _ts, g in elig.groupby("ts"):
            g = g.sort_values("exp_ret", ascending=False)
            target_count = int((g["rank_pct"] >= 1.0 - top_k).sum())
            if target_count <= 0:
                continue
            sel_syms = _capped_selection(
                g["symbol"].tolist(), target_count, cap, group_map
            )
            sig.loc[g.index[g["symbol"].isin(sel_syms)], "selected"] = True
    return sig[["symbol", "ts", "exp_ret", "rank_pct", "_day_cnt", "_px", "_mult",
                "group", "selected"]]


def engine_a_targets_cs(
    prices: pd.DataFrame,
    top_k: float = TOP_K,
    min_symbols: int | None = None,
    cache_path: Path | str | None = None,
    group_cap: float | None = None,
    group_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """引擎 A（修复版）：按日横截面 rank top_k 做多，支持稀疏日最小品种数过滤。

    与旧版 ``engine_a_targets`` 的差异仅在 rank 维度：
      - 旧版：``rank_pct = exp_ret.rank(pct=True)``（全表跨时间排序）
      - 本版：``rank_pct = groupby(ts).rank(pct=True)``（每日截面排序，[0,1] 越大越强）

    其余口径与 p3 完全一致：
      - 名义 NOTIONAL_FRAC=0.20 权益/标的；手数 = floor(notional/(px*multiplier))
      - multiplier 用 CONTRACTS18；_px 用 prices.xs(symbol).close.get(ts) 对齐

    P9-2 新增：``group_cap`` 单组敞口上限（默认 None 不启用，向后兼容）；
    启用后按日剔除超限组最低 exp_ret 成员并以次优品种替补（见
    :func:`_capped_selection`）。

    P10-1 新增：``group_cap`` / ``group_map`` 未显式传入时读生产配置
    （``cfg.backtest.engine_a.group_cap/group_map``，见 :func:`_resolve_group_cap`
    与 :func:`_resolve_group_map`）；配置未启用时保持 P9-2 默认行为
    （group_cap 不启用 / group_map 用 GROUPS_V2）。

    参数
    ----
    prices : MultiIndex(symbol, datetime) + close 列（同 p3 load_prices）。
    top_k : 每日截面做多分位阈值（默认 0.30 → 每日截面 top30%）。
    min_symbols : 当日有信号的品种数不足该值时整日空仓；None 表示无限制（策略 S1）。
    cache_path : 信号缓存 parquet 路径；None 时读生产配置（P8-4：v8）；
                 显式传入则覆盖（向后兼容：显式 v2/v3 路径仍可用）。
    group_cap : 单组敞口上限（0,1）；None 读生产配置（P10-1），配置未启用则不限制
                （P9-2 默认，向后兼容）。
    group_map : symbol→group 映射；None 读生产配置（P10-1），配置未启用则用
                GROUPS_V2 默认 8 组。

    返回
    ----
    MultiIndex(symbol, ts) + target 列，与 p3 旧版格式一致，可直接喂 BacktestEngine。
    """
    sig = engine_a_selection(
        prices, top_k, min_symbols, cache_path, group_cap, group_map
    )
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = notional / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(sig["selected"], raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 2. 回测辅助
# ---------------------------------------------------------------------------
def run_engine_row(
    cfg,
    cost: CostModel,
    prices: pd.DataFrame,
    targets: pd.DataFrame,
    label: str,
) -> tuple[pd.Series, pd.Series, object, object, float]:
    """完整口径回测 → (日收益, 权益曲线, 全样本指标, OOS 指标, 做多天数占比)。

    口径：BacktestEngine + CostModel（滑点1tick + 手续费0.005% + 保证金12%）
    + CONTRACTS18 + INITIAL_CAPITAL=1e6（与 P3/P4 完全一致）。
    """
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(prices, targets)
    eq = pf.equity_curve
    ret = eq.pct_change().dropna()
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None

    # 做多天数占比 = 至少持有一个做多标的的日数 / 全部有信号的日数
    # 注意：engine_a targets 的 MultiIndex 层名是 ["symbol","ts"]，engine_b 是
    # ["symbol","datetime"]，故按位置取第 1 层（时间层），不依赖层名。
    tgt = targets["target"]
    total_days = pd.Index(pd.to_datetime(targets.index.get_level_values(1).unique()))
    long_days = pd.Index(
        pd.to_datetime(tgt[tgt > 0].index.get_level_values(1).unique())
    )
    long_ratio = len(long_days) / len(total_days) if len(total_days) else float("nan")
    return ret, eq, m, m_oos, long_ratio


def seg_sharpe(eq: pd.Series, start: str, end: str | None = None) -> float:
    """指定时间段的 Sharpe（区间 bars 数 >30 才计算，否则 NaN）。"""
    idx = pd.to_datetime(eq.index)
    sel = idx >= pd.Timestamp(start)
    if end is not None:
        sel &= idx < pd.Timestamp(end)
    sub = eq[sel]
    if len(sub) > 30:
        return float(compute_metrics(sub, freq="daily").sharpe)
    return float("nan")


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%" if pd.notna(x) else "   n/a"


def fmt_sh(x: float) -> str:
    return f"{x:.3f}" if pd.notna(x) else "  n/a"


# ---------------------------------------------------------------------------
# 3. 组合辅助（与 P4-1 combo_stats_row 逻辑一致）
# ---------------------------------------------------------------------------
def combo_stats_row(ret_a: pd.Series, ret_b: pd.Series, w_a: float, vol_target: bool) -> dict:
    """加权组合日收益 → 权益 → 全样本 + OOS 指标（一行）。"""
    comb = w_a * ret_a + (1.0 - w_a) * ret_b
    if vol_target:
        # EWMA 波动率（halflife=10），shift(1) 只用 t-1 信息避免前视
        vol = comb.ewm(halflife=VOL_HALFLIFE, adjust=False).std().shift(1)
        scale = (VOL_TARGET / (vol * np.sqrt(252))).clip(lower=0.0, upper=VOL_SCALE_CAP)
        scale = scale.fillna(1.0)  # 预热期不缩放
        comb = comb * scale
    eq = (1.0 + comb).cumprod() * INITIAL_CAPITAL
    m = compute_metrics(eq, freq="daily")
    idx = pd.to_datetime(eq.index)
    oos_eq = eq[idx >= OOS_START]
    m_oos = compute_metrics(oos_eq, freq="daily") if len(oos_eq) > 30 else None
    return {
        "w_a": w_a,
        "w_b": round(1.0 - w_a, 4),
        "vol_target": vol_target,
        "sharpe_full": m.sharpe,
        "ann_ret_full": m.annual_return,
        "maxdd_full": m.max_drawdown,
        "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
        "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
        "oos_ret": m_oos.total_return if m_oos else np.nan,
        "oos_n": len(oos_eq) if m_oos else 0,
    }


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 96)
    print("P5 引擎 A 跨时间 rank 修复：按日横截面 rank + 稀疏策略对比 + 组合重估")
    print("=" * 96)

    # ---- 0. 问题确认：覆盖率统计 ----
    sig0 = pd.read_parquet(SIGNALS_PATH)
    sig0["ts"] = pd.to_datetime(sig0["ts"])
    cov = sig0.groupby(sig0["ts"].dt.date)["symbol"].count()
    print("\n[0] 问题确认：信号缓存覆盖率（全表 rank 的病灶）")
    print(f"    总行数 {len(sig0)} | 天数 {len(cov)} | 日均品种 {cov.mean():.1f} "
          f"(min={cov.min()}, max={cov.max()})")
    print(f"    天数 <5 品种占比 {100 * (cov < 5).mean():.1f}% | "
          f"天数 <3 品种占比 {100 * (cov < 3).mean():.1f}%")
    for y, g in sig0.groupby(sig0["ts"].dt.year):
        c = g.groupby(g["ts"].dt.date)["symbol"].count()
        print(f"      {y}: 日均 {c.mean():.1f} 品种 | >=15 品种天数 {100*(c>=15).mean():.0f}% | "
              f"<5 品种天数 {100*(c<5).mean():.0f}% | <3 品种天数 {100*(c<3).mean():.0f}%")

    # ---- 1. 环境 ----
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    print(f"\n[1] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种")
    print(f"    口径: 滑点1tick + 手续费0.005% + 保证金12% + CONTRACTS18 | "
          f"INITIAL_CAPITAL={INITIAL_CAPITAL:,.0f} | top_k={TOP_K} | notional={NOTIONAL_FRAC*100:.0f}%权益/标的")

    # ---- 2. 单引擎对比：旧版 + S1-S4 ----
    strategies = [
        ("old", engine_a_targets(prices), "旧版(全表rank)"),
        ("S1", engine_a_targets_cs(prices, TOP_K, None), "S1 截面rank min=None"),
        ("S2", engine_a_targets_cs(prices, TOP_K, 3), "S2 截面rank min=3"),
        ("S3", engine_a_targets_cs(prices, TOP_K, 5), "S3 截面rank min=5"),
        ("S4", engine_a_targets_cs(prices, TOP_K, 8), "S4 截面rank min=8"),
    ]
    print("\n[2] 单引擎回测（完整口径）")
    rets: dict[str, pd.Series] = {}
    eqs: dict[str, pd.Series] = {}
    rows: list[dict] = []
    for key, tgt, desc in strategies:
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt, key)
        rets[key] = ret
        eqs[key] = eq
        oos_sh = m_oos.sharpe if m_oos else np.nan
        oos_dd = m_oos.max_drawdown if m_oos else np.nan
        n_long = int((tgt["target"] > 0).sum())
        rows.append({
            "strategy": key,
            "desc": desc,
            "sharpe_full": m.sharpe,
            "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "oos_sharpe": oos_sh,
            "oos_maxdd": oos_dd,
            "long_day_ratio": long_ratio,
            "n_long_rows": n_long,
            "oos_n": m_oos.n_bars if m_oos else 0,
        })
        print(f"  [{key:<3} {desc:<22}] Sharpe={m.sharpe:.3f} "
              f"年化={m.annual_return*100:+.1f}% MaxDD={m.max_drawdown*100:.1f}% "
              f"| OOS Sharpe={fmt_sh(oos_sh)} OOS MaxDD={fmt_pct(oos_dd)} "
              f"| 做多天数占比={long_ratio*100:.1f}% 做多行={n_long}")

    tbl = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    tbl.to_csv(ART / "p5_engineA_strategy_compare.csv", index=False)
    print(f"\n  [OK] 结果 → {ART / 'p5_engineA_strategy_compare.csv'}")

    # ---- 3. OOS 稳定性诊断（稀疏策略选择依据） ----
    print("\n[3] OOS 分段稳定性诊断（结构性修复判断依据）")
    print(f"    {'策略':<6}{'IS(<=2022-04-21)':>18}{'OOS 2024H2':>14}{'OOS 2025+':>14}"
          f"{'OOS 全段':>12}{'OOS 最差段':>12}")
    for key, desc, *_ in strategies:
        ret = rets[key]
        eq_approx = eqs[key]
        is_sh = seg_sharpe(eq_approx, "2019-01-01", IS_END)
        seg1 = seg_sharpe(eq_approx, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
        seg2 = seg_sharpe(eq_approx, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
        oos_sh = tbl.loc[tbl["strategy"] == key, "oos_sharpe"].iloc[0]
        worst = min([s for s in (seg1, seg2) if pd.notna(s)], default=np.nan)
        print(f"    {key:<6}{fmt_sh(is_sh):>18}{fmt_sh(seg1):>14}{fmt_sh(seg2):>14}"
              f"{fmt_sh(oos_sh):>12}{fmt_sh(worst):>12}")

    # ---- 4. 稀疏策略选择 ----
    s_rows = {r["strategy"]: r for r in rows if r["strategy"] != "old"}
    oos_map = {k: v["oos_sharpe"] for k, v in s_rows.items()}
    spread = max(oos_map.values()) - min(oos_map.values())
    print(f"\n[4] 稀疏策略选择（S1-S4 OOS Sharpe 差异 = {spread:.3f}）")
    if spread >= DECISION_SPREAD:
        chosen = max(oos_map, key=oos_map.get)
        reason = f"S1-S4 OOS Sharpe 差异大(>= {DECISION_SPREAD}) → 选 OOS 最优 {chosen}"
    else:
        chosen = "S2"
        reason = f"S1-S4 OOS Sharpe 差异小(< {DECISION_SPREAD}) → 选 S2 中位数稳健"
    # 若选中的策略 OOS 最差段为负而 S2 更好，说明稳定性存疑，做说明性提示
    oos_worst = {}
    for key, desc, *_ in strategies:
        if key == "old":
            continue
        eq_approx = eqs[key]
        seg1 = seg_sharpe(eq_approx, OOS_SUB_BOUNDS[0][0], OOS_SUB_BOUNDS[0][1])
        seg2 = seg_sharpe(eq_approx, OOS_SUB_BOUNDS[1][0], OOS_SUB_BOUNDS[1][1])
        oos_worst[key] = min([s for s in (seg1, seg2) if pd.notna(s)], default=np.nan)
    print(f"    选择: {chosen} —— {reason}")
    print(f"    OOS 最差段: { {k: round(v,3) if pd.notna(v) else None for k,v in oos_worst.items()} }")
    if chosen != "S2" and oos_worst.get(chosen, np.nan) < oos_worst.get("S2", np.nan):
        print("    提示: 所选策略的 OOS 最差段弱于 S2，稳定性存疑（报告中人工复核）")

    # ---- 5. 组合重估 ----
    print("\n[5] 组合重估（P4-1 网格 × 修复后引擎 A，引擎 B 固定 win252/thr0.7）")
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b = run_engine_row(cfg, cost, prices, tgt_b, "B")[0]
    ret_a_old = rets["old"]
    ret_a_new = rets[chosen]

    def align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
        common = a.index.intersection(b.index)
        return a.loc[common].sort_index(), b.loc[common].sort_index()

    combo_rows = []
    for engine_key, ret_a in (("old", ret_a_old), (chosen, ret_a_new)):
        ra, rb = align(ret_a, ret_b)
        for w_a in W_A_GRID:
            for vt in (False, True):
                r = combo_stats_row(ra, rb, w_a, vt)
                r["engine_a"] = engine_key
                combo_rows.append(r)
    ctbl = pd.DataFrame(combo_rows)
    ctbl = ctbl[["engine_a", "w_a", "w_b", "vol_target", "sharpe_full", "ann_ret_full",
                 "maxdd_full", "oos_sharpe", "oos_maxdd", "oos_ret", "oos_n"]]
    ctbl.to_csv(ART / "p5_combo_retune.csv", index=False)

    # 展示
    show = ctbl.copy()
    show["vol_target"] = show["vol_target"].map({True: "Y", False: "N"})
    show["sharpe_full"] = show["sharpe_full"].map(lambda v: f"{v:.3f}")
    show["ann_ret_full"] = show["ann_ret_full"].map(fmt_pct)
    show["maxdd_full"] = show["maxdd_full"].map(fmt_pct)
    show["oos_sharpe"] = show["oos_sharpe"].map(fmt_sh)
    show["oos_maxdd"] = show["oos_maxdd"].map(fmt_pct)
    show["oos_ret"] = show["oos_ret"].map(fmt_pct)
    show["engine_a"] = show["engine_a"].map(lambda k: "修复后" if k == chosen else "修复前")
    print(show[["engine_a", "w_a", "w_b", "vol_target", "sharpe_full", "ann_ret_full",
                "maxdd_full", "oos_sharpe", "oos_maxdd", "oos_ret"]].to_string(index=False))
    print(f"\n  [OK] 结果 → {ART / 'p5_combo_retune.csv'}")

    # ---- 6. 最终判定 ----
    new_tbl = ctbl[ctbl["engine_a"] == chosen]
    best_new = new_tbl.loc[new_tbl["oos_sharpe"].idxmax()]
    base = pd.read_csv(ART / "p4_combo_tune.csv")
    base_row = base.loc[(base["w_a"] == 0.25) & (~base["vol_target"])].iloc[0]
    old_best = ctbl[ctbl["engine_a"] == "old"].sort_values("oos_sharpe", ascending=False).iloc[0]

    print("\n[6] 最终判定")
    print(f"  P4-1 基线 A25/B75(vol=N): OOS Sharpe={base_row['oos_sharpe']:.3f} "
          f"OOS MaxDD={base_row['oos_maxdd']*100:.1f}% 全样本 Sharpe={base_row['sharpe_full']:.3f}")
    print(f"  修复前组合最优           : A{old_best['w_a']:.2f}/B{old_best['w_b']:.2f} "
          f"vol={'Y' if old_best['vol_target'] else 'N'} OOS Sharpe={old_best['oos_sharpe']:.3f} "
          f"OOS MaxDD={old_best['oos_maxdd']*100:.1f}%")
    print(f"  修复后组合最优(引擎A={chosen}): A{best_new['w_a']:.2f}/B{best_new['w_b']:.2f} "
          f"vol={'Y' if best_new['vol_target'] else 'N'} OOS Sharpe={best_new['oos_sharpe']:.3f} "
          f"OOS MaxDD={best_new['oos_maxdd']*100:.1f}% 全样本 Sharpe={best_new['sharpe_full']:.3f}")
    delta = best_new["oos_sharpe"] - base_row["oos_sharpe"]
    print(f"  OOS Sharpe 增量 vs P4-1 基线: {delta:+.3f} → "
          f"{'PASS(优于基线)' if delta > 0 else 'NEUTRAL(不优于基线)'}")
    print(f"  最终推荐配置: 引擎A={chosen}(按日截面rank) + 引擎B(win{BASIS_WIN}/thr{BASIS_THR}) "
          f"权重 A={best_new['w_a']:.2f}/B={best_new['w_b']:.2f} "
          f"{'叠加' if best_new['vol_target'] else '不叠加'}波动率目标")


if __name__ == "__main__":
    main()
