#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P23-2 生产节奏自动化一键脚本：每日生产运行（数据检查 → 出计划 → 增量入账 → 摘要）。

背景
----
P16（生产信号流水线）与 P17（内部簿记模拟盘）已就绪，但每日生产需手动串联
多个命令。本脚本把每日生产节奏一键化，**复用既有函数（p16/p17），不重写引擎
逻辑**：

  1. 数据新鲜度检查：18 品种 K 线最新日期 + 17 品种基差最新日期 vs 目标日
     （默认最新交易日）。缺失/过期 → 提示（不自动拉取——拉取需 PandaData
     token/网关；可选 --update-data 做基差增量合并）。
  2. 出当日计划：复用 p16 同一套函数（engine_a_signal / engine_b_signal /
     combo_plan / write_plan + configs/base.yaml 显式加载）→ 交易计划落盘
     artifacts/trade_plans/{date}_sentinel2_plan.json/csv（同步 trade_plans/）。
  3. 增量入账：复用 p17.run_apply_date（--apply-date 语义）→ 模拟盘更新
     （T+1 开盘成交约定不变；无次日 → pending 标注，不污染账户）。
  4. 摘要报告：当日计划（合约/手数/名义/黑色系敞口）+ 模拟盘最新权益/持仓 +
     数据新鲜度状态 → stdout + artifacts/daily_runs/{date}_daily_summary.md。

防重入（幂等）
------------
  - 基差合并：p20_4_basis_update 以 (sym, seg) 登记 applied，天然幂等。
  - 计划生成：覆盖写盘，天然幂等。
  - 增量入账：以 artifacts/daily_runs/apply_registry.json 登记
    {plan_date: {plan_md5, applied_at, state, note}}；**仅 state=applied
    且同 plan_md5 跳过**（幂等防重入）；state=pending（T+1 无次日）**允许
    重试**——数据到达后重跑同 date 应把 pending 计划执行入账并转 applied
    （P24-1 QA L2 修正：旧逻辑 fp 一致即跳过、不区分状态，导致 pending
    计划在数据到达后无法自动入账）。计划内容变化（fp 变化）→ 重新入账。
    --force-apply 可强制重入（对照/审计用）。

--update-data（可选）
-------------------
  仅支持**基差**增量合并：读取 artifacts/p20_4_raw/seg_{YYYYMMDD}_basis.json
  （需先经 PandaData MCP get_future_basis 拉取并持久化——token 在网关侧，
  CLI 无法直连）→ 调 p20_4_basis_update 合并写回（备份 + applied 登记）。
  K 线增量（p6_4 流程）涉及 close_pcr 口径换算与原始价换算系数，实现复杂且
  依赖外部拉取，本脚本不内嵌（保留为独立 p6_4/手动流程），如实说明。

口径铁律：复利口径；OOS 2024-07-18 后（仅上下文标注）；修复后 broker；
生产 cap 口径（load_config("configs/base.yaml") + 显式传参）；嵌套零泄漏。
纯本地、无外部 API（--update-data 只做本地合并，不做在线拉取）。

用法
----
  python scripts/p23_daily_run.py                              # 默认最新数据日
  python scripts/p23_daily_run.py --date 2026-08-21            # 指定目标日
  python scripts/p23_daily_run.py --date 2026-08-21 --update-data  # 先基差合并再跑
  python scripts/p23_daily_run.py --date 2026-08-21 --force-apply   # 强制重新入账
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from hexbroker.config import load_config
from scripts.p2_basis_backtest import load_basis_panel, load_prices
from scripts.build_signals18 import CONTRACTS18, SYMBOLS18

import scripts.p16_daily_signal as p16
import scripts.p17_shadow_account as p17

OOS_START = "2024-07-18"  # 口径铁律：OOS 起始（仅上下文标注）

PLAN_OUT_DIR = ROOT / "artifacts" / "trade_plans"
PLAN_ROOT_DIR = ROOT / "trade_plans"
DAILY_RUNS_DIR = ROOT / "artifacts" / "daily_runs"
REGISTRY_PATH = DAILY_RUNS_DIR / "apply_registry.json"
P204_RAW_DIR = ROOT / "artifacts" / "p20_4_raw"

# p17 增量入账默认参数（与生产约定一致：block + 1 tick 滑点 + auto 锚点）
APPLY_FERROUS_MODE = "block"
APPLY_SLIPPAGE = 1.0
APPLY_ANCHOR = "auto"


