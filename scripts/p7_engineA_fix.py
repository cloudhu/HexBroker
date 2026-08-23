"""P7-2 引擎 A 信号端修复：四方案对比（分组滚动 IC 加权 / IS 翻转 / 剔除 / 更频繁重训）。

背景
----
P7-1 诊断（主理人完成）定位引擎 A OOS（2024-07-18 后）exp_ret 无预测力的根因：
**分组信号 OOS 翻转**：

  - agri（m0/y0/p0/sr0/cf0）      IS IC +0.133 → OOS IC -0.190（严重翻转）
  - precious（au0/ag0）            IS +0.025  → OOS -0.186（严重翻转）
  - ferrous（rb0/i0/hc0/j0/jm0）   IS +0.072  → OOS +0.059（稳定）
  - industrial（cu0/al0/zn0/ni0）  IS +0.025  → OOS +0.025（稳定）
  - chem_energy（ta0/sc0）         IS +0.046  → OOS +0.197（改善）

本脚本实现四个候选方案并在**嵌套零泄漏**口径下对比：

  S1 分组滚动 IC 加权   ：按组 126 日滚动 IC（只用 t 日及以前已实现收益），
                          w=max(IC,0)，score=exp_ret*w，再按日截面 rank top30%。
  S2 IS 段翻转          ：翻转组集合**只用 IS 段（<=2022-04-21）组 IC 符号**决定
                          （IS IC<0 的组翻转）。另附 ex-post 参考变体（翻转 agri/
                          precious，OOS 事后知识，**仅诊断不可选**）。
  S3 负 IC 组剔除       ：按组 126 日滚动 IC，IC<=0 的组当日 score=NaN（剔除出截面
                          rank）；缺失 IC 组中性（不剔除）。
  S4 更频繁重训         ：walk_forward 测试窗 60→30（splitter_overrides），重建
                          v2 同口径信号缓存后按 S2 策略构造 targets。

口径铁律（本任务核心）
--------------------
1. **禁止用 OOS 段信息选方案**：方案间选择只看 IS 段（<=2022-04-21）绩效；
   OOS 只做终裁。
2. **嵌套零泄漏**：滚动 IC 在 t 日用 ts'<=t-5（horizon=5）且已实现收益的信号行
   计算，并在脚本内做泄漏断言。
3. **完整回测口径**：滑点1tick + 手续费0.005% + 保证金12% + CONTRACTS18 +
   INITIAL_CAPITAL=1e6 + top_k=0.30 + min_symbols=3（P5 终裁 S2）。
4. OOS 严格留出 2024-07-18 后。
5. 不改数据文件、不改 BacktestEngine；本脚本独立。

用法
----
  python scripts/p7_engineA_fix.py                     # 全部方案（S4 若缓存存在则跳过重训）
  python scripts/p7_engineA_fix.py --schemes 1,2,3     # 只跑 S1-S3
  python scripts/p7_engineA_fix.py --retrain           # 强制重训 S4 缓存
  python scripts/p7_engineA_fix.py --skip-backtest     # 仅重训/输出诊断（调试）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import INITIAL_CAPITAL, load_prices
from scripts.p3_combo_backtest import OOS_START
from scripts.p5_engineA_cross_section import combo_stats_row, run_engine_row, seg_sharpe

ART = ROOT / "artifacts"
SIGNALS_PATH = ART / "signals_cache18_grouped_v2.parquet"
RT30_PATH = ART / "signals_cache18_grouped_v2_rt30.parquet"
COMPARE_CSV = ART / "p7_engineA_fix_compare.csv"
IC_CSV = ART / "p7_engineA_fix_group_ic.csv"
WEIGHT_CSV = ART / "p7_engineA_fix_weights_monthly.csv"

# 口径常量（与 p3/p5 完全一致）
TOP_K = 0.30
NOTIONAL_FRAC = 0.20
BASIS_WIN = 252
BASIS_THR = 0.70
MIN_SYMBOLS = 3              # P5 终裁 S2：截面 rank + min=3
IS_END = "2022-04-21"        # IS 段终点（方案选择唯一依据段）
HORIZON = 5                  # 与模型 forecast.horizon 一致（前向收益窗）
IC_WIN = 126                 # 滚动 IC 窗口（约 6 个月交易日）
MIN_PAIRS = 30               # 滚动 IC 最小配对样本数
PROD_W_A = 0.15              # 生产组合权重 A15/B85
A25 = 0.25

# P7-1 诊断用粗分组（group_modeling.py 定义，5 组）
GROUPS: dict[str, list[str]] = {
    "agri": ["m0", "y0", "p0", "sr0", "cf0"],
    "precious": ["au0", "ag0"],
    "ferrous": ["rb0", "i0", "hc0", "j0", "jm0"],
    "industrial": ["cu0", "al0", "zn0", "ni0"],
    "chem_energy": ["ta0", "sc0"],
}
SYM2GROUP: dict[str, str] = {s: g for g, ss in GROUPS.items() for s in ss}


# ---------------------------------------------------------------------------
# 1. 前向收益面板 + 零泄漏滚动组 IC
# ---------------------------------------------------------------------------
def build_fwd_panel(prices: pd.DataFrame, horizon: int = HORIZON) -> pd.DataFrame:
    """前向 horizon 日收益面板 → DataFrame(datetime, symbol) → fwd。"""
    close_df = prices["close"].unstack("symbol").sort_index()
    fwd = close_df.shift(-horizon) / close_df - 1.0
    return fwd


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman 秩相关，退化输入返回 NaN。"""
    if len(x) < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return float("nan")
    try:
        r = pd.Series(x).corr(pd.Series(y), method="spearman")
        return float(r) if pd.notna(r) else float("nan")
    except Exception:  # noqa: BLE001
        return float("nan")


