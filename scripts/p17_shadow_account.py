"""P17 内部簿记模拟盘（shadow account）：Sentinel-2 生产计划 → 本地逐日簿记回放。

背景
----
外部模拟盘调查（P16 结论）：westock portfolio_paper_trade 与 mx-moni 模拟交易均仅
支持 A 股（100 整数倍、无期货合约）→ Sentinel-2 期货策略无法对接外部模拟盘 →
P17 采用**内部簿记模拟盘**（本地 shadow account）：

  1. 复用 P16 生产计划（``artifacts/trade_plans/{date}_sentinel2_plan.json``，
     schema v1.0：combo.positions 含 symbol/lots/lots_a/lots_b/notional/
     engine_source/group；combo.risk 含 ferrous_all_ratio/ferrous_all_pass）
  2. 以 T+1 开盘价成交、逐日收盘 mark-to-market 簿记，输出净值曲线 / 持仓记录 /
     账户状态 / 一致性验证。

成交约定（文档化）
----------------
1. **T+1 开盘成交**：T 日交易计划 → T+1 日开盘价成交（price = 次日 open）。
2. **无次日 → pending**：计划日 = 最新数据日（无次日开盘）时，该计划不入账并标注
   pending（默认不执行）；可通过 ``--fill-convention same_bar_close`` 以收盘价模拟
   （仅供对照，非生产默认）。
3. **手续费** 0.005% × 名义（与回测 ``CostModel.fee_open/fee_close`` 对齐）。
4. **滑点** 默认 1 tick（与回测对齐，``--slippage 0`` 可关闭）；
   成交价 = open ± tick（买入 +tick、卖出 -tick，方向不利）。
5. **复利口径**：权益 = 现金 + 持仓市值（mark-to-market）；实现/未实现盈亏分开记账，
   与 ``SimBroker`` 同一套逐笔均价记账（open 更新加权均价 / close 结算已实现盈亏）。
6. **每日再平衡**：计划即目标持仓；T+1 开盘将持仓调整到最新已执行计划的 targets，
   计划中未出现的品种目标为 0（清仓）。

黑色系 FAIL 处理（P16 必接项）
----------------------------
``combo.risk.ferrous_all_pass=False``（黑色系敞口 >50%，引擎 B 无 cap）时：
  - ``--ferrous-mode block``（默认，更保守）：**当日计划整体阻断，不执行**；
    持仓维持上一已执行计划；计数 ``blocked_plans``。
  - ``--ferrous-mode scale``：黑色系（group=ferrous_all）品种手数减半（floor）
    循环直至敞口 ≤50%（或黑色系清空）。
  - ``--ferrous-mode none``：不做干预（仅一致性对照，非生产）。

引擎 A 0 手处理
--------------
计划 positions 保留 lots_a/lots_b/engine_source 标注；消费端如实记录"纯 B"
（positions 全部 lots_a==0）事实。P16 已证实引擎 A 加权名义 20k CNY/标的在全部
OOS 日 floor 后 0 手（结构性 0 手）。

回放性能
--------
P16 逐日生成计划 ~5.8s/日 × 502 日 ≈ 48min，不可接受。P17 采用**预计算信号 +
内存逐日切片**：引擎 A/B targets 与选中明细只在启动时计算一次（复用 p5/p3/p16
同一函数与口径），随后逐日切片 + 复用 ``p16.combo_plan`` / ``p16._to_plan_json``
（保证与 P16 逐日输出逐字段一致），计划落盘到
``artifacts/shadow_account/plans_cache/`` 供二次运行秒级复用。总运行 < 30s。

用法
----
  python scripts/p17_shadow_account.py                          # 默认全量回放
  python scripts/p17_shadow_account.py --ferrous-mode none      # 对照：不干预
  python scripts/p17_shadow_account.py --slippage 0             # 对照：无滑点
  python scripts/p17_shadow_account.py --reuse-plans            # 复用计划缓存
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from hexbroker.backtest.cost import CostModel
from hexbroker.backtest.engine import BacktestEngine
from hexbroker.config import load_config
from hexbroker.evaluation.metrics import compute_metrics
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18
from scripts.p2_basis_backtest import load_basis_panel
from scripts.p3_combo_backtest import engine_b_targets
from scripts.p5_engineA_cross_section import engine_a_selection, engine_a_targets_cs

import scripts.p16_daily_signal as p16  # 复用 combo_plan / _to_plan_json / day_slice / check_data_integrity

OOS_START = "2024-07-18"  # 口径铁律：OOS 起始
OOS_END = "2026-08-17"    # 数据最新日（与 P16 一致）
INITIAL_CAPITAL = 1_000_000.0
FEE_RATE = 0.00005        # 0.005% × 名义（与回测 CostModel 对齐）
SLIPPAGE_TICKS = 1.0      # 1 tick（与回测对齐；--slippage 0 关闭）
MARGIN_RATE = 0.12        # 保证金率（仅报告占用，不触发强平）

ART_DIR = Path("artifacts/shadow_account")
PLANS_CACHE_DIR = ART_DIR / "plans_cache"


def load_prices_ohlc() -> pd.DataFrame:
    """18 品种 K 线 → MultiIndex(symbol, datetime) + open/close（p17 专用）。

    与 p2.load_prices 同构，但保留 open 列（T+1 开盘成交需要）；close 列与
    p2/p3/p16 口径逐字节一致，预计算函数可直接复用。
    """
    import glob

    parts = []
    for sym in SYMBOLS18:
        for fp in sorted(glob.glob(f"data/raw/processed/{sym}/1d/*.parquet")):
            df = pd.read_parquet(fp)
            df["datetime"] = pd.to_datetime(df["datetime"])
            parts.append(df[["symbol", "datetime", "open", "close"]])
    prices = pd.concat(parts, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    return prices


# ---------------------------------------------------------------------------
# 1. ShadowAccount：内部簿记模拟盘
# ---------------------------------------------------------------------------
@dataclass
class TradeRecord:
    """一笔成交记录（与 SimBroker.Trade 同构，供落盘/统计）。"""

    plan_date: str
    exec_date: str
    symbol: str
    delta_lots: int
    fill_price: float
    fee: float
    is_open: bool
    note: str = ""


class ShadowAccount:
    """期货内部簿记模拟盘（仅做多，逐标的均价记账）。

    记账口径与 ``SimBroker`` 一致：open/add 更新加权均价、close/reduce 结算已实现
    盈亏；但**实现/未实现盈亏使用品种级 multiplier**（回测 SimBroker 使用全局
    multiplier=10，见 :meth:`backtest_aligned_equity` 对照）。
    """

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        fee_rate: float = FEE_RATE,
        slippage_ticks: float = SLIPPAGE_TICKS,
        contracts: dict[str, dict] | None = None,
    ) -> None:
        self.initial_capital = float(initial_capital)
        self.fee_rate = float(fee_rate)
        self.slippage_ticks = float(slippage_ticks)
        self.contracts = dict(contracts or CONTRACTS18)
        self.cash = float(initial_capital)
        self.positions: dict[str, int] = {}
        self.avg_entry: dict[str, float] = {}
        self.realized_pnl = 0.0
        self.trades: list[TradeRecord] = []
        self.last_plan_src: dict[str, dict] = {}  # symbol -> {lots_a, lots_b, engine_source, group}
        self.no_fill_notes: list[str] = []

    # ---- 价格/费用 ----
    def _fill_price(self, ref_price: float, side: int, symbol: str) -> float:
        """side=+1 买入 → 价格不利 +tick；side=-1 卖出 → 价格不利 -tick。"""
        tick = float(self.contracts[symbol]["min_tick"]) * self.slippage_ticks
        return float(ref_price) + side * tick

    def _fee(self, fill_price: float, lots: int, symbol: str) -> float:
        mult = float(self.contracts[symbol]["multiplier"])
        return float(fill_price * mult * abs(lots) * self.fee_rate)

    def _price(self, prices: pd.DataFrame, symbol: str, date: str, col: str) -> float | None:
        try:
            v = prices.xs(symbol, level=0)[col].get(pd.Timestamp(date))
        except KeyError:
            return None
        if v is None or pd.isna(v):
            return None
        return float(v)

    # ---- 执行 ----
    def execute(
        self,
        symbol: str,
        target_lots: int,
        ref_price: float,
        plan_date: str,
        exec_date: str,
    ) -> TradeRecord | None:
        """将 symbol 持仓调整到 target_lots（与 SimBroker.execute 同构）。"""
        cur = int(self.positions.get(symbol, 0))
        delta = int(target_lots) - cur
        if delta == 0:
            return None
        mult = float(self.contracts[symbol]["multiplier"])
        side = 1 if delta > 0 else -1
        fp = self._fill_price(ref_price, side, symbol)
        fee = self._fee(fp, delta, symbol)
        # 现金：买入（delta>0）扣款，卖出（delta<0）入账，均扣手续费
        self.cash -= fp * mult * delta + fee
        if cur == 0 or (delta > 0) == (cur > 0):
            # 开仓/加仓：更新加权均价；手续费计入已实现（负向）
            self.realized_pnl -= fee
            abs_cur, abs_del = abs(cur), abs(delta)
            if abs_cur == 0:
                self.avg_entry[symbol] = fp
            else:
                self.avg_entry[symbol] = (
                    self.avg_entry[symbol] * abs_cur + fp * abs_del
                ) / (abs_cur + abs_del)
            is_open = True
        else:
            # 平仓/减仓：结算已实现盈亏
            closed = min(abs(delta), abs(cur))
            direction = 1 if cur > 0 else -1
            self.realized_pnl += (
                direction * (fp - self.avg_entry.get(symbol, fp)) * mult * closed - fee
            )
            if abs(cur + delta) < 1e-9:
                self.avg_entry[symbol] = 0.0
            is_open = False
        self.positions[symbol] = int(target_lots)
        rec = TradeRecord(
            plan_date=plan_date, exec_date=exec_date, symbol=symbol,
            delta_lots=delta, fill_price=fp, fee=fee, is_open=is_open,
        )
        self.trades.append(rec)
        return rec

    def execute_plan(
        self,
        plan: dict,
        exec_date: str,
        prices: pd.DataFrame,
        ferrous_mode: str = "block",
        stats: dict | None = None,
        price_col: str = "open",
    ) -> tuple[bool, bool]:
        """执行一份 P16 交易计划（在 exec_date 开盘/收盘价）。

        返回 (blocked, scaled)：黑色系 FAIL 是否阻断 / 是否缩量。
        """
        risk = plan["combo"]["risk"]
        positions = plan["combo"]["positions"]
        ferrous_fail = not bool(risk.get("ferrous_all_pass", True))
        blocked, scaled = False, False

        if ferrous_mode == "block" and ferrous_fail:
            blocked = True
            if stats is not None:
                stats["blocked_plans"] += 1
            return blocked, scaled
        if ferrous_mode == "scale" and ferrous_fail:
            positions = self._scale_ferrous(plan)
            scaled = True
            if stats is not None:
                stats["scaled_plans"] += 1

        targets = {p["symbol"]: int(p["lots"]) for p in positions}
        # 记录来源标注（消费端"纯 B"检查）
        for p in positions:
            self.last_plan_src[p["symbol"]] = {
                "lots_a": int(p.get("lots_a", 0)),
                "lots_b": int(p.get("lots_b", 0)),
                "engine_source": p.get("engine_source", "NONE"),
                "group": p.get("group", "other"),
            }
        # 目标 = 计划 positions 中全部品种；当前持仓但不在计划 → 目标 0（清仓）
        all_syms = sorted(set(targets) | set(self.positions))
        for sym in all_syms:
            tgt = targets.get(sym, 0)
            px = self._price(prices, sym, exec_date, price_col)
            if px is None:
                self.no_fill_notes.append(
                    f"{exec_date} {sym} 无{price_col}价，计划 {plan['date']} 该品种未成交"
                )
                if stats is not None:
                    stats["no_fill_symbols"] += 1
                continue
            tr = self.execute(sym, tgt, px, plan_date=plan["date"], exec_date=exec_date)
            if tr is not None and stats is not None:
                stats["fill_count"] += 1
                stats["buy_lots"] += max(tr.delta_lots, 0)
                stats["sell_lots"] += max(-tr.delta_lots, 0)
        if stats is not None:
            stats["executed_plans"] += 1
        return blocked, scaled

    def _scale_ferrous(self, plan: dict) -> list[dict]:
        """黑色系 FAIL → 黑色系（ferrous_all）手数减半（floor）直至敞口 ≤50%。"""
        positions = [dict(p) for p in plan["combo"]["positions"]]

        def ratio() -> float:
            total = sum(p["lots"] * p["price"] * p["multiplier"] for p in positions)
            fer = sum(
                p["lots"] * p["price"] * p["multiplier"]
                for p in positions if p["group"] == "ferrous_all"
            )
            return fer / total if total else 0.0

        guard = 0
        while ratio() > 0.5 + 1e-9 and any(
            p["lots"] > 0 and p["group"] == "ferrous_all" for p in positions
        ):
            for p in positions:
                if p["group"] == "ferrous_all":
                    p["lots"] = int(p["lots"]) // 2
            guard += 1
            if guard > 20:
                break
        return [p for p in positions if p["lots"] > 0]

    # ---- 估值 ----
    def mark(self, date: str, prices: pd.DataFrame) -> dict:
        """按日收盘 mark-to-market，返回快照。"""
        pos_value = 0.0
        unreal = 0.0
        for sym, lots in self.positions.items():
            if lots == 0:
                continue
            close = self._price(prices, sym, date, "close")
            if close is None:
                continue
            mult = float(self.contracts[sym]["multiplier"])
            pos_value += lots * close * mult
            unreal += lots * (close - self.avg_entry.get(sym, close)) * mult
        equity = self.cash + pos_value
        return {
            "date": date,
            "equity": equity,
            "pos_value": pos_value,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": unreal,
            "margin_used": pos_value * MARGIN_RATE,
        }

    def backtest_aligned_equity(self, date: str, prices: pd.DataFrame) -> float:
        """回测对齐权益：PnL 用全局 multiplier=10（SimBroker 口径），手续费仍品种级。

        仅重放 ``exec_date <= date`` 的成交，保证某日权益不包含未来已实现盈亏。

        用于一致性验证差异归因：``SA_samebar(准确口径)`` vs ``BacktestEngine`` 的
        差异 = 全局×10 vs 品种级 multiplier 记账差异。
        """
        realized10 = 0.0
        unreal10 = 0.0
        avg10: dict[str, float] = {}
        pos10: dict[str, float] = {}
        for t in self.trades:
            if t.exec_date > date:
                continue
            sym = t.symbol
            cur = pos10.get(sym, 0.0)
            delta = float(t.delta_lots)
            if cur == 0.0 or (delta > 0) == (cur > 0):
                realized10 -= t.fee
                abs_cur, abs_del = abs(cur), abs(delta)
                if abs_cur < 1e-12:
                    avg10[sym] = t.fill_price
                else:
                    avg10[sym] = (avg10[sym] * abs_cur + t.fill_price * abs_del) / (
                        abs_cur + abs_del
                    )
            else:
                closed = min(abs(delta), abs(cur))
                direction = 1 if cur > 0 else -1
                realized10 += (
                    direction * (t.fill_price - avg10.get(sym, t.fill_price)) * closed * 10.0
                    - t.fee
                )
                if abs(cur + delta) < 1e-9:
                    avg10[sym] = 0.0
            pos10[sym] = cur + delta
        for sym, lots in pos10.items():
            if abs(lots) < 1e-12:
                continue
            close = self._price(prices, sym, date, "close")
            if close is None:
                continue
            unreal10 += lots * (close - avg10.get(sym, close)) * 10.0
        return float(self.initial_capital + realized10 + unreal10)


# ---------------------------------------------------------------------------
# 2. 交易计划生成（预计算信号 + 逐日切片，与 P16 同口径）
# ---------------------------------------------------------------------------
def precompute_signals(cfg, prices: pd.DataFrame, basis: pd.DataFrame) -> dict:
    """一次性计算引擎 A/B 全历史 targets 与选中明细（与 p16 同函数同参数）。"""
    ea = cfg.backtest.engine_a
    eb = cfg.backtest.engine_b
    tgt_a = engine_a_targets_cs(
        prices,
        top_k=ea.top_k,
        min_symbols=ea.min_symbols,
        cache_path=ea.signal_cache,
        group_cap=ea.group_cap,
        group_map=ea.group_map,
        score_col="exp_ret",
    )
    sel_a = engine_a_selection(
        prices,
        top_k=ea.top_k,
        min_symbols=ea.min_symbols,
        cache_path=ea.signal_cache,
        group_cap=ea.group_cap,
        group_map=ea.group_map,
        score_col="exp_ret",
    )
    cache_max = pd.Timestamp(tgt_a.index.get_level_values(1).max())
    tgt_b = engine_b_targets(prices, win=eb.win, thr=eb.thr)
    basis2 = basis.copy()
    basis2["br_rank"] = basis2.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(eb.win, min_periods=60).rank(pct=True)
    )
    return {
        "tgt_a": tgt_a,
        "sel_a": sel_a,
        "cache_max": cache_max,
        "tgt_b": tgt_b,
        "basis_br": basis2,
    }


def engine_a_day(pre: dict, cfg, prices: pd.DataFrame, date: pd.Timestamp) -> dict:
    """逐日引擎 A 信号（与 p16.engine_a_signal 同日逻辑，基于预计算表）。"""
    tgt_map: dict[str, int] = {}
    day = p16.day_slice(pre["tgt_a"], date)
    for (sym, _ts), row in day.iterrows():
        tgt_map[sym] = int(row["target"])
    detail = pre["sel_a"][pd.to_datetime(pre["sel_a"]["ts"]) == date]
    detail_rows: list[dict] = []
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
    cache_max = pre["cache_max"]
    if len(detail) == 0:
        a_status = "cache_end" if date > cache_max else "cache_gap"
    else:
        a_status = "s2_sparse" if not selected else "active"
    return {
        "selected": selected,
        "selected_detail": detail_rows,
        "a_status": a_status,
        "cache_max": cache_max.strftime("%Y-%m-%d"),
        "fold_truncated": a_status in ("cache_end", "cache_gap"),
    }


def engine_b_day(pre: dict, cfg, prices: pd.DataFrame, date: pd.Timestamp) -> dict:
    """逐日引擎 B 信号（与 p16.engine_b_signal 同日逻辑，基于预计算表）。"""
    eb = cfg.backtest.engine_b
    tgt_map: dict[str, int] = {}
    day = p16.day_slice(pre["tgt_b"], date)
    for (sym, _ts), row in day.iterrows():
        tgt_map[sym] = int(row["target"])
    detail_rows: list[dict] = []
    selected: list[str] = []
    for sym in SYMBOLS18:
        try:
            br = pre["basis_br"].xs(sym, level=0)["br_rank"].get(date)
        except KeyError:
            br = None
        if br is None or pd.isna(br):
            continue
        try:
            px = prices.xs(sym, level=0)["close"].get(date)
        except KeyError:
            px = None
        if px is None or pd.isna(px):
            continue
        is_sel = bool(br >= eb.thr)
        detail_rows.append({
            "symbol": sym,
            "br_rank": round(float(br), 4),
            "price": float(px),
            "selected": is_sel,
            "standalone_target": tgt_map.get(sym, 0),
        })
        if is_sel:
            selected.append(sym)
    return {"selected": sorted(selected), "selected_detail": detail_rows}


def build_plan(cfg, prices: pd.DataFrame, basis: pd.DataFrame, pre: dict,
               date: pd.Timestamp) -> dict:
    """构建单日完整计划 JSON（复用 p16.combo_plan + p16._to_plan_json）。"""
    ea = engine_a_day(pre, cfg, prices, date)
    eb = engine_b_day(pre, cfg, prices, date)
    combo = p16.combo_plan(cfg, prices, ea, eb, date)
    integ = p16.check_data_integrity(prices, basis, date)
    return p16._to_plan_json(cfg, date, integ, ea, eb, combo, prices, basis)


def oos_dates(prices: pd.DataFrame) -> list[pd.Timestamp]:
    """OOS 窗口内的全部交易日（18 品种共用同一日历，已核实 502 日）。"""
    dates = pd.to_datetime(prices.index.get_level_values(1)).unique()
    dates = dates[(dates >= pd.Timestamp(OOS_START)) & (dates <= pd.Timestamp(OOS_END))]
    return sorted(dates)


def generate_plans(cfg, prices: pd.DataFrame, basis: pd.DataFrame,
                   dates: list[pd.Timestamp], cache_dir: Path = PLANS_CACHE_DIR) -> dict:
    """逐日生成交易计划并缓存（首次运行；二次运行用 --reuse-plans 复用）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"  [预计算] 引擎 A/B 全历史 signals（一次性）...")
    pre = precompute_signals(cfg, prices, basis)
    print(f"  [生成] 逐日构建 {len(dates)} 份交易计划...")
    plans: dict[str, dict] = {}
    for i, date in enumerate(dates):
        plan = build_plan(cfg, prices, basis, pre, date)
        ds = date.strftime("%Y-%m-%d")
        plans[ds] = plan
        (cache_dir / f"{ds}_sentinel2_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(dates)}")
    return plans