# ---------------------------------------------------------------------------
# 1. 数据新鲜度检查
# ---------------------------------------------------------------------------
def _latest_per_symbol(df: pd.DataFrame, level: int = 1) -> dict[str, pd.Timestamp]:
    """MultiIndex(symbol, datetime) → 每品种最新时间戳（与 p16 同构）。"""
    return {
        sym: pd.Timestamp(g.index.get_level_values(level).max())
        for sym, g in df.groupby(level=0)
    }


def freshness_check(prices: pd.DataFrame, basis: pd.DataFrame,
                    target_date: pd.Timestamp) -> dict:
    """扫描 18 品种 K 线 + 基差最新日期 vs 目标日，返回状态。

    复用 p16.check_data_integrity 的口径（strict：latest < target 即 WARN），
    另附人读摘要：过期品种清单 / 基差断档（SC 04-30 历史缺数据）标注。
    """
    integrity = p16.check_data_integrity(prices, basis, target_date)
    px_latest = integrity["prices_latest"]
    bs_latest = integrity["basis_latest"]

    px_ok = [s for s in SYMBOLS18 if px_latest.get(s, "") >= target_date.strftime("%Y-%m-%d")]
    px_stale = [s for s in SYMBOLS18 if px_latest.get(s, "") < target_date.strftime("%Y-%m-%d")]
    bs_ok = [s for s in SYMBOLS18 if bs_latest.get(s, "") >= target_date.strftime("%Y-%m-%d")]
    bs_stale = [s for s in SYMBOLS18 if bs_latest.get(s, "") < target_date.strftime("%Y-%m-%d")]

    kline_gap_days = {}
    for sym in SYMBOLS18:
        if sym in px_latest and px_latest[sym] < target_date.strftime("%Y-%m-%d"):
            kline_gap_days[sym] = (
                target_date - pd.Timestamp(px_latest[sym])
            ).days
    basis_gap_days = {}
    for sym in SYMBOLS18:
        if sym in bs_latest and bs_latest[sym] < target_date.strftime("%Y-%m-%d"):
            basis_gap_days[sym] = (
                target_date - pd.Timestamp(bs_latest[sym])
            ).days

    return {
        "target_date": target_date.strftime("%Y-%m-%d"),
        "prices_latest": px_latest,
        "basis_latest": bs_latest,
        "px_ok": sorted(px_ok),
        "px_stale": sorted(px_stale),
        "bs_ok": sorted(bs_ok),
        "bs_stale": sorted(bs_stale),
        "kline_gap_days": kline_gap_days,
        "basis_gap_days": basis_gap_days,
        "warnings": integrity["warnings"],
        "fresh": len(px_stale) == 0 and len(bs_stale) == 0,
    }


# ---------------------------------------------------------------------------
# 2. 目标日解析
# ---------------------------------------------------------------------------
def resolve_target_date(prices: pd.DataFrame, date_arg: str | None) -> pd.Timestamp:
    """--date 显式指定；否则默认最新交易日（K 线跨品种最新日期）。"""
    if date_arg:
        return pd.Timestamp(date_arg)
    latest = pd.Timestamp(prices.index.get_level_values(1).max())
    return latest


# ---------------------------------------------------------------------------
# 3. 出当日计划（复用 p16 函数，不重写引擎逻辑）
# ---------------------------------------------------------------------------
def generate_plan(cfg, prices: pd.DataFrame, basis: pd.DataFrame,
                  target_date: pd.Timestamp) -> tuple[dict, dict, Path, Path]:
    """引擎 A/B → 组合 → 落盘（与 p16.main 步骤 3-6 同函数同参数）。"""
    engine_a = p16.engine_a_signal(prices, cfg, target_date)
    engine_b = p16.engine_b_signal(prices, basis, cfg, target_date)
    combo = p16.combo_plan(cfg, prices, engine_a, engine_b, target_date)
    integrity = p16.check_data_integrity(prices, basis, target_date)
    json_path, csv_path = p16.write_plan(
        cfg, target_date, integrity, engine_a, engine_b, combo, prices, basis,
        PLAN_OUT_DIR,
    )
    # 同步到 trade_plans/ 根目录（既有 P21/P22 约定位置，p17 回退路径）
    PLAN_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(json_path, PLAN_ROOT_DIR / json_path.name)
    shutil.copy2(csv_path, PLAN_ROOT_DIR / csv_path.name)
    return engine_a, combo, json_path, csv_path


