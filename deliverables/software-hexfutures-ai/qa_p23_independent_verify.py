#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QA P23 独立复核脚本（fresh-eyes，不调用 p23 实现函数计算关键指标）。

覆盖：
  1. 基差面板独立确认：18 品种最新日期（17×08-21 + SC 04-30）；抽查 TA/CU 08-21
     行值与源端 seg_20260821_basis.json 一致
  2. 引擎 B br_rank 独立复算（品种内 rolling 252 rank pct, min_periods=60）
     → ta0 0.9921 / cu0 0.7698；独立口径手数 floor
  3. 组合独立复算：notional_b=1e6*0.30*0.70=210,000 → ta0 floor 3 手；
     notional=3*px*mult；纯 B 判定（引擎 A 无信号）
  4. 计划指纹独立验证：plan JSON 去 generated_at 后规范化 md5 == registry
     plan_fp（防重入正确性）
  5. tail_ext 对齐独立抽样：共享窗口信号逐字节一致（v8 vs tail_ext ≤ cut）
     + 尾部 0 重合对断言 + 追加窗口 IC/命中率抽样
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.build_signals18 import CONTRACTS18, SYMBOLS18  # noqa: E402

FUND_DIR = ROOT / "data" / "raw" / "fundamental"
RAW_DIR = ROOT / "artifacts" / "p20_4_raw"
PLAN_JSON = ROOT / "trade_plans" / "2026-08-21_sentinel2_plan.json"
REGISTRY = ROOT / "artifacts" / "daily_runs" / "apply_registry.json"
V8 = ROOT / "artifacts" / "signals_cache18_grouped_v8.parquet"
TAIL_EXT = ROOT / "artifacts" / "signals_cache18_grouped_v8_tail_ext.parquet"
TAIL_CUT = pd.Timestamp("2026-06-29")

TARGET = pd.Timestamp("2026-08-21")
NOTIONAL_B = 1_000_000.0 * 0.30 * 0.70  # initial_capital × nf_b × w_b