def load_plans(cache_dir: Path, dates: list[pd.Timestamp]) -> tuple[dict, list[pd.Timestamp]]:
    """从缓存加载计划；返回 (plans, missing_dates)。"""
    plans: dict[str, dict] = {}
    missing: list[pd.Timestamp] = []
    for date in dates:
        ds = date.strftime("%Y-%m-%d")
        fp = cache_dir / f"{ds}_sentinel2_plan.json"
        if fp.exists():
            plans[ds] = json.loads(fp.read_text(encoding="utf-8"))
        else:
            missing.append(date)
    return plans, missing


# ---------------------------------------------------------------------------
# 3. 历史回放
# ---------------------------------------------------------------------------
def run_replay(
    plans: dict,
    prices: pd.DataFrame,
    dates: list[pd.Timestamp],
    ferrous_mode: str = "block",
    fill_convention: str = "T+1_open",
    slippage_ticks: float = SLIPPAGE_TICKS,
    fee_rate: float = FEE_RATE,
) -> tuple[ShadowAccount, pd.DataFrame, pd.DataFrame, dict]:
    """逐日回放：T+1 开盘执行计划（或同 bar 收盘对照），收盘 mark-to-market。"""
    account = ShadowAccount(
        initial_capital=INITIAL_CAPITAL, fee_rate=fee_rate,
        slippage_ticks=slippage_ticks, contracts=CONTRACTS18,
    )
    stats: dict[str, Any] = {
        "executed_plans": 0,
        "blocked_plans": 0,
        "scaled_plans": 0,
        "pending_plans": 0,
        "fill_count": 0,
        "buy_lots": 0,
        "sell_lots": 0,
        "no_fill_symbols": 0,
        "pure_b_days": 0,
        "days_with_positions": 0,
    }
    equity_rows: list[dict] = []
    pos_rows: list[dict] = []
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    for i, date in enumerate(dates):
        ds = date_strs[i]
        if fill_convention == "T+1_open":
            if i > 0:
                account.execute_plan(
                    plans[date_strs[i - 1]], exec_date=ds, prices=prices,
                    ferrous_mode=ferrous_mode, stats=stats, price_col="open",
                )
        elif fill_convention == "same_bar_close":
            account.execute_plan(
                plans[ds], exec_date=ds, prices=prices,
                ferrous_mode=ferrous_mode, stats=stats, price_col="close",
            )
        else:
            raise ValueError(f"未知 fill_convention={fill_convention!r}")

        snap = account.mark(ds, prices)
        equity_rows.append(snap)

        # 持仓快照
        has_pos = False
        for sym, lots in sorted(account.positions.items()):
            if lots == 0:
                continue
            has_pos = True
            close = account._price(prices, sym, ds, "close")
            mult = float(CONTRACTS18[sym]["multiplier"])
            src = account.last_plan_src.get(sym, {})
            pos_rows.append({
                "date": ds,
                "symbol": sym,
                "lots": lots,
                "avg_entry": round(account.avg_entry.get(sym, 0.0), 4),
                "close": close,
                "pos_value": round(lots * (close or 0.0) * mult, 2),
                "unrealized_pnl": round(
                    lots * ((close or 0.0) - account.avg_entry.get(sym, close or 0.0)) * mult, 2
                ),
                "lots_a": src.get("lots_a", 0),
                "lots_b": src.get("lots_b", 0),
                "engine_source": src.get("engine_source", "NONE"),
                "group": src.get("group", "other"),
            })
        if has_pos:
            stats["days_with_positions"] += 1
        # "纯 B"：当日实际持仓的计划且全部 lots_a==0（T+1 模式第 1 日无持仓计划）
        if fill_convention == "T+1_open":
            held_idx = i - 1
        else:
            held_idx = i
        if held_idx >= 0:
            plan_for_today = plans.get(date_strs[held_idx])
            if plan_for_today is not None:
                pos_list = plan_for_today.get("combo", {}).get("positions", [])
                if pos_list and all(int(p.get("lots_a", 0)) == 0 for p in pos_list):
                    stats["pure_b_days"] += 1

    if fill_convention == "T+1_open":
        # 最后一日计划无次日 → pending（本数据 2026-08-17 为最新数据日）
        stats["pending_plans"] = int(not any(d > dates[-1] for d in pd.to_datetime(
            prices.index.get_level_values(1)).unique()))

    equity_df = pd.DataFrame(equity_rows)
    pos_df = pd.DataFrame(pos_rows)
    return account, equity_df, pos_df, stats