# ---------------------------------------------------------------------------
# 4. 增量入账（复用 p17.run_apply_date + 防重入登记）
# ---------------------------------------------------------------------------
def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 损坏则重建
            pass
    return {"records": {}}


def save_registry(reg: dict) -> None:
    DAILY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def plan_fingerprint(plan_path: Path) -> str:
    """计划内容指纹（排除 generated_at 易变字段）——防重入依据。

    计划 JSON 每次重出 generated_at 都会变；业务内容（combo/engines/
    data_integrity）在数据不变时稳定。指纹去掉 generated_at 后做规范化
    JSON md5，保证"同计划内容重复运行"可被识别跳过。
    """
    if not plan_path.exists():
        return ""
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    plan.pop("generated_at", None)
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _legacy_state(record: dict) -> str:
    """向后兼容：旧 registry 记录可能无 ``state`` 字段。

    P24-1 迁移规则：缺失 state 的记录按 ``applied`` 处理（保守——保持旧版
    "fp 一致即跳过"语义，避免对可能已入账的计划重复入账污染账户）；带
    ``state`` 的记录（含 08-21 pending 记录）按实际状态处理。
    """
    return record.get("state", "applied")


def _should_skip_apply(prev: dict | None, fp: str, force: bool) -> tuple[bool, str]:
    """防重入判定：仅 ``state == applied`` 且计划指纹一致时跳过；pending 允许重试。

    P24-1 增强（QA L2）：旧逻辑按 fp 一致即跳过（不区分 applied/pending），
    导致 08-21 pending 计划（fp 一致）在数据到达后无法自动入账。现改为：
      - force=True                    → 不跳过（强制重入）
      - prev 为空                      → 不跳过（首次）
      - fp 不一致                      → 不跳过（计划内容变化，重新入账）
      - state == applied 且 fp 一致    → 跳过（幂等）
      - state == pending 且 fp 一致    → 不跳过（数据到达后允许重试入账）
      - 无 state（legacy）            → 按 applied 处理（保守迁移，见 _legacy_state）

    返回 (skip, reason)。
    """
    if force:
        return False, "force 强制重入"
    if not prev:
        return False, "首次入账"
    state = _legacy_state(prev)
    if prev.get("plan_fp") != fp:
        return False, (
            f"计划内容变化（fp {str(prev.get('plan_fp'))[:12]}… → {fp[:12]}…）→ 重新入账"
        )
    if state == "applied":
        return True, (
            f"防重入：{prev.get('applied_at')} 已入账（state=applied）且计划内容一致"
            f"（fp={fp[:12]}…）→ 跳过"
        )
    if state == "pending":
        return False, (
            f"pending 重试：{prev.get('applied_at')} 曾登记 pending（无 T+1 数据）"
            f"，数据到达后允许重试入账"
        )
    return False, f"未知 state={state!r} → 允许重试"