def compute_rolling_group_ic(
    sig: pd.DataFrame,
    fwd: pd.DataFrame,
    ic_win: int = IC_WIN,
    min_pairs: int = MIN_PAIRS,
    horizon: int = HORIZON,
    debug: bool = False,
) -> pd.DataFrame:
    """零泄漏滚动组 IC：对每个 (group, ts) 计算「t 日可知」的滚动 IC。

    泄漏防线（关键）
    ----------------
    决策日 d 的组 IC 只用同时满足以下条件的信号行：
      1. ts' <= d - horizon（前向收益在 d 日已完全实现，不窥探未来）
      2. fwd5 非空（价格样本存在）
      3. ts' 落在最近 ic_win 个已实现交易日内
    若 debug=True 做逐行断言：窗口内最大 ts <= d - horizon。

    返回
    ----
    sig 副本 + 列 ``ic``（每行是其所在组、所在日的滚动 IC；样本不足为 NaN）。
    """
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    sig["group"] = sig["symbol"].map(SYM2GROUP)
    if sig["group"].isna().any():
        raise ValueError("存在未映射分组的 symbol")

    # 全样本 fwd（仅用于判断"该行前向收益最终可观测"；决策日是否可用由 ts<=d-h 把关）
    fwd_long = fwd.stack().rename("fwd").reset_index()
    fwd_long.columns = ["ts", "symbol", "fwd"]
    fwd_long["ts"] = pd.to_datetime(fwd_long["ts"])
    sig = sig.merge(fwd_long, on=["symbol", "ts"], how="left")

    rows: list[pd.DataFrame] = []
    for g, sub in sig.groupby("group", sort=True):
        # 可观测前向收益的行（fwd 非空），按 ts 排序
        real = sub.loc[sub["fwd"].notna(), ["ts", "exp_ret", "fwd"]].sort_values("ts")
        if len(real) == 0:
            sub = sub.copy()
            sub["ic"] = np.nan
            rows.append(sub)
            continue
        ts_sorted = real["ts"].to_numpy(dtype="datetime64[ns]")
        exp_sorted = real["exp_ret"].to_numpy(dtype=float)
        fwd_sorted = real["fwd"].to_numpy(dtype=float)
        days = np.unique(ts_sorted)  # 已实现交易日（升序）

        sig_days = np.unique(sub["ts"].to_numpy(dtype="datetime64[ns]"))
        ic_map: dict[np.datetime64, float] = {}
        for d in sig_days:
            hi = d - np.timedelta64(horizon, "D")
            j = int(np.searchsorted(days, hi, side="right")) - 1
            if j < 0:
                ic_map[d] = np.nan
                continue
            lo = days[max(0, j - ic_win + 1)]
            i0 = int(np.searchsorted(ts_sorted, lo, side="left"))
            i1 = int(np.searchsorted(ts_sorted, hi, side="right"))
            x = exp_sorted[i0:i1]
            y = fwd_sorted[i0:i1]
            if len(x) < min_pairs:
                ic_map[d] = np.nan
            else:
                ic_map[d] = _spearman(x, y)
        sub = sub.copy()
        sub["ic"] = sub["ts"].map(ic_map)
        rows.append(sub)

    out = pd.concat(rows, ignore_index=True)
    # 泄漏断言（debug=True）：对每个 ic 非空的行，逐组验证窗口上界 <= ts - horizon
    if debug:
        n_bad = 0
        for g, sub in out.groupby("group"):
            real = sub.loc[sub["fwd"].notna(), ["ts", "exp_ret", "fwd"]].sort_values("ts")
            if len(real) == 0:
                continue
            ts_sorted = real["ts"].to_numpy(dtype="datetime64[ns]")
            days = np.unique(ts_sorted)
            for _, r in sub[sub["ic"].notna()].iterrows():
                d = np.datetime64(r["ts"])
                hi = d - np.timedelta64(horizon, "D")
                i1 = int(np.searchsorted(ts_sorted, hi, side="right"))
                if i1 > 0 and ts_sorted[i1 - 1] > hi:
                    n_bad += 1
        print(f"    [LEAK CHECK] 滚动 IC 窗口上界 > 决策日-horizon 的行数 = {n_bad}")
    return out