# ---------------------------------------------------------------------------
# 4. 生产路径一致性验证（模拟盘 vs BacktestEngine）
# ---------------------------------------------------------------------------
def pure_b_targets_oos(
    prices: pd.DataFrame, basis: pd.DataFrame, win: int, thr: float,
    notional_per_symbol: float,
) -> pd.DataFrame:
    """纯引擎 B targets（名义 180k/标的，win/thr 取自配置，OOS 切片）。

    与 p3.engine_b_targets 同构，仅名义不同（组合口径 1e6×0.2×0.9=180k）。
    """
    basis2 = basis.copy()
    basis2["br_rank"] = basis2.groupby("symbol")["basis_ratio"].transform(
        lambda s: s.rolling(win, min_periods=60).rank(pct=True)
    )
    sig = basis2[["br_rank"]].copy()
    sig["_px"] = prices["close"]
    sig = sig.dropna(subset=["_px", "br_rank"])
    mult_map = {s: CONTRACTS18[s]["multiplier"] for s in SYMBOLS18}
    sym_level = sig.index.get_level_values("symbol")
    sig["target"] = np.where(
        sig["br_rank"] >= thr,
        (notional_per_symbol / (sig["_px"] * sym_level.map(mult_map))).astype(int),
        0,
    )
    targets = sig[["target"]].sort_index()
    ts = pd.to_datetime(targets.index.get_level_values(1))
    return targets.loc[ts >= pd.Timestamp(OOS_START)]


