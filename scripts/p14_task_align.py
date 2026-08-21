"""P14 引擎 A 标签/任务错配深层修复：分类目标（P14-1）+ 校准器修复（P14-2）。

立项背景
--------
引擎 A 生产状态（P8-4/P9/P13 定案，QA VERIFIED）：
- v8 信号缓存 ``artifacts/signals_cache18_grouped_v8.parquet``（label_pool=all +
  cross_z 标签截面化，8,624 行 / 18 品种）；EngineAConfig 固化
  （top_k=0.30 / S2 min=3 / group_cap=0.5 / group_map=ferrous_all）。
- 单引擎 OOS Sharpe ~0.646、OOS 复利 ~+9.01%、组合 A10/B90 OOS ~1.614；
  但 **OOS 截面 IC 仍负（-0.064）**——盈利靠黑色系敞口。
- P13 定论：多模型融合（LGB+XGB+HGB）改善 IC（-0.039~-0.061）但未转化收益
  （cap 口径 F2 0.597 < v8 0.646 反转）→ **瓶颈不在模型多样性，在标签/任务错配**：
  训练=回归预测收益水平（cross_z 标签），推理=截面选 top30% 做多——两目标不同。
- P6-3 附带发现：校准器从未执行——60 日测试窗→lookback 30→仅 31 条信号→
  k=15 < 20 最小样本阈值→scaler=None（p_up 从未真正校准）。

本脚本（自包含编排，两个低风险高信息量实验）
--------------------------------------------
P14-1 分类目标实验（v10_cls）：
  - 新增 ``label_mode="cross_top30"``（scripts/refine_lightgbm_champion.py）：
    标签 y = 当日**全品种截面**（label_pool=all 口径）fwd 收益是否在 top30% → 0/1。
  - 新增分类模型 ``LGBMClassifyForecast``（scripts/p14_models.py）：
    LightGBM 二分类（objective=binary），输出概率 p_buy → exp_ret 列填 p_buy
    （映射说明：引擎 A 按 exp_ret 截面 rank 即按 p_buy rank，与「rank(p_buy) 选
    top30%」同构，engine_a_targets_cs 无需改选择逻辑）。
  - 同特征、同 walk_forward 网格（train_len=250/test_len=60/purge=5/embargo=2）、
    同分组 GROUPS_V2、cal_split=0.5、cal_return_all=False。
  - 产出 ``artifacts/signals_cache18_grouped_v10_cls.parquet``（列对齐 v8）。

P14-2 校准器修复 + p_up 选择（v10_cal）：
  - ``_calibrate_and_split`` 新增 ``cal_min_samples``（默认 20 与历史一致；P14 传
    10），并把已拟合校准器应用到评估子窗（cal_return_all=False 时），使输出 p_up
    为真校准值（历史口径 k=15<20 从不触发 → 既有缓存零影响）。
  - 只重训引擎 A 信号（v8 同口径 + cal_min_samples=10）→
    ``artifacts/signals_cache18_grouped_v10_cal.parquet``（含真实 p_up）。
  - 验证①：v10_cal 与 v8 同模型/同特征/同标签/同网格，LightGBM 训练确定性
    （subsample 路径无外部随机源）→ exp_ret 应逐字节一致；p_up 因校准生效而不同。
  - 验证②：engine_a_targets_cs 新增 ``score_col`` 参数（默认 exp_ret 保持既有
    行为）→ 对比「exp_ret 排序选择」vs「p_up 排序选择」（v10_cal_pup）。

重估（生产 cap 口径，P13 QA 复核 DISCREPANCY-1 教训）
----------------------------------------------------
  - 显式 ``cfg = load_config("configs/base.yaml")``（无 path 的 load_config 读不到
    base.yaml）→ ``group_cap=cfg.backtest.engine_a.group_cap`` /
    ``group_map=cfg.backtest.engine_a.group_map`` 显式传 ``engine_a_targets_cs``。
  - 复利口径评估、OOS 2024-07-18 后、完整回测（滑点1tick+费0.005%+保证金12%+
    CONTRACTS18）、单引擎 A-S2（min=3）+ 组合 A10/B90（volN/volY）+ OOS 截面 IC。
  - 落盘：artifacts/p14_compare.csv（v8 vs v10_cls vs v10_cal vs v10_cal_pup 全指标）
          artifacts/p14_ic_detail.csv、artifacts/p14_verdict.json、artifacts/p14_pup_verify.csv

口径铁律：嵌套零泄漏（标签截面化只用当日）；不改 v8 缓存、不改数据文件、
不改 hexbroker 包（仅 scripts 层扩展，见上）；新脚本独立。

用法
----
  python scripts/p14_task_align.py --train cls,cal --n-jobs 8     # Stage A 训练
  python scripts/p14_task_align.py --eval-only                     # Stage B 重估
  python scripts/p14_task_align.py --train cls --only-group precious  # 冒烟
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
from scripts.ablate_features import align_global_to_inner, load_best_params, load_global_close
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
from scripts.refine_lightgbm_champion import DATA_START, FREQ, walk_forward_lightgbm
from scripts.sentinel_phase4_evo import load_local_bars

from scripts.p14_models import LGBMClassifyForecast

ART = ROOT / "artifacts"
V8_PATH = ART / "signals_cache18_grouped_v8.parquet"
V10_CLS_PATH = ART / "signals_cache18_grouped_v10_cls.parquet"
V10_CAL_PATH = ART / "signals_cache18_grouped_v10_cal.parquet"
OUT_COMPARE = ART / "p14_compare.csv"
OUT_IC = ART / "p14_ic_detail.csv"
OUT_VERDICT = ART / "p14_verdict.json"
OUT_PUP_VERIFY = ART / "p14_pup_verify.csv"

# 口径常量（与 v8 / P13 完全一致）
LABEL_POOL = "all"            # v8 口径：全 18 品种统一截面
CAL_SPLIT = 0.5               # v8 口径：嵌套校准分割
CAL_RETURN_ALL = False        # v8 口径
CAL_MIN_SAMPLES = 10          # P14-2：调低校准最小样本阈值（默认 20 → 10）
MIN_SYMBOLS_S2 = 3            # P5 终裁：S2 截面 rank + min=3
COMBO_W_A = 0.10              # P9 终裁：A10/B90
COMBO_W_B = 0.90
TOP_K_FRAC = 0.30             # 分类标签 top 分位（= 引擎 A 选择分位，同构目标）


# ---------------------------------------------------------------------------
# Stage A：训练（与 build_group_signals / p13.build_group_signals_multi 同口径，
# 仅 label_mode / label_pool / cal_min_samples / model_cls 参数化）
# ---------------------------------------------------------------------------
def build_group_signals_p14(
    model_cls,
    params: dict,
    group_syms: list[str],
    global_codes: list[str],
    n_jobs: int,
    label_mode: str,
    label_pool: str,
    cal_min_samples: int,
) -> pd.DataFrame:
    """单组训练（镜像 scripts/group_modeling.build_group_signals + p13 参数化）。

    特征/标签面板/网格/校准分割全部与 v8 一致，仅模型类、标签口径、校准阈值
    参数化；``walk_forward_lightgbm`` 内部对 label_mode="cross_top30" 走
    ``_cross_top30_binarize`` 二元标签面板（P14-1）。
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
        label_mode=label_mode, label_pool=label_pool,
        cal_min_samples=cal_min_samples,
    )
    sig = pd.DataFrame(wf.records).set_index(["symbol", "ts"]).sort_index()
    sig = sig.loc[:, ~sig.columns.duplicated(keep="last")].reset_index()
    sig["ts"] = pd.to_datetime(sig["ts"])
    print(f"[OK] {group_syms}: {len(sig)} 条信号 (global={global_codes}, "
          f"fundamental={sorted(fund.keys())}, n_feat={len(features.df.columns)})")
    return sig