def apply_plan(date_str: str, force: bool = False) -> dict:
    """对 {date} 计划做增量入账（防重入）。

    返回 {applied: bool, reason: str, snapshot: dict|None}。
    """
    reg = load_registry()
    records = reg.setdefault("records", {})
    prev = records.get(date_str)

    plan_json = PLAN_OUT_DIR / f"{date_str}_sentinel2_plan.json"
    if not plan_json.exists():
        plan_json = PLAN_ROOT_DIR / f"{date_str}_sentinel2_plan.json"
    fp = plan_fingerprint(plan_json)

    skip, reason = _should_skip_apply(prev, fp, force)
    if skip:
        return {
            "applied": False,
            "reason": reason,
            "snapshot": None,
        }

    # 调用 p17.run_apply_date（--apply-date 语义：锚点续接 + MTM + T+1 开盘成交/pending）
    args = SimpleNamespace(
        apply_date=date_str,
        ferrous_mode=APPLY_FERROUS_MODE,
        slippage=APPLY_SLIPPAGE,
        apply_anchor=APPLY_ANCHOR,
    )
    p17.run_apply_date(args)

    # 读取 apply 状态快照（最新）供摘要
    snapshot = None
    latest_state = p17.ART_DIR / "p17_shadow_account_apply.json"
    if latest_state.exists():
        snapshot = json.loads(latest_state.read_text(encoding="utf-8"))

    state = "pending" if (snapshot or {}).get("pending", False) else "applied"
    records[date_str] = {
        "plan_fp": fp,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "state": state,
        "note": "T+1 无次日 → pending（计划待次日开盘入账）" if state == "pending" else "已入账",
        "force": bool(force),
    }
    save_registry(reg)
    return {
        "applied": True,
        "reason": f"已执行增量入账（state={state}）",
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# 5. 摘要报告
# ---------------------------------------------------------------------------
def _price(prices: pd.DataFrame, symbol: str, date_str: str, col: str = "close") -> float | None:
    """从 MultiIndex(symbol, datetime) 面板取某日价格（与 p17 同构）。"""
    try:
        v = prices.xs(symbol, level=0)[col].get(pd.Timestamp(date_str))
    except KeyError:
        return None
    if v is None or pd.isna(v):
        return None
    return float(v)


def _compute_account_mark(snap: dict, prices: pd.DataFrame) -> tuple[float, float, float]:
    """从 apply 快照 + 价格面板计算 pos_value / equity / unrealized（复利口径）。

    p17.apply 快照的 final_state 不含 equity/pos_value（与基线 state JSON 不同），
    这里按 持仓 × 收盘 × 品种级 multiplier 补算，避免把 equity 误显示为 cash。
    """
    pos = (snap or {}).get("positions", {})
    acct_date = (snap or {}).get("date")
    pos_value = 0.0
    unrealized = 0.0
    for sym, p in pos.items():
        lots = int(p.get("lots", 0))
        if lots == 0:
            continue
        close = _price(prices, sym, acct_date) if acct_date else None
        if close is None:
            continue
        mult = float(CONTRACTS18[sym]["multiplier"])
        avg = float(p.get("avg_entry", close))
        pos_value += lots * close * mult
        unrealized += lots * (close - avg) * mult
    cash = float((snap or {}).get("cash", 0.0))
    return pos_value, cash + pos_value, unrealized


def build_summary(date_str: str, freshness: dict, engine_a: dict, combo: dict,
                  apply_result: dict, prices: pd.DataFrame) -> str:
    plan_json = PLAN_OUT_DIR / f"{date_str}_sentinel2_plan.json"
    plan = json.loads(plan_json.read_text(encoding="utf-8")) if plan_json.exists() else {}
    positions = combo.get("positions", [])
    risk = combo.get("risk", {})
    snap = (apply_result.get("snapshot") or {}).get("final_state", {})
    if not snap:
        # 防重入跳过时无新快照 → 回读最近 apply 状态快照补充账户展示
        latest_state = p17.ART_DIR / "p17_shadow_account_apply.json"
        if latest_state.exists():
            snap = json.loads(latest_state.read_text(encoding="utf-8")).get("final_state", {})
    stats = (apply_result.get("snapshot") or {}).get("stats", {})

    lines: list[str] = []
    lines.append(f"# Sentinel-2 每日生产运行摘要 — {date_str}")
    lines.append("")
    lines.append(f"- 生成时间: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}")
    lines.append(f"- 计划来源: {plan_json.relative_to(ROOT) if plan_json.exists() else 'N/A'}")
    lines.append("")
    lines.append("## 1. 数据新鲜度")
    lines.append("")
    lines.append(f"- 目标日: {freshness['target_date']}")
    lines.append(f"- K 线最新: {min(freshness['prices_latest'].values())} ~ "
                 f"{max(freshness['prices_latest'].values())} | "
                 f"过期品种 {len(freshness['px_stale'])}")
    lines.append(f"- 基差最新: {min(freshness['basis_latest'].values())} ~ "
                 f"{max(freshness['basis_latest'].values())} | "
                 f"过期品种 {len(freshness['bs_stale'])}")
    if freshness["px_stale"]:
        lines.append(f"- K 线缺失/过期: {freshness['px_stale']} "
                     f"(gap 天数 {freshness['kline_gap_days']})")
    if freshness["bs_stale"]:
        lines.append(f"- 基差缺失/过期: {freshness['bs_stale']} "
                     f"(gap 天数 {freshness['basis_gap_days']})")
    lines.append(f"- 新鲜度结论: {'PASS（数据对齐目标日）' if freshness['fresh'] else 'WARN（见缺失清单）'}")
    lines.append("")
    lines.append("## 2. 当日交易计划")
    lines.append("")
    lines.append(f"- 引擎 A: {len(engine_a['selected'])} 个选中 "
                 f"(a_status={engine_a.get('a_status', 'N/A')}, cache_max={engine_a.get('cache_max', 'N/A')})")
    b_sel = plan.get("engines", {}).get("engine_b", {}).get("selected", [])
    lines.append(f"- 引擎 B: {len(b_sel)} 个选中 {b_sel}")
    lines.append(f"- 组合: 退化纯B={combo.get('degraded_to_pure_b', False)} | "
                 f"plan_note={plan.get('plan_note') or '（有持仓）'}")
    lines.append("")
    lines.append("| 合约 | 来源 | 手数 | 名义(CNY) | 组 |")
    lines.append("|---|---|---|---|---|")
    if positions:
        for p in positions:
            lines.append(f"| {p['symbol']} | {p['engine_source']} | {p['lots']} | "
                         f"{p['notional']:,.0f} | {p['group']} |")
    else:
        lines.append("| （空计划） | - | 0 | 0 | - |")
    lines.append("")
    lines.append(f"- 合计: {combo.get('total_lots', 0)} 手 | "
                 f"名义 {combo.get('total_notional', 0.0):,.0f} CNY "
                 f"({combo.get('total_notional_ratio', 0.0) * 100:.1f}% 权益)")
    lines.append(f"- 黑色系(ferrous_all) 敞口: {risk.get('ferrous_all_notional', 0.0):,.0f} CNY "
                 f"= {risk.get('ferrous_all_ratio', 0.0) * 100:.1f}% "
                 f"({'PASS ≤50%' if risk.get('ferrous_all_pass', True) else 'FAIL >50%'})")
    lines.append("")
    lines.append("## 3. 模拟盘增量入账")
    lines.append("")
    lines.append(f"- 入账状态: {'执行' if apply_result.get('applied') else '跳过（防重入）'}")
    lines.append(f"- 说明: {apply_result.get('reason', '')}")
    if snap:
        pos_value, equity, unrealized = _compute_account_mark(snap, prices)
        lines.append(f"- 账目日期: {snap.get('date', 'N/A')}")
        lines.append(f"- 现金: {snap.get('cash', 0.0):,.2f} CNY")
        lines.append(f"- 持仓市值: {pos_value:,.2f} CNY")
        lines.append(f"- 权益(复利): {equity:,.2f} CNY")
        lines.append(f"- 已实现盈亏: {snap.get('realized_pnl', 0.0):,.2f} CNY")
        lines.append(f"- 未实现盈亏: {unrealized:+,.2f} CNY")
        pos = snap.get("positions", {})
        pos_summary = {s: p.get("lots") for s, p in sorted(pos.items())} if pos else {}
        lines.append(f"- 持仓: {pos_summary if pos_summary else '（空仓）'}")
        if stats:
            lines.append(f"- 本次成交: {stats.get('fill_count', 0)} 笔 | "
                         f"pending={stats.get('pending_plans', 0)} | "
                         f"no_fill={stats.get('no_fill_symbols', 0)}")
    lines.append("")
    lines.append("## 4. 运行日志")
    lines.append("")
    lines.append(f"- 基差合并日志: artifacts/p23_basis_update.log（--update-data 时）")
    lines.append(f"- 计划落盘: {PLAN_OUT_DIR.relative_to(ROOT)}/{date_str}_sentinel2_plan.json/csv")
    lines.append(f"- 入账日志: artifacts/p22_apply_date_test.log（p17 --apply-date 追加）")
    lines.append("")
    return "\n".join(lines)


def print_summary(md_text: str) -> None:
    print("\n" + "=" * 88)
    print("P23-2 每日生产运行 — 摘要")
    print("=" * 88)
    for ln in md_text.splitlines():
        if ln.startswith("# "):
            continue
        if ln.startswith("|"):
            continue
        if ln.strip():
            print(ln)


# ---------------------------------------------------------------------------
# 6. --update-data：基差增量合并（本地，从持久化 JSON）
# ---------------------------------------------------------------------------
def update_basis_data(date_str: str) -> dict:
    """执行基差增量合并（p20_4_basis_update 语义），返回 {ok, msg, merged}。

    源端 JSON 需先经 PandaData MCP get_future_basis 拉取并持久化到
    artifacts/p20_4_raw/seg_{YYYYMMDD}_basis.json（token 在网关侧，CLI 无法
    在线拉取）——脚本只做合并写回（备份 + applied 登记），不内嵌在线拉取。
    """
    compact = date_str.replace("-", "")
    src = P204_RAW_DIR / f"seg_{compact}_basis.json"
    if not src.exists():
        return {
            "ok": False,
            "msg": (
                f"源端 JSON 不存在: {src.relative_to(ROOT)} —— 需先通过 PandaData MCP "
                f"get_future_basis 拉取 {compact} 并持久化（网关 token 在 MCP 侧，CLI 无法直连）"
            ),
            "merged": 0,
        }
    import scripts.p20_4_basis_update as p204

    ret = p204.main(seg=compact, src_file=src.name, target_date=date_str)
    if ret != 0:
        return {"ok": False, "msg": f"p20_4_basis_update 返回非零 {ret}", "merged": 0}
    return {
        "ok": True,
        "msg": f"基差增量合并完成（seg={compact}）",
        "merged": len([r for r in p204.load_applied() if r.get("seg") == compact]),
    }


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="P23-2 生产节奏自动化一键脚本（Sentinel-2 每日运行）")
    ap.add_argument("--date", type=str, default=None,
                    help="目标交易日 YYYY-MM-DD（默认=最新数据日）")
    ap.add_argument("--update-data", action="store_true",
                    help="先执行基差增量合并（p20_4_basis_update，源端 JSON 需已持久化）再跑 1-4")
    ap.add_argument("--force-apply", action="store_true",
                    help="强制重新增量入账（跳过防重入登记）")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 88)
    print("P23-2 生产节奏自动化一键脚本：数据检查 → 出计划 → 增量入账 → 摘要")
    print("=" * 88)

    # ---- [1] 配置 + 数据 ----
    print("\n[1/5] 配置加载 + 数据加载 ...")
    cfg = load_config("configs/base.yaml")
    prices = load_prices()
    basis = load_basis_panel()
    target_date = resolve_target_date(prices, args.date)
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"  目标日: {date_str}（K 线最新 {pd.Timestamp(prices.index.get_level_values(1).max()).date()}）")

    # ---- [2] --update-data：基差增量合并 ----
    if args.update_data:
        print("\n[2/5] --update-data 基差增量合并 ...")
        upd = update_basis_data(date_str)
        print(f"  {upd['msg']}")
        if not upd["ok"]:
            print("  [WARN] 基差合并失败/跳过 → 继续用现有数据（如实报告）")
        else:
            basis = load_basis_panel()  # 重载合并后的基差面板
    else:
        print("\n[2/5] --update-data 未启用（基差合并跳过）")

    # ---- [3] 数据新鲜度检查 ----
    print("\n[3/5] 数据新鲜度检查 ...")
    freshness = freshness_check(prices, basis, target_date)
    print(f"  K 线最新: {max(freshness['prices_latest'].values())} | "
          f"基差最新: {max(freshness['basis_latest'].values())} | "
          f"WARN×{len(freshness['warnings'])} | "
          f"新鲜={freshness['fresh']}")
    for w in freshness["warnings"]:
        print(f"    ! {w}")
    if not freshness["fresh"]:
        print("  [提示] 数据未对齐目标日（缺失清单见上）——继续出计划，结果按 p16 口径如实标注")

    # ---- [4] 出当日计划 + 增量入账 ----
    print(f"\n[4/5] 出当日计划（{date_str}）+ 增量入账 ...")
    engine_a, combo, json_path, csv_path = generate_plan(cfg, prices, basis, target_date)
    print(f"  计划落盘: {json_path.relative_to(ROOT)} (+ {csv_path.relative_to(ROOT)})")
    print(f"  计划内容: 组合 {len(combo['positions'])} 条 / {combo['total_lots']} 手 / "
          f"名义 {combo['total_notional']:,.0f} CNY")
    apply_result = apply_plan(date_str, force=args.force_apply)
    print(f"  入账: {apply_result['reason']}")

    # ---- [5] 摘要报告 ----
    print("\n[5/5] 摘要报告 ...")
    md_text = build_summary(date_str, freshness, engine_a, combo, apply_result, prices)
    DAILY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DAILY_RUNS_DIR / f"{date_str}_daily_summary.md"
    md_path.write_text(md_text, encoding="utf-8")
    print(f"  摘要落盘 → {md_path.relative_to(ROOT)}")
    print_summary(md_text)
    print(f"\n[P23-2 DONE] date={date_str} | 耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