def run_backtest_oos(cfg, prices: pd.DataFrame, basis: pd.DataFrame) -> pd.Series:
    """BacktestEngine 纯 B OOS 回测（同参数：win/thr + 180k + 费 0.005% + 滑点1tick）。

    注意：SimBroker 记账用全局 multiplier=10（品种级仅用于手续费），与准确口径差异
    在 :meth:`ShadowAccount.backtest_aligned_equity` 中单独归因。
    """
    eb = cfg.backtest.engine_b
    notional_b = INITIAL_CAPITAL * eb.notional_frac * cfg.backtest.combo.w_engine_b
    targets = pure_b_targets_oos(prices, basis, eb.win, eb.thr, notional_b)
    px_oos = prices.loc[pd.to_datetime(prices.index.get_level_values(1)) >= pd.Timestamp(OOS_START)]
    cost = CostModel.from_config(cfg)
    engine = BacktestEngine(cfg, cost=cost, initial_capital=INITIAL_CAPITAL)
    pf = engine.run(px_oos, targets)
    return pf.equity_curve


def compare_consistency(
    eq_sa_block: pd.Series,
    eq_sa_none: pd.Series,
    eq_sa_samebar: pd.Series,
    eq_sa_samebar_aligned: pd.Series,
    eq_bt: pd.Series,
) -> tuple[pd.DataFrame, dict]:
    """对齐五条路径的日收益，输出对比表 + 汇总。

    ``sa_samebar_aligned`` = 同 bar 收盘成交 + 全局 multiplier=10 记账（模拟
    SimBroker 口径），用于把"时点差异"与"multiplier 记账差异"分开归因。
    """
    def _as_dt(s: pd.Series) -> pd.Series:
        s = s.copy()
        s.index = pd.to_datetime(s.index)
        return s

    df = pd.DataFrame({
        "sa_block": _as_dt(eq_sa_block),
        "sa_none": _as_dt(eq_sa_none),
        "sa_samebar": _as_dt(eq_sa_samebar),
        "sa_samebar_aligned": _as_dt(eq_sa_samebar_aligned),
        "bt": _as_dt(eq_bt),
    })
    df = df.loc[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(how="all")
    rets = df.pct_change().dropna()

    summary: dict[str, Any] = {"paths": {}}
    for col in df.columns:
        m = compute_metrics(df[col], freq="daily")
        summary["paths"][col] = {
            "final_equity": round(float(m.final_equity), 2),
            "total_return": round(float(m.total_return), 6),
            "annual_return": round(float(m.annual_return), 6),
            "max_drawdown": round(float(m.max_drawdown), 6),
            "sharpe": round(float(m.sharpe), 4),
            "n_bars": int(m.n_bars),
        }
    corr = rets.corr()
    summary["daily_return_corr"] = {
        f"{a}~{b}": round(float(corr.loc[a, b]), 4)
        for a in df.columns for b in df.columns if a < b
    }
    summary["n_common_days"] = int(len(rets))
    return df, summary


# ---------------------------------------------------------------------------
# 5. 落盘 + 主流程
# ---------------------------------------------------------------------------
def write_artifacts(
    account: ShadowAccount,
    equity_df: pd.DataFrame,
    pos_df: pd.DataFrame,
    stats: dict,
    cfg,
    ferrous_mode: str,
    fill_convention: str,
    slippage_ticks: float,
    consistency_df: pd.DataFrame,
    consistency_summary: dict,
    bt_eq: pd.Series,
) -> None:
    ART_DIR.mkdir(parents=True, exist_ok=True)
    eq_path = ART_DIR / "equity_curve.csv"
    pos_path = ART_DIR / "positions_history.csv"
    exec_path = ART_DIR / "plan_executions.csv"
    cons_path = ART_DIR / "consistency_compare.csv"
    bt_path = ART_DIR / "consistency_bt_equity.csv"
    state_path = ART_DIR / "p17_shadow_account.json"

    equity_df.to_csv(eq_path, index=False, encoding="utf-8-sig")
    pos_df.to_csv(pos_path, index=False, encoding="utf-8-sig")
    pd.DataFrame([
        {
            "plan_date": t.plan_date, "exec_date": t.exec_date, "symbol": t.symbol,
            "delta_lots": t.delta_lots, "fill_price": round(t.fill_price, 4),
            "fee": round(t.fee, 4), "is_open": t.is_open, "note": t.note,
        }
        for t in account.trades
    ]).to_csv(exec_path, index=False, encoding="utf-8-sig")
    consistency_df.to_csv(cons_path, encoding="utf-8-sig")
    bt_eq.rename("equity").to_csv(bt_path, encoding="utf-8-sig")

    # 账户状态 + 摘要
    last = equity_df.iloc[-1].to_dict() if len(equity_df) else {}
    m = compute_metrics(equity_df.set_index("date")["equity"], freq="daily")
    state = {
        "schema_version": "1.0",
        "project": "HexBroker P17 内部簿记模拟盘（shadow account）",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "params": {
            "initial_capital": INITIAL_CAPITAL,
            "oos_window": [OOS_START, OOS_END],
            "fill_convention": fill_convention,
            "ferrous_mode": ferrous_mode,
            "fee_rate": FEE_RATE,
            "slippage_ticks": slippage_ticks,
            "margin_rate": MARGIN_RATE,
            "contracts": len(CONTRACTS18),
            "engine_a": {
                "signal_cache": cfg.backtest.engine_a.signal_cache,
                "top_k": cfg.backtest.engine_a.top_k,
                "min_symbols": cfg.backtest.engine_a.min_symbols,
                "group_cap": cfg.backtest.engine_a.group_cap,
            },
            "engine_b": {
                "win": cfg.backtest.engine_b.win,
                "thr": cfg.backtest.engine_b.thr,
                "notional_frac": cfg.backtest.engine_b.notional_frac,
            },
            "combo": {
                "w_engine_a": cfg.backtest.combo.w_engine_a,
                "w_engine_b": cfg.backtest.combo.w_engine_b,
            },
        },
        "final_state": {
            "date": last.get("date"),
            "cash": round(float(last.get("cash", account.cash)), 2),
            "equity": round(float(last.get("equity", account.initial_capital)), 2),
            "pos_value": round(float(last.get("pos_value", 0.0)), 2),
            "realized_pnl": round(float(last.get("realized_pnl", account.realized_pnl)), 2),
            "unrealized_pnl": round(float(last.get("unrealized_pnl", 0.0)), 2),
            "margin_used": round(float(last.get("margin_used", 0.0)), 2),
            "positions": {
                sym: {
                    "lots": lots,
                    "avg_entry": round(account.avg_entry.get(sym, 0.0), 4),
                    "source": account.last_plan_src.get(sym, {}),
                }
                for sym, lots in sorted(account.positions.items()) if lots != 0
            },
            "no_fill_notes": account.no_fill_notes[:20],
            "n_no_fill": len(account.no_fill_notes),
        },
        "summary": {
            "final_equity": round(float(m.final_equity), 2),
            "total_return": round(float(m.total_return), 6),
            "annual_return": round(float(m.annual_return), 6),
            "max_drawdown": round(float(m.max_drawdown), 6),
            "sharpe": round(float(m.sharpe), 4),
            "n_trading_days": int(m.n_bars) + 1,
            "fill_count": int(stats["fill_count"]),
            "buy_lots": int(stats["buy_lots"]),
            "sell_lots": int(stats["sell_lots"]),
            "executed_plans": int(stats["executed_plans"]),
            "blocked_plans": int(stats["blocked_plans"]),
            "scaled_plans": int(stats["scaled_plans"]),
            "pending_plans": int(stats["pending_plans"]),
            "no_fill_symbols": int(stats["no_fill_symbols"]),
            "pure_b_days": int(stats["pure_b_days"]),
            "days_with_positions": int(stats["days_with_positions"]),
        },
        "consistency": consistency_summary,
        "artifacts": {
            "equity_curve": str(eq_path),
            "positions_history": str(pos_path),
            "plan_executions": str(exec_path),
            "consistency_compare": str(cons_path),
            "consistency_bt_equity": str(bt_path),
            "plans_cache": str(PLANS_CACHE_DIR),
        },
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [OK] 账户状态 → {state_path}")
    for p in (eq_path, pos_path, exec_path, cons_path, bt_path):
        print(f"  [OK] {p.name} → {p}")


def spot_check_plans(plans: dict) -> None:
    """与既有 artifacts/trade_plans 逐字段比对（combo + engines），验证复用一致性。"""
    ref_dir = Path("artifacts/trade_plans")
    checked = 0
    mismatches = 0
    for fp in sorted(ref_dir.glob("*_sentinel2_plan.json")):
        ds = fp.name.replace("_sentinel2_plan.json", "")
        if ds not in plans:
            continue
        ref = json.loads(fp.read_text(encoding="utf-8"))
        got = plans[ds]
        checked += 1
        for key in ("combo", "engines"):
            if ref.get(key) != got.get(key):
                mismatches += 1
                print(f"  [WARN] {ds} {key} 与既有 P16 计划不一致")
    print(f"  [检查] 计划复用一致性：比对 {checked} 个既有计划，不一致 {mismatches}")


def print_summary(equity_df: pd.DataFrame, stats: dict, consistency_summary: dict) -> None:
    print("\n" + "=" * 88)
    print("P17 内部簿记模拟盘 — 回放摘要")
    print("=" * 88)
    m = compute_metrics(equity_df.set_index("date")["equity"], freq="daily")
    print(f"  期末权益   : {m.final_equity:>14,.2f} CNY")
    print(f"  总收益     : {m.total_return*100:>13.2f}%")
    print(f"  年化收益   : {m.annual_return*100:>13.2f}%")
    print(f"  最大回撤   : {m.max_drawdown*100:>13.2f}%")
    print(f"  Sharpe     : {m.sharpe:>14.3f}")
    print(f"  交易日数   : {m.n_bars + 1}")
    print(f"  成交笔数   : {stats['fill_count']}（买 {stats['buy_lots']} 手 / 卖 {stats['sell_lots']} 手）")
    print(f"  执行计划   : {stats['executed_plans']} | 黑色系 FAIL 阻断 {stats['blocked_plans']} | "
          f"缩量 {stats['scaled_plans']} | pending {stats['pending_plans']}")
    print(f"  纯 B 日    : {stats['pure_b_days']}（有持仓日 {stats['days_with_positions']}）")
    print(f"  无价未成交 : {stats['no_fill_symbols']}")
    print("\n[一致性验证]（模拟盘 vs BacktestEngine）")
    for path, p in consistency_summary["paths"].items():
        print(f"  {path:<12} 期末 {p['final_equity']:>12,.0f} | 总收益 {p['total_return']*100:>7.2f}% | "
              f"年化 {p['annual_return']*100:>7.2f}% | MaxDD {p['max_drawdown']*100:>7.2f}%")
    for pair, c in consistency_summary["daily_return_corr"].items():
        print(f"  日收益相关 {pair}: {c:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="P17 内部簿记模拟盘（Sentinel-2 shadow account）")
    ap.add_argument("--ferrous-mode", choices=["block", "scale", "none"], default="block",
                    help="黑色系 FAIL 处理：block=阻断（默认）/ scale=缩量 / none=不干预")
    ap.add_argument("--fill-convention", choices=["T+1_open", "same_bar_close"], default="T+1_open",
                    help="成交时点：T+1_open=生产默认（T 计划次日开盘）/ same_bar_close=对照")
    ap.add_argument("--slippage", type=float, default=SLIPPAGE_TICKS,
                    help="滑点 tick 数（默认 1，0=无滑点）")
    ap.add_argument("--reuse-plans", action="store_true",
                    help="复用 artifacts/shadow_account/plans_cache 计划缓存（存在则跳过生成）")
    ap.add_argument("--skip-consistency", action="store_true", help="跳过一致性验证（仅回放）")
    args = ap.parse_args()

    print("=" * 88)
    print("P17 内部簿记模拟盘：Sentinel-2 生产计划 → 本地 shadow account 回放")
    print(f"  成交约定: {args.fill_convention} | 黑色系 FAIL: {args.ferrous_mode} | "
          f"滑点: {args.slippage} tick | 手续费: {FEE_RATE*100:.3f}%")
    print("=" * 88)

    # ---- [1/6] 配置 + 数据 ----
    print("\n[1/6] 配置加载 + 数据加载 ...")
    cfg = load_config("configs/base.yaml")
    cfg.backtest.contracts = CONTRACTS18
    prices = load_prices_ohlc()
    basis = load_basis_panel()
    dates = oos_dates(prices)
    print(f"  OOS 交易日: {len(dates)}（{dates[0].date()} ~ {dates[-1].date()}）")
    print(f"  引擎 A: {cfg.backtest.engine_a.signal_cache} | "
          f"引擎 B: win={cfg.backtest.engine_b.win}/thr={cfg.backtest.engine_b.thr} | "
          f"组合: A{cfg.backtest.combo.w_engine_a}/B{cfg.backtest.combo.w_engine_b}")

    # ---- [2/6] 计划生成/复用 ----
    print("\n[2/6] 交易计划 ...")
    PLANS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.reuse_plans:
        plans, missing = load_plans(PLANS_CACHE_DIR, dates)
        if missing:
            print(f"  [提示] 缓存缺失 {len(missing)} 日，重新生成缺失部分 ...")
            full = generate_plans(cfg, prices, basis, dates)
            plans = full
        else:
            print(f"  [复用] 从 {PLANS_CACHE_DIR} 加载 {len(plans)} 份计划")
    else:
        plans = generate_plans(cfg, prices, basis, dates)
    spot_check_plans(plans)
    n_pos_plans = sum(1 for p in plans.values() if p["combo"]["positions"])
    n_fail = sum(1 for p in plans.values() if not p["combo"]["risk"]["ferrous_all_pass"])
    n_degraded = sum(1 for p in plans.values() if p["combo"]["degraded_to_pure_b"])
    print(f"  计划数 {len(plans)} | 有持仓计划 {n_pos_plans} | 黑色系 FAIL {n_fail} | "
          f"退化纯B {n_degraded}")

    # ---- [3/6] 主回放（生产口径：block + T+1 开盘）----
    print(f"\n[3/6] 主回放（ferrous={args.ferrous_mode}, fill={args.fill_convention}）...")
    account, equity_df, pos_df, stats = run_replay(
        plans, prices, dates,
        ferrous_mode=args.ferrous_mode,
        fill_convention=args.fill_convention,
        slippage_ticks=args.slippage,
    )
    print(f"  成交 {stats['fill_count']} 笔 | 阻断 {stats['blocked_plans']} | "
          f"期末权益 {equity_df.iloc[-1]['equity']:,.2f}")

    # ---- [4/6] 对照回放（none 模式，隔离 FAIL 干预；samebar，隔离时点）----
    print("\n[4/6] 对照回放（ferrous=none / same_bar_close）...")
    _, eq_none_df, _, stats_none = run_replay(
        plans, prices, dates, ferrous_mode="none",
        fill_convention=args.fill_convention, slippage_ticks=args.slippage,
    )
    sa_samebar_acct, eq_samebar_df, _, _ = run_replay(
        plans, prices, dates, ferrous_mode="none",
        fill_convention="same_bar_close", slippage_ticks=args.slippage,
    )
    # SimBroker 对齐权益（全局 multiplier=10 记账），隔离记账差异
    eq_samebar_aligned = pd.Series({
        d: sa_samebar_acct.backtest_aligned_equity(d, prices)
        for d in [x.strftime("%Y-%m-%d") for x in dates]
    })
    print(f"  none 模式期末 {eq_none_df.iloc[-1]['equity']:,.2f} | "
          f"samebar 期末 {eq_samebar_df.iloc[-1]['equity']:,.2f} | "
          f"samebar(SimBroker对齐) 期末 {eq_samebar_aligned.iloc[-1]:,.2f}")

    # ---- [5/6] 一致性验证 ----
    consistency_df = pd.DataFrame()
    consistency_summary: dict = {}
    if not args.skip_consistency:
        print("\n[5/6] 一致性验证（BacktestEngine 纯 B OOS）...")
        bt_eq = run_backtest_oos(cfg, prices, basis)
        print(f"  BT 期末权益 {bt_eq.iloc[-1]:,.2f}（{len(bt_eq)} 日）")
        eq_block = equity_df.set_index("date")["equity"]
        eq_none = eq_none_df.set_index("date")["equity"]
        eq_samebar = eq_samebar_df.set_index("date")["equity"]
        consistency_df, consistency_summary = compare_consistency(
            eq_block, eq_none, eq_samebar, eq_samebar_aligned, bt_eq,
        )
    else:
        bt_eq = pd.Series(dtype=float)

    # ---- [6/6] 落盘 ----
    print("\n[6/6] 落盘 ...")
    write_artifacts(
        account, equity_df, pos_df, stats, cfg,
        args.ferrous_mode, args.fill_convention, args.slippage,
        consistency_df, consistency_summary, bt_eq,
    )
    print_summary(equity_df, stats, consistency_summary)
    print("\n[DONE] P17 内部簿记模拟盘完成。")


if __name__ == "__main__":
    main()