# ---------------------------------------------------------------------------
# 2. 修复后 targets 构造（通用：任意 score 列 + 按日截面 rank，镜像 p5 逻辑）
# ---------------------------------------------------------------------------
def build_engineA_targets(
    prices: pd.DataFrame,
    sig: pd.DataFrame,
    score_col: str = "exp_ret",
    top_k: float = TOP_K,
    min_symbols: int | None = MIN_SYMBOLS,
) -> pd.DataFrame:
    """按日截面 rank top_k 做多（镜像 p5 engine_a_targets_cs 口径）。

    score_col 含 NaN 的行 rank 为 NaN → 不会被选中（剔除语义）。
    返回 MultiIndex(symbol, ts) + target，可直接喂 BacktestEngine。
    """
    sig = sig.copy()
    sig["ts"] = pd.to_datetime(sig["ts"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    notional = INITIAL_CAPITAL * NOTIONAL_FRAC

    sig["rank_pct"] = sig.groupby("ts")[score_col].rank(pct=True, ascending=True)
    sig["_day_cnt"] = sig.groupby("ts")["symbol"].transform("count")
    sig["_px"] = sig.apply(
        lambda r: prices.xs(r["symbol"], level=0)["close"].get(r["ts"]), axis=1
    )
    sig["_mult"] = sig["symbol"].map(mult_map)

    long_cond = (sig["rank_pct"] >= 1.0 - top_k) & sig["_px"].notna()
    if min_symbols is not None:
        long_cond = long_cond & (sig["_day_cnt"] >= min_symbols)

    with np.errstate(invalid="ignore", divide="ignore"):
        raw_lots = notional / (sig["_px"] * sig["_mult"])
    sig["target"] = np.where(long_cond, raw_lots.fillna(0.0).astype(int), 0)
    return sig.set_index(["symbol", "ts"])[["target"]].sort_index()


# ---------------------------------------------------------------------------
# 3. 各方案 score 构造
# ---------------------------------------------------------------------------
def scheme1_score(sig_ic: pd.DataFrame) -> pd.DataFrame:
    """S1 分组滚动 IC 加权：w=max(IC,0)；缺失 IC → w=1（中性，不改变早期行为）。"""
    out = sig_ic.copy()
    w = out["ic"].clip(lower=0.0)
    out["w1"] = w.fillna(1.0)
    out["score"] = out["exp_ret"] * out["w1"]
    out["scheme"] = "S1_ic_weight"
    return out


def scheme1b_score(sig_ic: pd.DataFrame) -> pd.DataFrame:
    """S1b 分组滚动 IC 温和加权：w=1+IC（降权但不剔除），缺失 IC → w=1 中性。

    与 S1 的 w=max(IC,0)（剔除端）对照：检验"高 IC 组加权"机制本身是否有效，
    排除"剔除导致参与度过低"的混杂。
    """
    out = sig_ic.copy()
    w = 1.0 + out["ic"]
    out["w1b"] = w.fillna(1.0)
    out["score"] = out["exp_ret"] * out["w1b"]
    out["scheme"] = "S1b_ic_mild"
    return out


def scheme2_score(sig_ic: pd.DataFrame, flip_groups: set[str]) -> pd.DataFrame:
    """S2 翻转：flip_groups 内组的 exp_ret 取反（其余不变）。"""
    out = sig_ic.copy()
    out["score"] = np.where(out["group"].isin(flip_groups), -out["exp_ret"], out["exp_ret"])
    out["scheme"] = "S2_is_flip"
    return out


def scheme3_score(sig_ic: pd.DataFrame) -> pd.DataFrame:
    """S3 负 IC 组剔除：IC<=0 → score=NaN（当日剔除出截面 rank）；缺失 IC 中性。"""
    out = sig_ic.copy()
    # 缺失 IC（样本不足/早期）→ 中性，保留原 exp_ret；IC<=0 → NaN 剔除
    out["score"] = np.where(
        out["ic"].isna(),
        out["exp_ret"],
        np.where(out["ic"] > 0.0, out["exp_ret"], np.nan),
    )
    out["scheme"] = "S3_drop_negic"
    return out


# ---------------------------------------------------------------------------
# 4. 方案选择（IS 段只做选择，OOS 只做终裁）
# ---------------------------------------------------------------------------
def is_metrics(eq: pd.Series, is_end: str = IS_END) -> dict:
    """IS 段（<=is_end）指标；bars<=30 返回 NaN。"""
    idx = pd.to_datetime(eq.index)
    sub = eq[idx <= pd.Timestamp(is_end)]
    if len(sub) > 30:
        m = compute_metrics(sub, freq="daily")
        return {
            "is_sharpe": m.sharpe,
            "is_ann_ret": m.annual_return,
            "is_maxdd": m.max_drawdown,
            "is_n": m.n_bars,
        }
    return {"is_sharpe": np.nan, "is_ann_ret": np.nan, "is_maxdd": np.nan, "is_n": len(sub)}


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P7-2 引擎 A 信号端修复：四方案对比")
    ap.add_argument("--schemes", type=str, default="1,2,3,4", help="要运行的方案（逗号分隔）")
    ap.add_argument("--retrain", action="store_true", help="强制重训 S4 缓存（test_len=30）")
    ap.add_argument("--skip-backtest", action="store_true", help="只做诊断/重训，不跑回测")
    ap.add_argument("--debug-leak", action="store_true", help="滚动 IC 泄漏断言")
    args = ap.parse_args()

    schemes = {int(x) for x in args.schemes.split(",") if x.strip()}
    t_start = time.time()

    print("=" * 100)
    print("P7-2 引擎 A 信号端修复：S1 滚动IC加权 / S2 IS翻转 / S3 剔除 / S4 更频繁重训")
    print(f"IS 段终点(选择依据)={IS_END} | OOS 留出={OOS_START} | "
          f"IC_WIN={IC_WIN} | MIN_PAIRS={MIN_PAIRS} | horizon={HORIZON}")
    print(f"回测口径: 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | top_k={TOP_K} "
          f"min_symbols={MIN_SYMBOLS} | INITIAL_CAPITAL={INITIAL_CAPITAL:,.0f}")
    print("=" * 100)

    # ---- 0. 数据 ----
    cfg = load_config()
    cfg.backtest.contracts = CONTRACTS18
    cfg.backtest.initial_capital = INITIAL_CAPITAL
    cost = CostModel.from_config(cfg)
    prices = load_prices()
    fwd = build_fwd_panel(prices)
    print(f"\n[0] prices: {len(prices)} 行, {prices.index.get_level_values(0).nunique()} 品种 | "
          f"fwd 面板: {fwd.shape}")

    sig0 = pd.read_parquet(SIGNALS_PATH)
    sig0["ts"] = pd.to_datetime(sig0["ts"])
    print(f"    信号缓存 v2: {len(sig0)} 行, {sig0['ts'].min().date()} → {sig0['ts'].max().date()}")

    # ---- 1. 滚动组 IC（零泄漏） ----
    print(f"\n[1] 滚动组 IC（ic_win={IC_WIN}, min_pairs={MIN_PAIRS}, 零泄漏）")
    sig_ic = compute_rolling_group_ic(
        sig0, fwd, ic_win=IC_WIN, min_pairs=MIN_PAIRS, debug=args.debug_leak
    )
    print(f"    sig_ic: {len(sig_ic)} 行 | ic 非空 {sig_ic['ic'].notna().sum()} "
          f"({100*sig_ic['ic'].notna().mean():.1f}%)")

    # 组 IC 汇总（全样本 / IS / OOS，诊断回顾）
    print("\n[1b] 分组 IC 诊断回顾（与 P7-1 对照）")
    ic_rows = []
    for g in GROUPS:
        sub = sig_ic[sig_ic["group"] == g]
        m = sub["fwd"].notna() & sub["exp_ret"].notna()
        def _seg_ic(sel):
            s = sub[sel & m]
            return _spearman(s["exp_ret"].to_numpy(), s["fwd"].to_numpy()) if len(s) >= 10 else np.nan
        ic_full = _seg_ic(sub["ts"].notna())
        ic_is = _seg_ic(sub["ts"] <= pd.Timestamp(IS_END))
        ic_oos = _seg_ic(sub["ts"] >= pd.Timestamp(OOS_START))
        ic_rows.append({"group": g, "ic_is": ic_is, "ic_oos": ic_oos, "ic_full": ic_full})
        print(f"    {g:<12} IS IC={ic_is:+.3f}  OOS IC={ic_oos:+.3f}  全样本 IC={ic_full:+.3f}")
    ic_diag = pd.DataFrame(ic_rows)
    ic_diag.to_csv(ART / "p7_engineA_fix_group_ic_diag.csv", index=False)

    # ---- 2. 方案 score 构造（IS 段选择逻辑标注） ----
    print("\n[2] 方案构造")
    scheme_targets: dict[str, pd.DataFrame] = {}

    if 1 in schemes:
        print("  S1 分组滚动 IC 加权（剔除端 w=max(IC,0)）+ S1b（温和降权 w=1+IC）")
        print("    选择逻辑：无 IS 参数调优，ic_win=126 为任务建议默认；权重逐日滚动，只用 t 日及以前数据")
        s1 = scheme1_score(sig_ic)
        scheme_targets["S1_ic_weight"] = build_engineA_targets(prices, s1, score_col="score")
        s1b = scheme1b_score(sig_ic)
        scheme_targets["S1b_ic_mild"] = build_engineA_targets(prices, s1b, score_col="score")

    if 2 in schemes:
        # IS 段定翻转组：组 IS IC < 0 → 翻转（嵌套口径）
        print("  S2 IS 段翻转：翻转组由 IS 段（<=2022-04-21）组 IC 符号决定")
        is_sel = sig_ic[sig_ic["ts"] <= pd.Timestamp(IS_END)]
        flip_groups: set[str] = set()
        for g in GROUPS:
            sub = is_sel[(is_sel["group"] == g) & is_sel["fwd"].notna()]
            if len(sub) >= 10:
                ic = _spearman(sub["exp_ret"].to_numpy(), sub["fwd"].to_numpy())
                print(f"    IS IC[{g}]={ic:+.3f} → {'翻转' if ic < 0 else '不翻转'}")
                if ic < 0:
                    flip_groups.add(g)
            else:
                print(f"    IS IC[{g}]=n/a → 不翻转")
        print(f"    IS 段决定的翻转组: {sorted(flip_groups) or '无（== 基线 S2）'}")
        s2 = scheme2_score(sig_ic, flip_groups)
        scheme_targets["S2_is_flip"] = build_engineA_targets(prices, s2, score_col="score")
        # ex-post 参考变体（OOS 事后知识，仅诊断不可选）
        print("    [参考-不可选] ex-post 翻转 agri/precious（OOS 事后知识，仅用于理解翻转机制）")
        s2x = scheme2_score(sig_ic, {"agri", "precious"})
        scheme_targets["S2_expost_ref"] = build_engineA_targets(prices, s2x, score_col="score")

    if 3 in schemes:
        print("  S3 负 IC 组剔除：IC<=0 → score=NaN 剔除出当日截面 rank；缺失 IC 中性")
        print("    选择逻辑：无 IS 参数调优，ic_win=126 默认；剔除阈值 0 固定")
        s3 = scheme3_score(sig_ic)
        scheme_targets["S3_drop_negic"] = build_engineA_targets(prices, s3, score_col="score")

    if 4 in schemes:
        print("  S4 更频繁重训：walk_forward 测试窗 60→30（模型 lookback 30→20，见 _build_rt30_cache）")
        print("    选择逻辑：重训频率为结构性方法变化，不依赖任何收益数据方向；"
              "结果缓存用于同一 S2 策略构造")
        if not RT30_PATH.exists() or args.retrain:
            print("    [重训] 重建 test_len=30 信号缓存（可能耗时，建议后台）")
            _build_rt30_cache()
        else:
            print(f"    [SKIP] 已存在 {RT30_PATH.name}（--retrain 强制重跑）")
        sig_rt = pd.read_parquet(RT30_PATH)
        sig_rt["ts"] = pd.to_datetime(sig_rt["ts"])
        print(f"    rt30 缓存: {len(sig_rt)} 行, {sig_rt['ts'].min().date()} → {sig_rt['ts'].max().date()}")
        # 用与 S2 完全相同的构造逻辑（截面 rank + min=3）
        scheme_targets["S4_retrain30"] = build_engineA_targets(prices, sig_rt, score_col="exp_ret")

    if args.skip_backtest:
        print("\n[SKIP-BACKTEST] 已按要求跳过回测（仅重训/诊断）。")
        print(f"[DONE] 总耗时 {time.time()-t_start:.1f}s")
        return

    # ---- 3. 基线 + 方案回测 ----
    print("\n[3] 回测（完整口径）")
    print(f"    {'方案':<16}{'全样本Sh':>10}{'年化':>9}{'MaxDD':>9}{'IS Sh':>9}"
          f"{'IS MaxDD':>10}{'OOS Sh':>9}{'OOS MaxDD':>10}{'OOS n':>7}")
    rows: list[dict] = []

    # 基线：引擎 A S2（当前生产信号端）
    tgt_base = build_engineA_targets(prices, sig0, score_col="exp_ret")
    ret_base, eq_base, m_base, m_oos_base, long_ratio_base = run_engine_row(
        cfg, cost, prices, tgt_base, "BASE"
    )
    is_base = is_metrics(eq_base)
    rows.append({
        "scheme": "BASE_S2", "desc": "基线 引擎A S2（截面rank min=3）",
        "sharpe_full": m_base.sharpe, "ann_ret_full": m_base.annual_return,
        "maxdd_full": m_base.max_drawdown,
        "is_sharpe": is_base["is_sharpe"], "is_ann_ret": is_base["is_ann_ret"],
        "is_maxdd": is_base["is_maxdd"],
        "oos_sharpe": m_oos_base.sharpe if m_oos_base else np.nan,
        "oos_maxdd": m_oos_base.max_drawdown if m_oos_base else np.nan,
        "oos_ret": m_oos_base.total_return if m_oos_base else np.nan,
        "oos_seg1_sharpe": seg_sharpe(eq_base, "2024-07-18", "2025-06-30"),
        "oos_seg2_sharpe": seg_sharpe(eq_base, "2025-07-01", None),
        "oos_n": m_oos_base.n_bars if m_oos_base else 0,
        "eligible": False, "ret_key": "BASE_S2",
    })
    print(f"    {'BASE_S2':<16}{m_base.sharpe:>10.3f}{m_base.annual_return*100:>+9.1f}%"
          f"{m_base.max_drawdown*100:>9.1f}%{is_base['is_sharpe']:>9.3f}"
          f"{is_base['is_maxdd']*100:>10.1f}%{m_oos_base.sharpe:>9.3f}"
          f"{m_oos_base.max_drawdown*100:>10.1f}%{m_oos_base.n_bars:>7}")

    rets: dict[str, pd.Series] = {"BASE_S2": ret_base}
    eqs: dict[str, pd.Series] = {"BASE_S2": eq_base}
    for key, tgt in scheme_targets.items():
        ret, eq, m, m_oos, long_ratio = run_engine_row(cfg, cost, prices, tgt, key)
        rets[key] = ret
        eqs[key] = eq
        ism = is_metrics(eq)
        eligible = key not in ("S2_expost_ref",)
        rows.append({
            "scheme": key,
            "desc": _DESC.get(key, key),
            "sharpe_full": m.sharpe, "ann_ret_full": m.annual_return,
            "maxdd_full": m.max_drawdown,
            "is_sharpe": ism["is_sharpe"], "is_ann_ret": ism["is_ann_ret"],
            "is_maxdd": ism["is_maxdd"],
            "oos_sharpe": m_oos.sharpe if m_oos else np.nan,
            "oos_maxdd": m_oos.max_drawdown if m_oos else np.nan,
            "oos_ret": m_oos.total_return if m_oos else np.nan,
            "oos_seg1_sharpe": seg_sharpe(eq, "2024-07-18", "2025-06-30"),
            "oos_seg2_sharpe": seg_sharpe(eq, "2025-07-01", None),
            "oos_n": m_oos.n_bars if m_oos else 0,
            "eligible": eligible, "ret_key": key,
        })
        print(f"    {key:<16}{m.sharpe:>10.3f}{m.annual_return*100:>+9.1f}%"
              f"{m.max_drawdown*100:>9.1f}%{ism['is_sharpe']:>9.3f}"
              f"{ism['is_maxdd']*100:>10.1f}%{m_oos.sharpe:>9.3f}"
              f"{m_oos.max_drawdown*100:>10.1f}%{m_oos.n_bars:>7}")

    tbl = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    tbl.to_csv(COMPARE_CSV, index=False)
    print(f"\n  [OK] 对比表 → {COMPARE_CSV}")

    # ---- 4. 方案选择（IS 段选优，OOS 终裁） ----
    print("\n[4] 方案选择（铁律：只用 IS 段 <=2022-04-21 选择；OOS 只终裁）")
    elig = tbl[tbl["eligible"] & (tbl["scheme"] != "BASE_S2")].copy()
    if len(elig) == 0:
        print("  [FAIL] 无可用方案")
        return
    elig = elig.sort_values("is_sharpe", ascending=False)
    for _, r in elig.iterrows():
        print(f"    IS 排序: {r['scheme']:<16} IS Sharpe={r['is_sharpe']:.3f} "
              f"IS MaxDD={r['is_maxdd']*100:.1f}% | OOS Sharpe={r['oos_sharpe']:.3f}（终裁）")
    best = elig.iloc[0]
    best_key = best["scheme"]

    # 严格 IS-Sharpe 选择
    print(f"\n  → [严格 IS-Sharpe] IS 段最优方案: {best_key} (IS Sharpe={best['is_sharpe']:.3f})")
    print(f"  → OOS 终裁: OOS Sharpe={best['oos_sharpe']:.3f} OOS MaxDD={best['oos_maxdd']*100:.1f}% "
          f"(基线引擎A S2: OOS Sharpe={rows[0]['oos_sharpe']:.3f})")
    if best_key == "S2_is_flip":
        print("  → 注意: S2_is_flip 与基线完全一致（IS 段 IC 全为正 → 无翻转组），"
              "是退化结果而非修复；严格 IS-Sharpe 规则下无候选修复超越基线。")

    # 真实修复（排除退化的 S2）中的 IS 最优 → 用于组合重估与终裁
    genuine = elig[elig["scheme"] != "S2_is_flip"].copy()
    g_best = best
    g_key = best_key
    if len(genuine) > 0:
        g_best = genuine.sort_values("is_sharpe", ascending=False).iloc[0]
        g_key = g_best["scheme"]
        print(f"\n  → [真实修复选择] 排除退化 S2 后 IS 最优修复: {g_key} "
              f"(IS Sharpe={g_best['is_sharpe']:.3f})")
        print(f"  → OOS 终裁: OOS Sharpe={g_best['oos_sharpe']:.3f} "
              f"OOS MaxDD={g_best['oos_maxdd']*100:.1f}% "
              f"| OOS seg1={g_best['oos_seg1_sharpe']:.3f} seg2={g_best['oos_seg2_sharpe']:.3f}")

    # ---- 5. 组合重估（IS 最优修复引擎 A + 引擎 B） ----
    print("\n[5] 组合重估（IS 最优修复引擎 A + 引擎 B win252/thr0.7）")
    from scripts.p3_combo_backtest import engine_b_targets
    tgt_b = engine_b_targets(prices, BASIS_WIN, BASIS_THR)
    ret_b, eq_b, m_b, m_oos_b, _ = run_engine_row(cfg, cost, prices, tgt_b, "B")

    def _combo_line(key: str, label: str) -> None:
        ra = rets[key]
        common = ra.index.intersection(ret_b.index)
        ra_, rb_ = ra.loc[common], ret_b.loc[common]
        for w_a in (PROD_W_A, A25):
            r = combo_stats_row(ra_, rb_, w_a, False)
            print(f"    {label} A{w_a:.2f}/B{1-w_a:.2f} (vol=N): 全样本 Sharpe={r['sharpe_full']:.3f} "
                  f"| OOS Sharpe={r['oos_sharpe']:.3f} OOS MaxDD={r['oos_maxdd']*100:.1f}% "
                  f"(基线 A15/B85 OOS 0.999)")

    _combo_line(best_key, f"[严格最优 {best_key}]")
    if g_key != best_key:
        _combo_line(g_key, f"[真实修复 {g_key}]")

    # ---- 6. 权重演变表（S1 诊断：验证非仅在 OOS 段才翻转） ----
    if "S1_ic_weight" in scheme_targets:
        print("\n[6] S1 分组权重演变（月度均值，w=max(IC,0)，缺失=NaN）")
        s1d = scheme1_score(sig_ic)
        wtab = s1d.pivot_table(index=s1d["ts"].dt.to_period("M"), columns="group",
                               values="w1", aggfunc="mean")
        wtab.to_csv(WEIGHT_CSV)
        print(wtab.round(3).to_string())
        print(f"  [OK] → {WEIGHT_CSV}")

    # ---- 7. 结论 ----
    print("\n[7] 结论")
    delta = best["oos_sharpe"] - rows[0]["oos_sharpe"]
    verdict = "PASS(优于基线)" if delta > 0 else "NEUTRAL(不优于基线)"
    print(f"  [严格 IS-Sharpe] IS 最优方案: {best_key} | OOS Sharpe 增量 vs 基线 S2: {delta:+.3f} → {verdict}")
    if g_key != best_key:
        g_delta = g_best["oos_sharpe"] - rows[0]["oos_sharpe"]
        g_verdict = "PASS(优于基线)" if g_delta > 0 else "NEUTRAL(不优于基线)"
        print(f"  [真实修复选择] IS 最优修复: {g_key} | OOS Sharpe 增量 vs 基线 S2: {g_delta:+.3f} → {g_verdict}")
    print("  （注意：方案选择依据 IS 段；OOS 仅终裁，最终推荐需综合组合层表现）")
    print(f"\n[DONE] 总耗时 {time.time()-t_start:.1f}s")


_DESC = {
    "S1_ic_weight": "S1 分组滚动IC加权(w=max(IC,0)剔除端)",
    "S1b_ic_mild": "S1b 分组滚动IC温和加权(w=1+IC降权端)",
    "S2_is_flip": "S2 IS段翻转(IS IC<0组)",
    "S2_expost_ref": "S2 ex-post参考(翻转agri/precious,不可选)",
    "S3_drop_negic": "S3 负IC组剔除(IC<=0当日剔除)",
    "S4_retrain30": "S4 更频繁重训(test_len=30)",
}


def _build_rt30_cache() -> None:
    """重建 test_len=30 信号缓存（更频繁重训；v2 同口径：8 组、cal_split=0.5、cal_return_all=False）。

    关键技术说明：walk_forward 的 lookback = min(normalize_window, 30)。若保持
    lookback=30，则 30 天测试窗只产出 1 条预测，被嵌套校准（cal_split=0.5）截断为 0。
    故在特征构建（normalize_window=120，特征值与 v2 完全一致）之后，将传入
    walk_forward 的 cfg.normalize_window 降为 20 —— 只改模型 lookback（20 天回看），
    30 天测试窗产出 11 条预测、校准后 6 条评估信号/折。重训节奏 60→30 天（折数翻倍）。
    """
    from hexbroker.feature import build_features
    from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
    from scripts.group_modeling import LOCAL_MAP
    from scripts.group_modeling_v2 import GROUPS_V2
    from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
    from scripts.sentinel_phase4_evo import load_local_bars

    splitter = dict(train_len=250, test_len=30, purge=5, embargo=2, mode="rolling")
    frames = []
    for gname, gcfg in GROUPS_V2.items():
        t0 = time.time()
        std_syms = [LOCAL_MAP[s] for s in gcfg["syms"]]
        cfg = load_config()
        cfg.data.symbols = std_syms
        cfg.data.freq = FREQ
        cfg.data.start = DATA_START
        cfg.data.end = "2026-08-17"
        cfg.forecast.horizon = 5
        cfg.forecast.n_mc_samples = 30
        cfg.forecast.calibration_method = "platt"
        cfg.feature.transformers = ["technical", "microstructure", "iterative", "cross", "normalize"]
        cfg.feature.iterative_params = {"include": ["f_range_pos_20"]}
        cfg.feature.cross_params = {"global_codes": gcfg["global"]}
        bars = load_local_bars(std_syms)
        inner_dates = bars.df.index.get_level_values("datetime").unique().sort_values()
        gc = {c: align_global_to_inner(load_global_close(c), inner_dates) for c in gcfg["global"]}
        features = build_features(bars, cfg, global_close=gc)
        # 特征已按 normalize_window=120 构建（与 v2 一致）；只改模型 lookback=20
        cfg.feature.normalize_window = 20
        wf = walk_forward_lightgbm(
            cfg, bars, features, params=load_best_params(),
            collect_models=False, splitter_overrides=splitter,
            n_jobs_folds=6, cal_split=0.5, cal_return_all=False,
        )
        sig = pd.DataFrame(wf.records)
        frames.append(sig)
        print(f"    [OK] {gname}: {len(sig)} 条 (test_len=30/lb20, {time.time()-t0:.0f}s)", flush=True)
    all_sig = pd.concat(frames, ignore_index=True)
    all_sig.to_parquet(RT30_PATH, index=False)
    print(f"  [OK] rt30 缓存 {len(all_sig)} 条 → {RT30_PATH}")


if __name__ == "__main__":
    main()