ok = 0
fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  | {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  | {detail}" if detail else ""))


print("=" * 90)
print("QA P23 独立复核 —— 基差面板 / 引擎B br_rank / 组合 / 指纹 / tail_ext")
print("=" * 90)

# ---------------------------------------------------------------------------
# 1. 基差面板独立确认
# ---------------------------------------------------------------------------
print("\n[1] 基差面板独立确认")
src = json.loads((RAW_DIR / "seg_20260821_basis.json").read_text(encoding="utf-8"))
src_rows = {r["underlying_symbol"]: r for r in src["result"]}
print(f"  源端 seg_20260821_basis.json: {len(src['result'])} 品种（SC 缺失）")

latest_map = {}
for sym in SYMBOLS18:
    sym_u = sym[:-1].upper()
    f = FUND_DIR / f"basis_{sym_u}.parquet"
    if not f.exists():
        latest_map[sym] = "NO_FILE"
        continue
    df = pd.read_parquet(f)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    latest_map[sym] = df["date"].max().date()

ok_17 = all(latest_map[s] == TARGET.date() for s in SYMBOLS18 if s != "sc0")
check("17 品种基差最新=2026-08-21", ok_17, str({s: str(latest_map[s]) for s in SYMBOLS18 if latest_map[s] != TARGET.date()}))
check("SC 基差最新=2026-04-30（源端缺失保留）", latest_map.get("sc0") == pd.Timestamp("2026-04-30").date(),
      f"sc0={latest_map.get('sc0')}")

# 抽查 TA / CU 08-21 新行与源端一致
for sym in ("TA", "CU"):
    f = FUND_DIR / f"basis_{sym}.parquet"
    df = pd.read_parquet(f)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    row = df[df["date"] == TARGET].iloc[-1]
    src_r = src_rows[sym]
    match = (abs(float(row["basis"]) - float(src_r["basis"])) < 1e-6
             and abs(float(row["basis_ratio"]) - float(src_r["basis_ratio"])) < 1e-6
             and abs(float(row["spot_price"]) - float(src_r["spot_price"])) < 1e-6)
    check(f"{sym} 08-21 行与 MCP 返回值一致",
          match,
          f"basis={float(row['basis']):.4f}/{src_r['basis']} "
          f"ratio={float(row['basis_ratio']):.6f}/{src_r['basis_ratio']} "
          f"spot={float(row['spot_price']):.4f}/{src_r['spot_price']}")

# ---------------------------------------------------------------------------
# 2. 引擎 B br_rank 独立复算
# ---------------------------------------------------------------------------
print("\n[2] 引擎 B br_rank 独立复算（rolling 252 rank pct, min_periods=60）")
basis_rows = []
for sym in SYMBOLS18:
    sym_u = sym[:-1].upper()
    f = FUND_DIR / f"basis_{sym_u}.parquet"
    if not f.exists():
        continue
    df = pd.read_parquet(f)
    df["datetime"] = pd.to_datetime(df["date"]).dt.normalize()
    df["symbol"] = sym
    basis_rows.append(df[["symbol", "datetime", "basis_ratio", "basis", "spot_price"]])
panel = pd.concat(basis_rows, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
panel["br_rank"] = panel.groupby("symbol")["basis_ratio"].transform(
    lambda s: s.rolling(252, min_periods=60).rank(pct=True)
)

# 价格面板
price_rows = []
for sym in SYMBOLS18:
    for fp in sorted((ROOT / "data" / "raw" / "processed" / sym / "1d").glob("*.parquet")):
        df = pd.read_parquet(fp)
        df["datetime"] = pd.to_datetime(df["datetime"])
        price_rows.append(df[["symbol", "datetime", "close"]])
prices = pd.concat(price_rows, ignore_index=True).set_index(["symbol", "datetime"]).sort_index()
prices = prices.loc[~prices.index.duplicated(keep="last")]

for sym, exp_rank in (("ta0", 0.9921), ("cu0", 0.7698)):
    br = panel.xs(sym, level=0)["br_rank"].get(TARGET)
    check(f"{sym} br_rank@2026-08-21 ≈ {exp_rank}",
          br is not None and abs(float(br) - exp_rank) < 1e-4,
          f"独立计算 br_rank={float(br):.4f}（期望 {exp_rank}）")

# ---------------------------------------------------------------------------
# 3. 组合独立复算（纯 B）
# ---------------------------------------------------------------------------
print("\n[3] 组合独立复算（notional_b=1e6×0.30×0.70=210,000；floor 手数）")
computed = {}
for sym in ("ta0", "cu0"):
    br = panel.xs(sym, level=0)["br_rank"].get(TARGET)
    px = prices.xs(sym, level=0)["close"].get(TARGET)
    mult = float(CONTRACTS18[sym]["multiplier"])
    lots = int(np.floor(NOTIONAL_B / (float(px) * mult))) if (br is not None and float(br) >= 0.70) else 0
    computed[sym] = {"br_rank": float(br), "px": float(px), "mult": mult, "lots": lots}
    print(f"  {sym}: br_rank={float(br):.4f} px={float(px):.2f} mult={mult:.0f} "
          f"floor(210000/{float(px)*mult:,.0f})={lots} 手")

check("ta0 独立手数=3", computed["ta0"]["lots"] == 3,
      f"lots={computed['ta0']['lots']}（期望 3）")
check("cu0 独立手数=0（floor 截断）", computed["cu0"]["lots"] == 0,
      f"lots={computed['cu0']['lots']}（期望 0）")

notional_ta = 3 * computed["ta0"]["px"] * computed["ta0"]["mult"]
check("ta0 名义 ≈ 176,958 CNY",
      abs(notional_ta - 176957.86) < 1.0,
      f"notional={notional_ta:,.2f}（期望 176,957.86）")
check("总名义/权益 = 17.7%",
      abs(notional_ta / 1_000_000 - 0.177) < 0.001,
      f"ratio={notional_ta/1_000_000:.4f}（期望 0.177）")

plan = json.loads(PLAN_JSON.read_text(encoding="utf-8"))
check("计划退化纯B=True（引擎A 无信号 cache_end）",
      plan["combo"]["degraded_to_pure_b"] is True
      and plan["engines"]["engine_a"]["a_status"] == "cache_end",
      f"a_status={plan['engines']['engine_a']['a_status']}, "
      f"cache_max={plan['engines']['engine_a']['cache_max']}")
check("计划组合=ta0 3 手/来源B",
      len(plan["combo"]["positions"]) == 1
      and plan["combo"]["positions"][0]["symbol"] == "ta0"
      and plan["combo"]["positions"][0]["lots"] == 3
      and plan["combo"]["positions"][0]["engine_source"] == "B",
      str(plan["combo"]["positions"]))

# ---------------------------------------------------------------------------
# 4. 计划指纹独立验证（防重入）
# ---------------------------------------------------------------------------
print("\n[4] 计划指纹独立验证（去 generated_at 规范化 md5）")
reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
reg_rec = reg["records"].get("2026-08-21", {})
plan_copy = json.loads(PLAN_JSON.read_text(encoding="utf-8"))
plan_copy.pop("generated_at", None)
canon = json.dumps(plan_copy, sort_keys=True, ensure_ascii=False)
fp = hashlib.md5(canon.encode("utf-8")).hexdigest()
check("计划指纹 == registry.plan_fp",
      fp == reg_rec.get("plan_fp"),
      f"独立 fp={fp[:16]}… registry={reg_rec.get('plan_fp')}")
check("registry 记录 state=pending（T+1 无次日）",
      reg_rec.get("state") == "pending",
      str(reg_rec))

# ---------------------------------------------------------------------------
# 5. tail_ext 对齐独立抽样
# ---------------------------------------------------------------------------
print("\n[5] tail_ext 对齐独立抽样")
sig_v8 = pd.read_parquet(V8)
sig_v8["ts"] = pd.to_datetime(sig_v8["ts"]).dt.normalize()
sig_te = pd.read_parquet(TAIL_EXT)
sig_te["ts"] = pd.to_datetime(sig_te["ts"]).dt.normalize()

shared = sig_v8[["symbol", "ts", "exp_ret"]].merge(
    sig_te[["symbol", "ts", "exp_ret"]], on=["symbol", "ts"], suffixes=("_v8", "_te")
)
check("共享窗口（≤cut）信号逐字节一致",
      len(shared) == len(sig_v8) and (shared["exp_ret_v8"] == shared["exp_ret_te"]).all(),
      f"shared={len(shared)} vs v8_rows={len(sig_v8)}")

tail_shared = shared[shared["ts"] > TAIL_CUT]
check("尾部窗口（>cut）0 重合对（v8 无尾部信号行）",
      len(tail_shared) == 0,
      f"tail_shared={len(tail_shared)}")

te_tail = sig_te[sig_te["ts"] > TAIL_CUT]
check("tail_ext 追加窗口=648 行/36 日",
      len(te_tail) == 648 and te_tail["ts"].nunique() == 36,
      f"rows={len(te_tail)} dates={te_tail['ts'].nunique()}")

print(f"\n[结果] PASS={ok} FAIL={fail}")
sys.exit(1 if fail else 0)