def train_cache(
    variant: str,
    n_jobs: int,
    only_groups: str = "",
    resume: bool = True,
) -> Path:
    """训练单个变体（GROUPS_V2 8 组）→ 写信号缓存 parquet，返回路径。

    variant：'cls'（P14-1 分类：cross_top30 + LGBMClassifyForecast）或
             'cal'（P14-2 校准修复：cross_z + 默认 LGBM + cal_min_samples=10）。
    resume=True（默认）：若目标缓存已存在，跳过已完成组，仅训练缺失组
    （每完成一组即 checkpoint 落盘，中断/崩溃后可续跑，不丢已完成组）。
    """
    if variant == "cls":
        model_cls, label_mode, cal_min_samples, out_path = (
            LGBMClassifyForecast, "cross_top30", 20, V10_CLS_PATH
        )
    elif variant == "cal":
        model_cls, label_mode, cal_min_samples, out_path = (
            None, "cross_z", CAL_MIN_SAMPLES, V10_CAL_PATH
        )
    else:
        raise ValueError(f"未知 variant={variant!r}（可选 cls/cal）")

    only = set(only_groups.split(",")) if only_groups else None
    params = load_best_params()
    t0 = time.time()
    print("=" * 88)
    print(f"[Stage A] 训练 {variant}（GROUPS_V2 8 组，n_jobs={n_jobs}）")
    print(f"  口径: label_mode={label_mode} label_pool={LABEL_POOL} "
          f"cal_split={CAL_SPLIT} cal_return_all={CAL_RETURN_ALL} "
          f"cal_min_samples={cal_min_samples} | model_cls={model_cls.__name__ if model_cls else 'LightGBM(默认)'}")
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
                print(f"[resume] {variant}: 已有缓存 {out_path.name}（{len(prev)} 条，"
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
        sig = build_group_signals_p14(
            model_cls, params, gcfg["syms"], gcfg["global"], n_jobs,
            label_mode, LABEL_POOL, cal_min_samples,
        )
        print(f"  [{gname}] 耗时 {time.time()-t1:.0f}s")
        frames.append(sig)
        # 每完成一组 checkpoint 落盘（崩溃/中断可续跑，不丢已完成组）
        ART.mkdir(exist_ok=True)
        all_sig = pd.concat(frames, ignore_index=True)
        all_sig = all_sig.drop_duplicates(subset=["symbol", "ts"], keep="last")
        all_sig.to_parquet(out_path, index=False)
        print(f"[checkpoint] {variant} 中间落盘 {len(all_sig)} 条 → {out_path.name}")

    if not frames:
        print(f"[OK] {variant}: 全部组已完成，跳过训练（缓存 {out_path.name} 已完整）")
        return out_path
    all_sig = pd.concat(frames, ignore_index=True)
    all_sig = all_sig.drop_duplicates(subset=["symbol", "ts"], keep="last")
    all_sig.to_parquet(out_path, index=False)
    print(f"[OK] {variant} 信号合并 {len(all_sig)} 条 → {out_path.name} "
          f"| 品种 {sorted(all_sig['symbol'].unique())} | 总耗时 {time.time()-t0:.0f}s")
    return out_path


# ---------------------------------------------------------------------------
# Stage B：重估（生产 cap 口径）
# ---------------------------------------------------------------------------
def load_cache(path: Path) -> pd.DataFrame:
    sig = pd.read_parquet(path)
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig


def cs_ic_on_score(sig: pd.DataFrame, realized: pd.Series, oos_start: str,
                   score_col: str) -> dict:
    """在指定打分列上计算 OOS 截面 IC（cs_ic_summary 硬编码 exp_ret → 临时替换）。"""
    s = sig.copy()
    if score_col != "exp_ret":
        s["exp_ret"] = s[score_col]
    return cs_ic_summary(s, realized, oos_start)


def evaluate_variant(
    cfg,
    cost,
    prices: pd.DataFrame,
    realized: pd.Series,
    ret_b: pd.Series,
    variant: str,
    cache_path: Path,
    select_col: str,
    group_cap: float | None = None,
    group_map: dict[str, str] | None = None,
) -> list[dict]:
    """单个变体的重估行（A-S2 / A10B90-N / A10B90-Y）。

    select_col：引擎 A 截面 rank / 排序所依据的打分列（"exp_ret" 或 "p_up"）。
    生产口径：group_cap / group_map 显式传入（P13 QA 复核 DISCREPANCY-1）。
    """
    sig = load_cache(cache_path)
    tgt_a = engine_a_targets_cs(
        prices, TOP_K, MIN_SYMBOLS_S2, cache_path=cache_path,
        group_cap=group_cap, group_map=group_map, score_col=select_col,
    )
    ret_a, eq_a, m_a, m_oos_a, long_ratio = run_engine_row(cfg, cost, prices, tgt_a, f"A-{variant}")
    summ = cs_ic_on_score(sig, realized, OOS_START, select_col)
    oos_ic = summ["oos"]
    rows = [{
        "variant": variant, "select_col": select_col, "engine": "A-S2",
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
        ra = ret_a.loc[ret_a.index.intersection(ret_b.index)]
        rb = ret_b.loc[ret_b.index.intersection(ret_a.index)]
        r = combo_stats_row(ra, rb, COMBO_W_A, vt)
        rows.append({
            "variant": variant, "select_col": select_col,
            "engine": f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}" + ("-volY" if vt else "-volN"),
            "sharpe_full": r["sharpe_full"], "ann_ret_full": r["ann_ret_full"],
            "maxdd_full": r["maxdd_full"],
            "oos_sharpe": r["oos_sharpe"], "oos_maxdd": r["oos_maxdd"],
            "oos_ret": r["oos_ret"], "long_day_ratio": np.nan,
            "oos_n": r["oos_n"], "oos_cs_ic": oos_ic["ic_mean"],
            "oos_cs_icir": oos_ic["icir"], "oos_cs_pos_ratio": oos_ic["pos_ratio"],
        })
    print(f"  [{variant:<6} sel={select_col:<7} A-S2] Sharpe={m_a.sharpe:.3f} "
          f"年化={m_a.annual_return*100:+.1f}% MaxDD={m_a.max_drawdown*100:.1f}% "
          f"| OOS Sharpe={m_oos_a.sharpe:.3f} OOS 复利={m_oos_a.total_return*100:+.2f}% "
          f"OOS MaxDD={m_oos_a.max_drawdown*100:.1f}% | OOS 截面IC={oos_ic['ic_mean']:+.4f} "
          f"(ICIR={oos_ic['icir']:+.3f}, pos={oos_ic['pos_ratio']*100:.0f}%)")
    return rows


# ---------------------------------------------------------------------------
# P14-2 ①：p_up 校准生效验证（v10_cal vs v8）
# ---------------------------------------------------------------------------
def verify_pup_calibration(v8: pd.DataFrame, v10_cal: pd.DataFrame) -> pd.DataFrame:
    """验证校准器真正生效：同模型/同特征/同标签 → exp_ret 应逐字节一致，
    p_up 因 Platt 校准而不同（否则 scaler 未拟合，p_up 仍为原始概率）。

    返回逐品种汇总行（n 对齐 / exp_ret 一致率 / p_up 不一致率 / 平均 |Δp_up|）。
    """
    v8 = v8.copy(); v8["ts"] = pd.to_datetime(v8["ts"])
    v10 = v10_cal.copy(); v10["ts"] = pd.to_datetime(v10["ts"])
    m = v8.merge(v10, on=["symbol", "ts"], suffixes=("_v8", "_cal"), how="inner")
    rows = []
    for sym, g in m.groupby("symbol"):
        n = len(g)
        exp_match = float(np.isclose(g["exp_ret_v8"], g["exp_ret_cal"],
                                     rtol=1e-9, atol=1e-12).mean())
        p_up_diff = float((np.abs(g["p_up_v8"] - g["p_up_cal"]) > 1e-9).mean())
        mean_abs_d = float(np.abs(g["p_up_v8"] - g["p_up_cal"]).mean())
        rows.append({
            "symbol": sym, "n_align": n,
            "exp_ret_match_rate": exp_match,
            "p_up_diff_rate": p_up_diff,
            "mean_abs_delta_pup": mean_abs_d,
        })
    out = pd.DataFrame(rows)
    # 抽样展示 3 个品种若干行（验证样例）
    sample = m.sample(min(12, len(m)), random_state=42)
    print("\n[P14-2 ①] p_up 校准生效抽样（v8 原始 vs v10_cal 校准）")
    print(sample[["symbol", "ts", "exp_ret_v8", "exp_ret_cal", "p_up_v8", "p_up_cal"]]
          .round(4).to_string(index=False))
    return out


# ---------------------------------------------------------------------------
# 最终裁决
# ---------------------------------------------------------------------------
def make_verdict(tbl: pd.DataFrame) -> dict:
    a = tbl[tbl["engine"] == "A-S2"].set_index(["variant", "select_col"])
    combo = tbl[tbl["engine"] == f"A{COMBO_W_A:.2f}/B{COMBO_W_B:.2f}-volN"].set_index(["variant", "select_col"])
    base = ("v8", "exp_ret")
    base_a_sh, base_combo_sh, base_ic = (
        a.loc[base, "oos_sharpe"], combo.loc[base, "oos_sharpe"], a.loc[base, "oos_cs_ic"]
    )
    print("=" * 96)
    print("最终裁决（复利口径；基线 v8）")
    print(f"  单引擎 OOS Sharpe 基线 {base_a_sh:+.3f} | 组合 A10/B90 OOS 基线 "
          f"{base_combo_sh:.3f} | OOS 截面 IC 基线 {base_ic:+.4f}")
    rows = []
    for key in (("v10_cls", "exp_ret"), ("v10_cal", "exp_ret"), ("v10_cal", "p_up")):
        if key not in a.index:
            continue
        rows.append({
            "variant": key[0], "select_col": key[1],
            "oos_sharpe": a.loc[key, "oos_sharpe"],
            "combo_oos_sharpe": combo.loc[key, "oos_sharpe"],
            "oos_cs_ic": a.loc[key, "oos_cs_ic"],
            "delta_oos_sharpe": a.loc[key, "oos_sharpe"] - base_a_sh,
            "delta_combo_oos_sharpe": combo.loc[key, "oos_sharpe"] - base_combo_sh,
            "delta_oos_cs_ic": a.loc[key, "oos_cs_ic"] - base_ic,
        })
    d = pd.DataFrame(rows)
    for _, r in d.iterrows():
        print(f"  [{r['variant']:<8} sel={r['select_col']:<7}] ΔOOS Sharpe={r['delta_oos_sharpe']:+.3f} "
              f"Δ组合={r['delta_combo_oos_sharpe']:+.3f} Δ截面IC={r['delta_oos_cs_ic']:+.4f}")

    # 择优（与 P13 同构的多准则规则）：
    #   1) OOS 截面 IC 改善（P14 核心目标：IC 转正/改善）
    #   2) 单引擎 OOS Sharpe 不降（基线 0.646）
    #   3) 组合 A10/B90 OOS Sharpe 不降（基线 1.614）
    # 同时满足三者 → ADOPT；仅 IC 改善 → CANDIDATE（不足采纳）；否则 NOT-ADOPT。
    candidates = [("v10_cls", "exp_ret"), ("v10_cal", "exp_ret"), ("v10_cal", "p_up")]
    candidates = [k for k in candidates if k in a.index]
    ic_improved = [k for k in candidates if a.loc[k, "oos_cs_ic"] > base_ic]
    all3 = [k for k in ic_improved
            if a.loc[k, "oos_sharpe"] > base_a_sh and combo.loc[k, "oos_sharpe"] > base_combo_sh]
    if all3:
        best = max(all3, key=lambda k: a.loc[k, "oos_cs_ic"])
        is_pass = True
        adopt = "ADOPT"
        reason = (f"{best[0]}(sel={best[1]}) 同时改善三项关键指标：OOS 截面 IC "
                  f"{base_ic:+.4f} → {a.loc[best, 'oos_cs_ic']:+.4f}，单引擎 OOS Sharpe "
                  f"{base_a_sh:+.3f} → {a.loc[best, 'oos_sharpe']:+.3f}，组合 A10/B90 OOS "
                  f"{base_combo_sh:.3f} → {combo.loc[best, 'oos_sharpe']:.3f}")
    elif ic_improved:
        best = max(ic_improved, key=lambda k: a.loc[k, "oos_cs_ic"])
        is_pass = False
        adopt = "CANDIDATE"
        reason = (f"分类目标 / p_up 选择虽改善 OOS 截面 IC（{base_ic:+.4f} → "
                  f"{a.loc[best, 'oos_cs_ic']:+.4f}，最大者 {best[0]}(sel={best[1]})），"
                  f"但单引擎 OOS Sharpe（基线 {base_a_sh:+.3f}）或组合 A10/B90 "
                  f"（基线 {base_combo_sh:.3f}）未同时超过基线 → 只宜作候选")
    else:
        best = None
        is_pass = False
        adopt = "NOT-ADOPT"
        reason = (f"分类目标与 p_up 选择的 OOS 截面 IC 均未改善（基线 {base_ic:+.4f}）"
                  f"，且单引擎/组合未同时提升 → 标签/任务错配修复未在本实验窗口生效，"
                  f"如实报告无改善")
    print(f"  采纳建议: {adopt} —— {reason}")
    return {
        "is_pass": is_pass,
        "adopt": adopt,
        "best": best,
        "baseline": {"oos_sharpe_a": float(base_a_sh), "combo_oos_sharpe": float(base_combo_sh),
                     "oos_cs_ic": float(base_ic)},
        "details": {f"{r['variant']}__{r['select_col']}": {
            k: (float(r[k]) if pd.notna(r[k]) else None)
            for k in ("delta_oos_sharpe", "delta_combo_oos_sharpe",
                      "delta_oos_cs_ic", "oos_sharpe", "combo_oos_sharpe", "oos_cs_ic")}
            for _, r in d.iterrows()},
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="P14 引擎 A 标签/任务错配深层修复（分类目标 + 校准修复）")
    ap.add_argument("--train", type=str, default="",
                    help="逗号分隔要训练的变体（cls/cal），空=不训练")
    ap.add_argument("--eval-only", action="store_true",
                    help="只做重估（需 v8/v10_cls/v10_cal 缓存已存在；缺失变体自动跳过）")
    ap.add_argument("--n-jobs", type=int, default=8)
    ap.add_argument("--only-group", type=str, default="",
                    help="只训练指定组（逗号分隔，冒烟用）")
    args = ap.parse_args()

    t_start = time.time()
    print("=" * 96)
    print("P14 引擎 A 标签/任务错配深层修复（P14-1 分类目标 / P14-2 校准器修复）")
    print(f"OOS 起点 {OOS_START} | 口径 滑点1tick+费0.005%+保证金12%+CONTRACTS18 | "
          f"label_pool={LABEL_POOL} cal_split={CAL_SPLIT} cal_return_all={CAL_RETURN_ALL} "
          f"cal_min_samples={CAL_MIN_SAMPLES}")
    print("=" * 96)

    # ---- Stage A: 训练 ----
    if args.train:
        for vt in [v.strip() for v in args.train.split(",") if v.strip()]:
            if vt not in ("cls", "cal"):
                print(f"[WARN] 跳过未知变体 {vt!r}（可选 cls/cal）")
                continue
            train_cache(vt, args.n_jobs, args.only_group)

    if not args.eval_only and not args.train:
        print("[FAIL] 未指定 --train 或 --eval-only")
        return

    # ---- Stage B: 重估（生产 cap 口径） ----
    need = {"v8": V8_PATH, "v10_cls": V10_CLS_PATH, "v10_cal": V10_CAL_PATH}
    missing = [str(p) for n, p in need.items() if not p.exists()]
    if missing:
        print(f"[FAIL] 缺少缓存：{missing}（先运行 --train cls,cal）")
        return

    print("-" * 96)
    print("[Stage B] 环境：prices / 引擎 B / realized（生产口径：显式 base.yaml + group_cap/group_map）")
    # P13 QA 复核（DISCREPANCY-1）：必须显式加载 configs/base.yaml 并以参数传入
    # engine_a_targets_cs——其内部 _resolve_* 用无 path load_config() 读不到
    # base.yaml 的 group_cap=0.5 / ferrous_all。
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
    print("[Stage B] 重估：v8 vs v10_cls vs v10_cal(exp_ret) vs v10_cal(p_up)（生产口径）")
    variants = [
        ("v8", V8_PATH, "exp_ret"),
        ("v10_cls", V10_CLS_PATH, "exp_ret"),
        ("v10_cal", V10_CAL_PATH, "exp_ret"),
        ("v10_cal", V10_CAL_PATH, "p_up"),
    ]
    rows: list[dict] = []
    for variant, path, select_col in variants:
        if not path.exists():
            print(f"  [WARN] {path.name} 不存在，跳过 {variant}")
            continue
        rows.extend(evaluate_variant(cfg, cost, prices, realized, ret_b, variant, path,
                                     select_col, group_cap=group_cap, group_map=group_map))
    tbl = pd.DataFrame(rows)
    ART.mkdir(exist_ok=True)
    tbl.to_csv(OUT_COMPARE, index=False)
    print(f"  [OK] → {OUT_COMPARE}")

    print("-" * 96)
    print("[Stage B] OOS 截面 IC 明细（各变体按选择打分列）")
    ic_rows = []
    for variant, path, select_col in variants:
        if not path.exists():
            continue
        sig = load_cache(path)
        summ = cs_ic_on_score(sig, realized, OOS_START, select_col)
        for seg in ("full", "is", "oos"):
            st = summ[seg]
            ic_rows.append({
                "variant": variant, "select_col": select_col, "segment": seg,
                "ic": st["ic_mean"], "icir": st["icir"], "pos_ratio": st["pos_ratio"],
                "n_days": st["n_days"],
            })
    ic_tbl = pd.DataFrame(ic_rows)
    ic_tbl.to_csv(OUT_IC, index=False)
    print(ic_tbl.pivot_table(index=["variant", "select_col"], columns="segment",
                             values="ic").round(4).to_string())
    print(f"  [OK] → {OUT_IC}")

    print("-" * 96)
    print("[Stage B] P14-2 ① p_up 校准生效验证（v10_cal vs v8）")
    v8 = load_cache(V8_PATH)
    v10_cal = load_cache(V10_CAL_PATH)
    pup = verify_pup_calibration(v8, v10_cal)
    pup.to_csv(OUT_PUP_VERIFY, index=False)
    print(pup.round(4).to_string(index=False))
    print(f"  [OK] → {OUT_PUP_VERIFY}")
    exp_match_all = float(pup["exp_ret_match_rate"].mean())
    p_up_diff_all = float(pup["p_up_diff_rate"].mean())
    print(f"  总体验证: exp_ret 逐字节一致率={exp_match_all*100:.2f}% "
          f"（证明同模型/同数据/确定性） | p_up 不一致率={p_up_diff_all*100:.2f}% "
          f"（>0 即校准器真实生效）")

    # ---- 最终裁决 ----
    verdict = make_verdict(tbl)
    with open(OUT_VERDICT, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    print("=" * 96)
    print(f"[DONE] 总耗时 {time.time()-t_start:.0f}s | 对比表 → {OUT_COMPARE} | "
          f"IC → {OUT_IC} | p_up 验证 → {OUT_PUP_VERIFY} | 裁决 → {OUT_VERDICT}")
    print("=" * 96)


if __name__ == "__main__":
    main()
