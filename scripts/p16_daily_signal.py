"""P16 生产信号流水线：Sentinel-2 每日信号 → 交易计划（研究 → 可执行脚手架）。

背景
----
P0-P15 定案（QA VERIFIED）的 Sentinel-2 生产基线：
  - 引擎 A：v8 信号缓存 ``artifacts/signals_cache18_grouped_v8.parquet``
    （label_pool=all+cross_z，8,624 行/18 品种）+ 按日截面 rank top30%
    （top_k=0.30）+ S2 min_symbols=3 + group_cap=0.5（ferrous_all 合并黑色系）。
  - 引擎 B：basis_ratio 品种内滚动 252 日分位 >= 0.7 做多（win=252/thr=0.70）。
  - 组合：A30/B70（w_engine_a=0.30 / w_engine_b=0.70，P19 终裁）、vol_target=False
    → 组合 OOS 1.614（volN）。

P16 目标：把"研究框架"变成"可执行生产流水线"脚手架：
  1. 显式加载 ``configs/base.yaml``（§9.18：无 path 的 load_config() 不加载 base.yaml）
  2. 数据完整性检查（18 品种 K 线最新日期 + 基差最新日期，缺失/不一致 WARN）
  3. 引擎 A targets（复用 ``engine_a_targets_cs``，显式传参 group_cap/group_map/score_col）
  4. 引擎 B targets（复用 ``engine_b_targets``，win/thr 取自配置）
  5. 组合 A30/B70 名义加权：
       NOTIONAL_engine = initial_capital × notional_frac × weight
       → 手数 floor(notional / (px × multiplier)) → 合并同日多合约（两引擎同选相加）
  6. 交易计划落盘 ``artifacts/trade_plans/{date}_sentinel2_plan.json`` + ``.csv``
  7. 风控检查：黑色系（ferrous_all 组）敞口占比 <= 50%（sanity check）+
     总名义 vs 权益上限

已知限制（如实标注）：
  - v8 信号缓存高度稀疏且 fold 结构性截断（P6-3b 登记）：OOS（2024-07-18 起）
    502 个交易日中引擎 A 仅有 160 日有选中，且**非连续到 2026-06-29**——至 06-29 的
    470 日中 294 日 cache 无行（稀疏 gap）。引擎 A "无信号"按实际原因三态标注：
      cache_end  —— 目标日 > cache 末行信号日（2026-06-29 后 fold 截断）
      cache_gap  —— 目标日在覆盖期内但当日 cache 无行（如 2026-02-06/02-13）
      s2_sparse  —— 当日有行但品种数 < min_symbols（S2 稀疏过滤）
  - A10 权重下引擎 A 名义 = 1e6×0.20×0.10 = 20,000 CNY/标的，多数合约
    floor 后 0 手 → 引擎 A 的生产可交易贡献可能为 0（如实报告，不掩盖）。

口径铁律：复利口径；OOS 2024-07-18 后（仅上下文标注）；嵌套零泄漏（复用
既有信号缓存，不重新训练）；完整回测口径（滑点1tick+费0.005%+保证金12%）
由上游 P3/P5 保证；生产 cap 口径（显式 base.yaml + 显式传参）。
纯本地、无外部 API；模拟盘对接明确留 P17。

用法
----
  python scripts/p16_daily_signal.py                         # 默认 = 最新数据日
  python scripts/p16_daily_signal.py --date 2026-08-17       # 指定交易日
  python scripts/p16_daily_signal.py --date 2026-06-11       # 引擎 A 有信号日对比
  python scripts/p16_daily_signal.py --out-dir artifacts/trade_plans
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.config import load_config
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import load_basis_panel, load_prices
from scripts.p3_combo_backtest import engine_b_targets
from scripts.p5_engineA_cross_section import engine_a_selection, engine_a_targets_cs

OOS_START = "2024-07-18"  # 口径铁律：OOS 起始（仅上下文标注，不参与信号生成）
STALE_DAYS = 5            # [D1 修复后不再参与判定] 原容差口径（latest < target_date 即 WARN，见 check_data_integrity）


# ---------------------------------------------------------------------------
# 1. 数据完整性检查
# ---------------------------------------------------------------------------
def _latest_per_symbol(df: pd.DataFrame, level: int = 1) -> dict[str, pd.Timestamp]:
    """MultiIndex(symbol, datetime) → 每品种最新时间戳。"""
    return {
        sym: pd.Timestamp(g.index.get_level_values(level).max())
        for sym, g in df.groupby(level=0)
    }


def check_data_integrity(
    prices: pd.DataFrame,
    basis: pd.DataFrame,
    target_date: pd.Timestamp,
) -> dict:
    """扫描 18 品种 K 线 + 基差最新日期，报告缺失/不一致。

    返回 dict：
      prices_latest / basis_latest : {symbol: 最新日期}
      warnings : [str, ...]
    """
    px_latest = _latest_per_symbol(prices)
    bs_latest = _latest_per_symbol(basis)
    warnings: list[str] = []

    missing_px = [s for s in SYMBOLS18 if s not in px_latest]
    missing_bs = [s for s in SYMBOLS18 if s not in bs_latest]
    if missing_px:
        warnings.append(f"[K线缺失] 无任何 K 线数据品种: {missing_px}")
    if missing_bs:
        warnings.append(f"[基差缺失] 无任何基差数据品种: {missing_bs}")

    for sym in SYMBOLS18:
        # K 线新鲜度（D1 修复：严格口径——latest < target_date 即 WARN，
        # 原 STALE_DAYS=5 容差会掩盖滞后；例如基差止 08-17 而目标日 08-20 时不再静默）
        if sym in px_latest and px_latest[sym] < target_date:
            gap = (target_date - px_latest[sym]).days
            warnings.append(
                f"[K线过期] {sym} 最新 {px_latest[sym].date()}，早于目标日 "
                f"{target_date.date()} {gap} 天（引擎 A/B 无法对齐该品种价格）"
            )
        # 基差新鲜度（D1 修复：严格口径）
        if sym in bs_latest and bs_latest[sym] < target_date:
            gap = (target_date - bs_latest[sym]).days
            warnings.append(
                f"[基差过期] {sym} 最新 {bs_latest[sym].date()}，早于目标日 "
                f"{target_date.date()} {gap} 天（引擎 B 无法对该品种出信号）"
            )
        # 基差明显早于 K 线（spot 数据断层）
        if sym in px_latest and sym in bs_latest:
            if bs_latest[sym] < px_latest[sym] - pd.Timedelta(days=30):
                warnings.append(
                    f"[基差断层] {sym} 基差 {bs_latest[sym].date()} 早于 K 线 "
                    f"{px_latest[sym].date()} 30 天以上"
                )

    return {
        "target_date": target_date.strftime("%Y-%m-%d"),
        "prices_latest": {k: v.strftime("%Y-%m-%d") for k, v in px_latest.items()},
        "basis_latest": {k: v.strftime("%Y-%m-%d") for k, v in bs_latest.items()},
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 2. 引擎信号提取
# ---------------------------------------------------------------------------
def day_slice(targets: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
    """从 MultiIndex(symbol, time) targets 中取出目标日切片（时间层按位置取）。"""
    lvl = pd.to_datetime(targets.index.get_level_values(1))
    return targets.loc[lvl == date]


def engine_a_signal(
    prices: pd.DataFrame,
    cfg,
    date: pd.Timestamp,
) -> dict:
    """引擎 A：按日截面 rank top30% + S2 min=3 + group_cap（复用 p5 函数）。"""
    cache_path = cfg.backtest.engine_a.signal_cache
    group_cap = cfg.backtest.engine_a.group_cap
    group_map = cfg.backtest.engine_a.group_map
    score_col = "exp_ret"
    top_k = cfg.backtest.engine_a.top_k
    min_symbols = cfg.backtest.engine_a.min_symbols
    # §9.18：显式传参（top_k / min_symbols / group_cap / group_map / score_col）
    targets = engine_a_targets_cs(
        prices,
        top_k=top_k,
        min_symbols=min_symbols,
        cache_path=cache_path,
        group_cap=group_cap,
        group_map=group_map,
        score_col=score_col,
    )
    # cache 末行信号日（三态标注依据：区分 fold 截断 / cache 稀疏 gap）
    cache_max = pd.Timestamp(targets.index.get_level_values(1).max())
    day = day_slice(targets, date)
    # 独立口径手数（floor，NOTIONAL_FRAC=0.20 单引擎）：P15-2 整数手参考
    tgt_map: dict[str, int] = {}
    if len(day):
        for (sym, _ts), row in day.iterrows():
            tgt_map[sym] = int(row["target"])
    # 明细（rank_pct / group / px）——与 targets 同源，仅列更全
    # 注意：engine_a_selection 返回 symbol/ts 为列（非 MultiIndex），按 ts 列过滤
    sel_detail = engine_a_selection(
        prices,
        top_k=top_k,
        min_symbols=min_symbols,
        cache_path=cache_path,
        group_cap=group_cap,
        group_map=group_map,
        score_col=score_col,
    )
    detail = sel_detail[pd.to_datetime(sel_detail["ts"]) == date]
    detail_rows = []
    selected: list[str] = []
    if len(detail):
        for _, row in detail.iterrows():
            sym = row["symbol"]
            is_sel = bool(row["selected"])
            detail_rows.append({
                "symbol": sym,
                "rank_pct": round(float(row["rank_pct"]), 4),
                "day_cnt": int(row["_day_cnt"]),
                "price": None if pd.isna(row["_px"]) else float(row["_px"]),
                "group": row["group"],
                "selected": is_sel,
                "standalone_target": tgt_map.get(sym, 0),
            })
            if is_sel:
                selected.append(sym)
    selected = sorted(set(selected))
    # 三态标注（D3 修复，QA 复核建议——区分"无信号"的真实原因）：
    #   cache_end  —— 目标日 > cache 末行信号日（fold 结构性截断，2026-06-29 后）
    #   cache_gap  —— 目标日 ≤ 末行但当日 cache 无行（v8 cache 稀疏 gap，如 2026-02-06/02-13）
    #   s2_sparse  —— 当日有 cache 行但品种数 < min_symbols（S2 稀疏过滤）
    #   active     —— 正常出信号
    if len(detail) == 0:
        a_status = "cache_end" if date > cache_max else "cache_gap"
    else:
        a_status = "s2_sparse" if not selected else "active"
    return {
        "selected": selected,
        "selected_detail": detail_rows,
        "targets": targets,
        "a_status": a_status,
        "cache_max": cache_max.strftime("%Y-%m-%d"),
        # 兼容字段：cache 当日无信号行（cache_end / cache_gap 均为 True）
        "fold_truncated": a_status in ("cache_end", "cache_gap"),
    }


def engine_b_signal(prices: pd.DataFrame, basis: pd.DataFrame, cfg, date: pd.Timestamp) -> dict:
    """引擎 B：basis_ratio 品种内滚动 252 日分位 >= thr 做多（复用 p3 函数）。

    selected = 当日 br_rank >= thr 的全部品种（信号层，不含 floor 截断）；
    standalone_target 为独立口径手数（floor，NOTIONAL_FRAC=0.20）。
    """
    win = cfg.backtest.engine_b.win
    thr = cfg.backtest.engine_b.thr
    targets = engine_b_targets(prices, win=win, thr=thr)
    day = day_slice(targets, date)
    tgt_map: dict[str, int] = {}
    if len(day):
        for (sym, _ts), row in day.iterrows():
            tgt_map[sym] = int(row["target"])
    # br_rank 明细（与 engine_b_targets 同口径，供报告展示）
    basis = basis.copy()
    basis["br_rank"] = basis.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    detail_rows = []
    selected: list[str] = []
    for sym in SYMBOLS18:
        try:
            br = basis.xs(sym, level=0)["br_rank"].get(date)
        except KeyError:
            br = None
        if br is None or pd.isna(br):
            continue  # 目标日无基差 → 无法计算分位（如 sc0）
        px = prices.xs(sym, level=0)["close"].get(date)
        if px is None or pd.isna(px):
            continue
        is_sel = bool(br >= thr)
        detail_rows.append({
            "symbol": sym,
            "br_rank": round(float(br), 4),
            "price": float(px),
            "selected": is_sel,
            "standalone_target": tgt_map.get(sym, 0),
        })
        if is_sel:
            selected.append(sym)
    selected = sorted(selected)
    return {
        "selected": selected,
        "selected_detail": detail_rows,
        "targets": targets,
    }


# ---------------------------------------------------------------------------
# 3. 组合 A/B 名义加权 → 手数 → 合并
# ---------------------------------------------------------------------------
def combo_plan(
    cfg,
    prices: pd.DataFrame,
    engine_a: dict,
    engine_b: dict,
    date: pd.Timestamp,
) -> dict:
    """A/B 名义加权（权重取自配置，P19 终裁 A30/B70）→ 手数 floor → 合并同日多合约。

    名义口径（任务定稿）：
      NOTIONAL_engine = initial_capital × notional_frac × weight
    权重来源（weight_source）：0.10（引擎 A）/ 0.90（引擎 B）。
    """
    capital = float(cfg.backtest.initial_capital)
    w_a = float(cfg.backtest.combo.w_engine_a)
    w_b = float(cfg.backtest.combo.w_engine_b)
    nf_a = float(cfg.backtest.engine_a.notional_frac)
    nf_b = float(cfg.backtest.engine_b.notional_frac)
    notional_a = capital * nf_a * w_a
    notional_b = capital * nf_b * w_b
    group_map = dict(cfg.backtest.engine_a.group_map or {})

    positions: dict[str, dict] = {}
    notes: list[str] = []

    # 引擎 A 选中 → 加权手数
    for sym in engine_a["selected"]:
        px = prices.xs(sym, level=0)["close"].get(date)
        if px is None or pd.isna(px):
            notes.append(f"[A] {sym} 目标日无 K 线价格，跳过")
            continue
        mult = CONTRACTS18[sym]["multiplier"]
        lots = int(np.floor(notional_a / (px * mult)))
        positions.setdefault(sym, {
            "symbol": sym, "lots_a": 0, "lots_b": 0,
            "price": float(px), "multiplier": float(mult),
        })
        positions[sym]["lots_a"] = lots
        if lots <= 0:
            notes.append(
                f"[A] {sym} 选中但加权名义 {notional_a:,.0f} CNY floor 后 0 手"
                f"（px={px:.2f}×mult={mult:.0f}，单手持仓成本 {px*mult:,.0f} CNY）"
            )

    # 引擎 B 选中 → 加权手数
    for sym in engine_b["selected"]:
        px = prices.xs(sym, level=0)["close"].get(date)
        if px is None or pd.isna(px):
            notes.append(f"[B] {sym} 目标日无 K 线价格，跳过")
            continue
        mult = CONTRACTS18[sym]["multiplier"]
        lots = int(np.floor(notional_b / (px * mult)))
        positions.setdefault(sym, {
            "symbol": sym, "lots_a": 0, "lots_b": 0,
            "price": float(px), "multiplier": float(mult),
        })
        positions[sym]["lots_b"] = lots
        if lots <= 0:
            notes.append(
                f"[B] {sym} 选中但加权名义 {notional_b:,.0f} CNY floor 后 0 手"
                f"（px={px:.2f}×mult={mult:.0f}，单手持仓成本 {px*mult:,.0f} CNY）"
            )

    # 合并输出
    pos_rows = []
    for sym, p in sorted(positions.items()):
        total = p["lots_a"] + p["lots_b"]
        if total <= 0:
            notes.append(f"[合并] {sym} 两引擎 floor 后合计 0 手，不进入交易计划")
            continue
        src = []
        if p["lots_a"] > 0:
            src.append("A")
        if p["lots_b"] > 0:
            src.append("B")
        weight_src = (
            (p["lots_a"] / total) * w_a + (p["lots_b"] / total) * w_b
            if total
            else 0.0
        )
        pos_rows.append({
            "symbol": sym,
            "engine_source": "+".join(src) if src else "NONE",
            "lots": total,
            "lots_a": p["lots_a"],
            "lots_b": p["lots_b"],
            "price": p["price"],
            "multiplier": p["multiplier"],
            "notional": float(total * p["price"] * p["multiplier"]),
            "weight_source": round(weight_src, 4),
            "group": group_map.get(sym, "other"),
        })

    total_notional = float(sum(r["notional"] for r in pos_rows))
    total_lots = int(sum(r["lots"] for r in pos_rows))
    total_ratio = total_notional / capital if capital else 0.0

    # 风控：黑色系（ferrous_all）敞口占比 <= 50%
    ferrous_notional = float(
        sum(r["notional"] for r in pos_rows if r["group"] == "ferrous_all")
    )
    ferrous_ratio = ferrous_notional / total_notional if total_notional else 0.0
    ferrous_pass = ferrous_ratio <= 0.5 + 1e-9
    total_pass = total_ratio <= 1.0 + 1e-9

    degraded = len(engine_a["selected"]) == 0 and len(engine_b["selected"]) > 0
    if degraded:
        a_status = engine_a.get("a_status", "cache_gap")
        if a_status == "cache_end":
            combo_note = (
                "引擎 A 无信号（fold 结构性截断：目标日 > v8 缓存最后信号日 "
                "2026-06-29），组合退化为纯引擎 B"
            )
        elif a_status == "cache_gap":
            combo_note = (
                "引擎 A 无信号（v8 cache 稀疏 gap：目标日在缓存覆盖期内但当日"
                "无信号行），组合退化为纯引擎 B"
            )
        elif a_status == "s2_sparse":
            combo_note = (
                "引擎 A 无信号（S2 稀疏过滤：当日信号品种数 < "
                f"min_symbols={cfg.backtest.engine_a.min_symbols}），组合退化为纯引擎 B"
            )
        else:
            combo_note = "引擎 A 无信号，组合退化为纯引擎 B"
    else:
        combo_note = ""

    return {
        "degraded_to_pure_b": degraded,
        "note": combo_note,
        "positions": pos_rows,
        "total_lots": total_lots,
        "total_notional": total_notional,
        "total_notional_ratio": round(total_ratio, 4),
        "engine_notes": notes,
        "risk": {
            "ferrous_all_notional": ferrous_notional,
            "ferrous_all_ratio": round(ferrous_ratio, 4),
            "ferrous_all_pass": bool(ferrous_pass),
            "total_notional_ratio": round(total_ratio, 4),
            "total_notional_pass": bool(total_pass),
        },
    }


# ---------------------------------------------------------------------------
# 4. 落盘
# ---------------------------------------------------------------------------
def empty_plan_note(integrity: dict, engine_a: dict, engine_b: dict, combo: dict) -> str:
    """空计划（无 positions）时标注原因：数据不完整 vs 真实空仓（D4-附语义标注）。

    区分三类：
      - 数据不完整：引擎 B 基差最新日早于目标日（sc0 历史无基差除外）→ 无法出信号；
      - 真实无信号：基差完整但两引擎均无选中；
      - 结构性 0 手：有选中但加权名义 floor 后 0 手（非数据问题）。
    """
    if combo["positions"]:
        return ""
    bs_latest = integrity.get("basis_latest", {})
    target = integrity.get("target_date", "")
    missing_bs = [s for s, d in bs_latest.items()
                  if d < target and s != "sc0"]  # sc0 历史无基差，不算数据缺失
    a_status = engine_a.get("a_status", "cache_gap")
    b_sel = len(engine_b.get("selected", []))
    if missing_bs and b_sel == 0:
        return (
            f"数据不完整：引擎B基差止于 {max(bs_latest.values())}（缺失 "
            f"{len(missing_bs)} 品种），早于目标日 {target} → 引擎B无法出信号，"
            f"非真实空仓判断"
        )
    if b_sel == 0:
        return (
            f"真实无信号：基差数据完整至 {target}，引擎A({a_status})与引擎B"
            f"(br_rank<thr) 均无选中 → 空仓"
        )
    return (
        f"真实无信号（结构性0手）：引擎选中 {b_sel} 品种但加权名义 floor 后 "
        f"0 手 → 空仓（非数据问题）"
    )


def _to_plan_json(cfg, date: pd.Timestamp, integrity: dict, engine_a: dict,
                  engine_b: dict, combo: dict, prices, basis) -> dict:
    capital = float(cfg.backtest.initial_capital)
    w_a = float(cfg.backtest.combo.w_engine_a)
    w_b = float(cfg.backtest.combo.w_engine_b)
    nf_a = float(cfg.backtest.engine_a.notional_frac)
    nf_b = float(cfg.backtest.engine_b.notional_frac)

    # 引擎 A 明细行（仅选中，含独立口径手数）转 JSON 友好
    a_detail = [
        {k: r[k] for k in ("symbol", "rank_pct", "day_cnt", "price", "group",
                           "selected", "standalone_target")}
        for r in engine_a["selected_detail"]
        if r["selected"]
    ]
    # 引擎 B 明细行（仅选中，含独立口径手数）
    b_detail = [
        {k: r[k] for k in ("symbol", "br_rank", "price", "selected",
                           "standalone_target")}
        for r in engine_b["selected_detail"]
        if r["selected"]
    ]

    plan = {
        "schema_version": "1.0",
        "date": date.strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "oos_context": f"OOS 起始 {OOS_START}（口径铁律，仅上下文标注）",
        "plan_note": empty_plan_note(integrity, engine_a, engine_b, combo),
        "config": {
            "initial_capital": capital,
            "notional_frac_engine_a": nf_a,
            "notional_frac_engine_b": nf_b,
            "w_engine_a": w_a,
            "w_engine_b": w_b,
            "notional_engine_a_per_symbol": capital * nf_a * w_a,
            "notional_engine_b_per_symbol": capital * nf_b * w_b,
            "engine_a": {
                "signal_cache": cfg.backtest.engine_a.signal_cache,
                "top_k": cfg.backtest.engine_a.top_k,
                "min_symbols": cfg.backtest.engine_a.min_symbols,
                "group_cap": cfg.backtest.engine_a.group_cap,
                "score_col": "exp_ret",
            },
            "engine_b": {
                "win": cfg.backtest.engine_b.win,
                "thr": cfg.backtest.engine_b.thr,
            },
            "combo": {
                "w_engine_a": w_a,
                "w_engine_b": w_b,
                "vol_target": bool(cfg.backtest.combo.vol_target),
            },
        },
        "data_integrity": integrity,
        "engines": {
            "engine_a": {
                "has_signal_date": len(engine_a["selected"]) > 0,
                "a_status": engine_a["a_status"],
                "cache_max": engine_a.get("cache_max", "2026-06-29"),
                "fold_truncated": engine_a["fold_truncated"],
                "note": (
                    "fold 结构性截断：目标日 > v8 缓存最后信号日 2026-06-29"
                    if engine_a["a_status"] == "cache_end"
                    else "v8 cache 稀疏 gap：目标日在缓存覆盖期内但当日无信号行"
                    if engine_a["a_status"] == "cache_gap"
                    else "S2 稀疏过滤：当日信号品种数 < min_symbols"
                    if engine_a["a_status"] == "s2_sparse"
                    else ""
                ),
                "selected": engine_a["selected"],
                "selected_detail": a_detail,
            },
            "engine_b": {
                "has_signal_date": len(engine_b["selected"]) > 0,
                "selected": engine_b["selected"],
                "selected_detail": b_detail,
            },
        },
        "combo": {
            "degraded_to_pure_b": combo["degraded_to_pure_b"],
            "note": combo["note"],
            "engine_notes": combo["engine_notes"],
            "positions": combo["positions"],
            "total_lots": combo["total_lots"],
            "total_notional": combo["total_notional"],
            "total_notional_ratio": combo["total_notional_ratio"],
            "risk": combo["risk"],
        },
    }
    return plan


def write_plan(cfg, date: pd.Timestamp, integrity: dict, engine_a: dict,
               engine_b: dict, combo: dict, prices, basis, out_dir: Path) -> tuple[Path, Path]:
    """交易计划落盘 JSON + CSV，返回 (json_path, csv_path)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = date.strftime("%Y-%m-%d")

    plan = _to_plan_json(cfg, date, integrity, engine_a, engine_b, combo, prices, basis)
    json_path = out_dir / f"{date_str}_sentinel2_plan.json"
    json_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = []
    for p in combo["positions"]:
        rows.append({
            "date": date_str,
            "symbol": p["symbol"],
            "engine_source": p["engine_source"],
            "lots": p["lots"],
            "lots_a": p["lots_a"],
            "lots_b": p["lots_b"],
            "price": p["price"],
            "multiplier": p["multiplier"],
            "notional": p["notional"],
            "weight_source": p["weight_source"],
            "group": p["group"],
        })
    csv_path = out_dir / f"{date_str}_sentinel2_plan.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    return json_path, csv_path


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def print_plan_summary(cfg, date: pd.Timestamp, integrity: dict, engine_a: dict,
                       engine_b: dict, combo: dict) -> None:
    print(f"\n{'=' * 88}")
    print(f"Sentinel-2 生产信号流水线 — 交易计划摘要  {date.strftime('%Y-%m-%d')}")
    print(f"{'=' * 88}")

    print("\n[数据完整性]")
    n_px_stale = sum(1 for w in integrity["warnings"] if "[K线过期]" in w)
    n_bs_stale = sum(1 for w in integrity["warnings"] if "[基差过期]" in w)
    n_bs_gap = sum(1 for w in integrity["warnings"] if "[基差断层]" in w)
    print(f"  18 品种 K 线最新日: "
          f"{min(integrity['prices_latest'].values())} ~ {max(integrity['prices_latest'].values())}")
    print(f"  18 品种基差最新日: "
          f"{min(integrity['basis_latest'].values())} ~ {max(integrity['basis_latest'].values())}")
    print(f"  WARN: K线过期 {n_px_stale} | 基差过期 {n_bs_stale} | 基差断层 {n_bs_gap}")
    for w in integrity["warnings"]:
        print(f"    ! {w}")

    print("\n[引擎 A] 按日截面 rank top30% + S2 min=3 + group_cap=0.5")
    a_status = engine_a.get("a_status", "cache_gap")
    if a_status == "cache_end":
        print("  ⚠️ 无信号（fold 结构性截断：v8 缓存最后信号日 2026-06-29，目标日在其后）")
        print("  ⚠️ 组合退化为纯引擎 B —— 已知限制，P6-3b 登记，如实标注")
    elif a_status == "cache_gap":
        print("  ⚠️ 无信号（v8 cache 稀疏 gap：目标日在缓存覆盖期内但当日 cache 无行）")
        print("  ⚠️ 组合退化为纯引擎 B —— v8 cache 高度稀疏（OOS 502 日中仅 160 日有选中）")
    elif a_status == "s2_sparse":
        print("  ⚠️ 无信号（S2 稀疏过滤：当日信号品种数 < min_symbols=3）")
        print("  ⚠️ 组合退化为纯引擎 B")
    else:
        for r in engine_a["selected_detail"]:
            if r["selected"]:
                print(f"  选中 {r['symbol']:<5} rank_pct={r['rank_pct']:.3f} "
                      f"group={r['group']} px={r['price']} "
                      f"(独立口径 {r['standalone_target']} 手)")

    print("\n[引擎 B] basis_ratio 滚动 252 日分位 >= 0.70")
    if not engine_b["selected"]:
        print("  无信号")
    else:
        for r in engine_b["selected_detail"]:
            if r["selected"]:
                print(f"  选中 {r['symbol']:<5} br_rank={r['br_rank']:.4f} "
                      f"px={r['price']:.2f} (独立口径 {r['standalone_target']} 手)")

    print(f"\n[组合 A{cfg.backtest.combo.w_engine_a*100:.0f}/B{cfg.backtest.combo.w_engine_b*100:.0f}] "
          f"名义加权 → floor 手数 → 合并")
    if combo["degraded_to_pure_b"]:
        print("  ⚠️ 退化为纯引擎 B（引擎 A 无信号）")
    if not combo["positions"]:
        print(f"  ⚠️ 空计划：{empty_plan_note(integrity, engine_a, engine_b, combo)}")
    for r in combo["positions"]:
        print(f"  {r['symbol']:<5} 来源={r['engine_source']:<4} 手数={r['lots']:>3} "
              f"名义={r['notional']:>12,.0f} CNY 组={r['group']}")
    print(f"  合计: {combo['total_lots']} 手 | 名义 {combo['total_notional']:,.0f} CNY "
          f"({combo['total_notional_ratio']*100:.1f}% 权益)")
    for n in combo["engine_notes"]:
        print(f"    ! {n}")

    print("\n[风控]")
    rk = combo["risk"]
    print(f"  黑色系(ferrous_all) 名义 {rk['ferrous_all_notional']:,.0f} CNY / "
          f"总名义 {combo['total_notional']:,.0f} CNY = {rk['ferrous_all_ratio']*100:.1f}% "
          f"({'PASS ≤50%' if rk['ferrous_all_pass'] else 'FAIL >50%'})")
    print(f"  总名义/权益 = {rk['total_notional_ratio']*100:.1f}% "
          f"({'PASS ≤100%' if rk['total_notional_pass'] else 'FAIL >100%'})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sentinel-2 每日生产信号流水线")
    ap.add_argument("--date", type=str, default=None,
                    help="目标交易日 YYYY-MM-DD（默认=最新数据日）")
    ap.add_argument("--out-dir", type=str, default="artifacts/trade_plans",
                    help="交易计划输出目录（默认 artifacts/trade_plans）")
    args = ap.parse_args()

    print("=" * 88)
    print("P16 生产信号流水线：Sentinel-2 每日信号 → 交易计划")
    print("=" * 88)

    # ---- [1/7] 配置加载（§9.18：显式 base.yaml）----
    print("\n[1/7] 配置加载 configs/base.yaml ...")
    cfg = load_config("configs/base.yaml")
    print(f"  引擎 A: cache={cfg.backtest.engine_a.signal_cache} "
          f"top_k={cfg.backtest.engine_a.top_k} min_symbols={cfg.backtest.engine_a.min_symbols} "
          f"group_cap={cfg.backtest.engine_a.group_cap}")
    print(f"  引擎 B: win={cfg.backtest.engine_b.win} thr={cfg.backtest.engine_b.thr} "
          f"notional_frac={cfg.backtest.engine_b.notional_frac}")
    print(f"  组合: A{cfg.backtest.combo.w_engine_a:.2f}/B{cfg.backtest.combo.w_engine_b:.2f} "
          f"vol_target={cfg.backtest.combo.vol_target} "
          f"initial_capital={cfg.backtest.initial_capital:,.0f}")

    # ---- [2/7] 数据加载 + 完整性检查 ----
    print("\n[2/7] 数据加载 + 完整性检查 ...")
    prices = load_prices()
    basis = load_basis_panel()
    latest = pd.Timestamp(prices.index.get_level_values(1).max())
    if args.date:
        target_date = pd.Timestamp(args.date)
        if target_date > latest:
            print(f"  ⚠️ 目标日 {target_date.date()} 晚于 K 线最新日 {latest.date()}，"
                  f"将无法对齐价格")
    else:
        target_date = latest
        print(f"  --date 未指定，默认最新数据日 = {target_date.date()}")
    integrity = check_data_integrity(prices, basis, target_date)
    print(f"  K线最新: {max(integrity['prices_latest'].values())} | "
          f"基差最新: {max(integrity['basis_latest'].values())} | "
          f"WARN×{len(integrity['warnings'])}")
    for w in integrity["warnings"]:
        print(f"    ! {w}")

    # ---- [3/7] 引擎 A targets ----
    print("\n[3/7] 引擎 A targets（截面 rank + S2 min=3 + cap，复用 p5）...")
    engine_a = engine_a_signal(prices, cfg, target_date)
    a_status = engine_a["a_status"]
    if a_status == "cache_end":
        print("  ⚠️ 无信号（fold 截断：目标日 > v8 缓存最后信号日 2026-06-29）"
              "→ 组合退化为纯引擎 B（如实标注）")
    elif a_status == "cache_gap":
        print("  ⚠️ 无信号（v8 cache 稀疏 gap：目标日在覆盖期内但当日无信号行）"
              "→ 组合退化为纯引擎 B")
    elif a_status == "s2_sparse":
        print(f"  ⚠️ 无信号（S2 稀疏过滤：当日信号品种数 < min_symbols="
              f"{cfg.backtest.engine_a.min_symbols}）→ 组合退化为纯引擎 B")
    else:
        print(f"  选中 {len(engine_a['selected'])} 品种: {engine_a['selected']}")

    # ---- [4/7] 引擎 B targets ----
    print("\n[4/7] 引擎 B targets（win/thr 取自配置，复用 p3）...")
    engine_b = engine_b_signal(prices, basis, cfg, target_date)
    print(f"  选中 {len(engine_b['selected'])} 品种: {engine_b['selected']}")

    # ---- [5/7] 组合 A/B 名义加权 ----
    print(f"\n[5/7] 组合 A{cfg.backtest.combo.w_engine_a*100:.0f}/B{cfg.backtest.combo.w_engine_b*100:.0f} "
          f"名义加权 → 手数 floor → 合并 ...")
    combo = combo_plan(cfg, prices, engine_a, engine_b, target_date)
    print(f"  交易计划 {len(combo['positions'])} 条 | 合计 {combo['total_lots']} 手 | "
          f"名义 {combo['total_notional']:,.0f} CNY")
    for n in combo["engine_notes"]:
        print(f"    ! {n}")

    # ---- [6/7] 交易计划落盘 ----
    print("\n[6/7] 交易计划落盘 ...")
    json_path, csv_path = write_plan(
        cfg, target_date, integrity, engine_a, engine_b, combo, prices, basis,
        Path(args.out_dir),
    )
    print(f"  [OK] JSON → {json_path}")
    print(f"  [OK] CSV  → {csv_path}")

    # ---- [7/7] 风控检查 ----
    print("\n[7/7] 风控检查 ...")
    rk = combo["risk"]
    print(f"  黑色系敞口占比 {rk['ferrous_all_ratio']*100:.1f}% "
          f"({'PASS' if rk['ferrous_all_pass'] else 'FAIL'})")
    print(f"  总名义/权益 {rk['total_notional_ratio']*100:.1f}% "
          f"({'PASS' if rk['total_notional_pass'] else 'FAIL'})")

    print_plan_summary(cfg, target_date, integrity, engine_a, engine_b, combo)
    print("\n[DONE] P16 流水线完成。模拟盘对接 → P17（本脚本不含任何下单/外部调用）")


if __name__ == "__main__":
    main()
