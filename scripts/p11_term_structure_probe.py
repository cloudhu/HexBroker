"""P11-2 期限结构斜率因子探测：数据源可行性 + 小规模 RankIC 验证（P1）。

背景（P4-2 登记方向）
------------------
期限结构斜率因子（近远月价差）作为基差动量的替代。P4-2 当时 f2 基差动量
（5/10 日变化）OOS IC≈0 被否决；本探测验证期限结构斜率是否有增量预测力。

数据源可行性（本脚本同时承担探测结论的落盘）：
  - PandaData 网关 mcp__pandadata__call_pandadata：
      get_future_dominant        → date→主力合约映射（近月）
      get_future_term_structure  → 指定合约（含已交割历史合约）的收盘价
      get_future_free_spread / get_future_calendar_arbitrage → 双合约价差（按单日期）
  - 实测结论：**可用**。历史已交割合约（如 CU2501.SHF）可拉取；合约代码格式
    CU2503.SHF / RB2503.SHF / I2503.DCE；网关单次响应约 200 行上限（需按窗口分批）。

因子构造（小规模验证）：
  - 样本：CU / RB / I 三个品种（SHFE×2 + DCE×1，覆盖 2 个交易所后缀）
  - 窗口：2024-09-02 → 2026-08-17（约 2 年）
  - 近月价：本地主力连续（data/raw/processed/{sym}0/1d，close_pcr 口径，与生产一致）
  - 远月价：PandaData 指定合约收盘价（按 ~6 个月窗口滚动：2503→2509→2603→2609）
  - slope = (far_close - near_close) / near_close
  - IC 验证：品种内时序 Spearman RankIC（slope_t vs fwd_{h}），h∈{5,10,20}，
    嵌套 cal_split=0.5，门槛 |OOS IC|>=0.03 且 IS/OOS 同号（参考 scripts/p1_factor_ic.py）

数据来源说明：
  远月合约价取自本会话 MCP 调用（mcp__pandadata__call_pandadata /
  get_future_term_structure）的响应，自动持久化在会话转录
  agent-103140f3.jsonl（function_call / function_call_result 配对）。本脚本从该
  转录提取，保证可复现；不改数据文件、不改 hexbroker 包。

落盘：artifacts/p11_term_structure_probe.md（探测结论 + 可行性评估 + IC 结果）

用法：
  python scripts/p11_term_structure_probe.py [--transcript <path>]
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

ART = ROOT / "artifacts"

# 探测品种（SHFE×2 + DCE×1，验证 2 种合约后缀）
SYMS = ["cu", "rb", "i"]
SYM_MAP = {"cu": "CU", "rb": "RB", "i": "I"}

# 远月合约窗口（近月=主力连续本地价；远月=该窗口内固定合约）
# 格式: (start, end, contract_code)
FAR_WINDOWS = [
    ("2024-09-01", "2024-10-31", "2503"),
    ("2024-11-01", "2025-07-31", "2509"),
    ("2025-08-01", "2026-02-28", "2603"),
    ("2026-03-01", "2026-08-17", "2609"),
]
PLANNED_CONTRACTS = {w[2] for w in FAR_WINDOWS}  # 仅用于正式 IC 的合约（排除可行性探测段）

HORIZONS = [5, 10, 20]
IC_THRESHOLD = 0.03


def load_near_close(sym: str) -> pd.Series:
    """主力连续收盘（本地，close_pcr 口径，与生产一致）。"""
    d = ROOT / "data" / "raw" / "processed" / f"{sym}0" / "1d"
    frames = []
    for f in sorted(d.glob("*.parquet")):
        df = pd.read_parquet(f)
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df[["datetime", "close"]])
    if not frames:
        raise FileNotFoundError(f"{sym}0 无 K 线")
    k = pd.concat(frames, ignore_index=True).drop_duplicates("datetime").sort_values("datetime")
    return k.set_index("datetime")["close"]


def extract_far_from_transcript(transcript: Path) -> pd.DataFrame:
    """从会话转录提取 get_future_term_structure 响应 → 远月面板。

    返回 DataFrame: [symbol(小写), contract(YYMM), date, far_close]
    """
    rows = []
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    with open(transcript, encoding="utf-8") as f:
        for ln in f:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            t = obj.get("type")
            if t == "function_call" and obj.get("name") == "DeferExecuteTool":
                try:
                    args = json.loads(obj.get("arguments") or "{}")
                except Exception:
                    continue
                params = args.get("params") or {}
                method = params.get("method") or (params.get("params") or {}).get("method")
                if method == "get_future_term_structure":
                    calls[obj["callId"]] = params
            elif t == "function_call_result":
                out = obj.get("output")
                if isinstance(out, dict):
                    out = out.get("text") or out.get("content")
                if isinstance(out, str):
                    try:
                        out = json.loads(out)
                    except Exception:
                        continue
                if isinstance(out, dict) and out.get("method") == "get_future_term_structure":
                    results[obj.get("callId")] = out

    import re
    for call_id, params in calls.items():
        out = results.get(call_id)
        if out is None:
            continue
        res = out.get("result") or []
        if not isinstance(res, list):
            continue
        for r in res:
            sym = str(r.get("symbol", ""))
            m = re.match(r"([A-Za-z]+)", sym)
            if not m:
                continue
            underlying = m.group(1).lower()  # CU2503.SHF -> cu
            code = re.sub(r"[^0-9]", "", sym)[-4:]  # 2503
            rows.append({
                "symbol": underlying,
                "contract": code,
                "date": pd.Timestamp(str(r["date"])),
                "far_close": float(r["close_price"]),
            })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("转录中未提取到 get_future_term_structure 响应")
    df = df.drop_duplicates(["symbol", "contract", "date"]).sort_values(["symbol", "contract", "date"])
    return df


def build_slope_panel(far: pd.DataFrame) -> pd.DataFrame:
    """近月（本地主力连续）+ 远月（指定合约）→ slope 面板。

    注意：本地近月为后复权连续价（close_pcr），与远月原始合约价存在累计复权
    因子导致的水平偏差；水平不可比，但窗口内时间变化（经 rank 变换）仍有效，
    报告中将注明该口径限制。
    """
    far = far[far["contract"].isin(PLANNED_CONTRACTS)]  # 仅保留正式窗口合约
    parts = []
    for sym in SYMS:
        up = SYM_MAP[sym]
        near = load_near_close(sym)
        far_sym = far[far["symbol"] == sym]
        if far_sym.empty:
            print(f"  [SKIP] {sym}: 无远月数据")
            continue
        # 按窗口拼接远月（同一日期只取一个远月合约）
        far_sym = far_sym.set_index("date").sort_index()
        far_merged = far_sym.groupby(level=0).last()  # 同日期多个合约时取最后一个（窗口边界）
        m = pd.DataFrame({"near_close": near})
        m = m.join(far_merged[["far_close", "contract"]], how="inner")
        m["symbol"] = sym
        m["slope"] = m["far_close"] / m["near_close"] - 1.0
        m = m.reset_index().rename(columns={m.index.name or "datetime": "date"})
        parts.append(m)
    if not parts:
        raise RuntimeError("无有效斜率面板")
    return pd.concat(parts, ignore_index=True).sort_values(["symbol", "date"])


def per_symbol_ts_ic(seg: pd.DataFrame, h: int, min_n: int = 60,
                     factor: str = "slope") -> np.ndarray:
    """品种内时序 Spearman IC（factor_t vs fwd_{h}）。"""
    ics = []
    for sym, grp in seg.groupby("symbol"):
        g = grp[[factor, f"fwd_{h}"]].dropna()
        if len(g) >= min_n:
            ic = g[factor].corr(g[f"fwd_{h}"], method="spearman")
            if pd.notna(ic):
                ics.append(ic)
    return np.array(ics)


def main() -> None:
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", type=Path, default=None,
                    help="会话转录 jsonl（默认自动探测本会话 agent-*.jsonl）")
    args = ap.parse_args()

    if args.transcript is None:
        cands = sorted(
            (Path.home() / ".workbuddy" / "projects").glob(
                "e-Workspace-HexBroker/*/subagents/agent-103140f3.jsonl"))
        if not cands:
            raise SystemExit("未找到会话转录，请用 --transcript 指定")
        args.transcript = cands[-1]

    print("=" * 100)
    print("P11-2 期限结构斜率因子探测（数据源可行性 + 小规模 RankIC）")
    print("=" * 100)
    print(f"[0] 转录: {args.transcript}")

    # ---- 1. 数据源可行性 ----
    far = extract_far_from_transcript(args.transcript)
    print(f"[1] 从转录提取 get_future_term_structure 响应: {len(far)} 行 | "
          f"品种 {sorted(far['symbol'].unique())} | "
          f"合约 {sorted(far['contract'].unique())}")
    cov = far.groupby(["symbol", "contract"]).agg(
        n=("date", "size"), d0=("date", "min"), d1=("date", "max"))
    print(cov.to_string())

    # ---- 2. 构造 slope 面板 ----
    print("\n[2] 构造 slope = (远月-近月)/近月")
    panel = build_slope_panel(far)
    panel["fwd_5"] = panel.groupby("symbol")["near_close"].transform(
        lambda s: s.shift(-5) / s - 1.0)
    panel["fwd_10"] = panel.groupby("symbol")["near_close"].transform(
        lambda s: s.shift(-10) / s - 1.0)
    panel["fwd_20"] = panel.groupby("symbol")["near_close"].transform(
        lambda s: s.shift(-20) / s - 1.0)
    # 斜率动量（P4-2 想找的是基差动量替代：slope 的 5/10 日变化）
    panel["slope_chg_5"] = panel.groupby("symbol")["slope"].diff(5)
    panel["slope_chg_10"] = panel.groupby("symbol")["slope"].diff(10)
    print(f"  面板: {len(panel)} 行 | 品种 {panel['symbol'].nunique()} | "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()}")
    for sym, grp in panel.groupby("symbol"):
        print(f"    {sym}: n={len(grp)}  slope mean={grp['slope'].mean()*100:+.2f}% "
              f"std={grp['slope'].std()*100:.2f}% min={grp['slope'].min()*100:+.2f}% "
              f"max={grp['slope'].max()*100:+.2f}%")

    # ---- 3. 品种内时序 RankIC（嵌套 cal_split=0.5）----
    dates = sorted(panel["date"].unique())
    split_dt = dates[len(dates) // 2]
    print(f"\n[3] 品种内时序 RankIC（嵌套 cal_split=0.5 → {split_dt.date()}）")
    is_seg = panel[panel["date"] <= split_dt]
    oos_seg = panel[panel["date"] > split_dt]

    summary = []
    md_rows = []
    for h in HORIZONS:
        is_arr = per_symbol_ts_ic(is_seg, h)
        oos_arr = per_symbol_ts_ic(oos_seg, h)
        if len(is_arr) == 0 or len(oos_arr) == 0:
            print(f"  h={h:>2}: 样本不足")
            continue
        t_oos = (oos_arr.mean() / (oos_arr.std() / np.sqrt(len(oos_arr)))
                 if oos_arr.std() > 0 else 0.0)
        same = np.sign(is_arr.mean()) == np.sign(oos_arr.mean())
        strong = abs(oos_arr.mean()) >= IC_THRESHOLD
        verdict = "PASS" if (strong and same) else ("WEAK" if same else "REVERSED")
        print(f"  h={h:>2}: IS IC={is_arr.mean():+.4f}(n={len(is_arr)}) | "
              f"OOS IC={oos_arr.mean():+.4f}(t={t_oos:+.2f}, n={len(oos_arr)}, "
              f"正比={np.mean(oos_arr > 0):.2f}) → {verdict}")
        summary.append({
            "h": h, "is_ic": is_arr.mean(), "oos_ic": oos_arr.mean(),
            "oos_t": t_oos, "n_sym": len(oos_arr), "verdict": verdict,
        })
        md_rows.append(f"| {h} | {is_arr.mean():+.4f} | {oos_arr.mean():+.4f} "
                       f"| {t_oos:+.2f} | {len(oos_arr)} | {verdict} |")

    # ---- 4. 每品种 IC 明细 ----
    print("\n[4] 每品种 OOS 段 IC（h=5/10/20，完整样本段内）")
    per_sym_rows = []
    for sym, grp in panel.groupby("symbol"):
        for h in HORIZONS:
            g = grp[["slope", f"fwd_{h}"]].dropna()
            if len(g) >= 60:
                ic = g["slope"].corr(g[f"fwd_{h}"], method="spearman")
                per_sym_rows.append({"symbol": sym, "h": h, "ic": ic})
    psdf = pd.DataFrame(per_sym_rows)
    if not psdf.empty:
        piv = psdf.pivot(index="symbol", columns="h", values="ic").reindex(columns=HORIZONS)
        print(piv.round(4).to_string())

    # ---- 4c. 斜率动量 IC（slope 5/10 日变化，P4-2 基差动量替代的对应口径）----
    print("\n[4c] 斜率动量 IC（slope_chg_5 / slope_chg_10 vs fwd，嵌套同 3）")
    mom_rows = []
    for fac, h in [("slope_chg_5", 5), ("slope_chg_10", 10), ("slope_chg_10", 20)]:
        is_arr = per_symbol_ts_ic(is_seg, h, factor=fac)
        oos_arr = per_symbol_ts_ic(oos_seg, h, factor=fac)
        if len(is_arr) == 0 or len(oos_arr) == 0:
            print(f"  {fac} h={h:>2}: 样本不足")
            continue
        same = np.sign(is_arr.mean()) == np.sign(oos_arr.mean())
        strong = abs(oos_arr.mean()) >= IC_THRESHOLD
        verdict = "PASS" if (strong and same) else ("WEAK" if same else "REVERSED")
        print(f"  {fac} h={h:>2}: IS IC={is_arr.mean():+.4f} | OOS IC={oos_arr.mean():+.4f} "
              f"(n={len(oos_arr)}) → {verdict}")
        mom_rows.append({"factor": fac, "h": h, "is_ic": is_arr.mean(),
                         "oos_ic": oos_arr.mean(), "verdict": verdict})

    # ---- 4b. 每窗口 IC（规避远月切换/水平跳变污染，窗口内斜率变化）----
    print("\n[4b] 每 (品种, 远月窗口) IC（h=10，窗口内 slope vs fwd）")
    win_rows = []
    for sym in SYMS:
        for w_start, w_end, code in FAR_WINDOWS:
            g = panel[(panel["symbol"] == sym)
                      & (panel["date"] >= pd.Timestamp(w_start))
                      & (panel["date"] <= pd.Timestamp(w_end))
                      & (panel["contract"] == code)][["slope", "fwd_10"]].dropna()
            if len(g) >= 40:
                ic = g["slope"].corr(g["fwd_10"], method="spearman")
                win_rows.append({"symbol": sym, "contract": code, "n": len(g), "ic": ic})
    wdf = pd.DataFrame(win_rows)
    if not wdf.empty:
        wdf_piv = wdf.pivot(index="symbol", columns="contract", values="ic").reindex(
            columns=[w[2] for w in FAR_WINDOWS])
        print(wdf_piv.round(4).to_string())
        wics = wdf["ic"].dropna().to_numpy()
        if len(wics) >= 2:
            t_w = wics.mean() / (wics.std(ddof=1) / np.sqrt(len(wics)))
            print(f"  窗口级 IC: 均值={wics.mean():+.4f} std={wics.std(ddof=1):.4f} "
                  f"t={t_w:+.2f} n={len(wics)} 正比={np.mean(wics > 0):.2f}")
        else:
            t_w = 0.0
    else:
        wdf_piv = pd.DataFrame()
        t_w = 0.0

    # ---- 5. 落盘探测报告 ----
    print("\n[5] 落盘探测报告 → artifacts/p11_term_structure_probe.md")
    far_cov_txt = cov.to_string()
    per_sym_txt = piv.round(4).to_string() if not psdf.empty else "n/a"
    win_txt = wdf_piv.round(4).to_string() if not wdf_piv.empty else "n/a"
    best = None
    if summary:
        best = max(summary, key=lambda r: abs(r["oos_ic"]))
        feasi = "可用（已拉取 3 品种 × 4 窗口 × ~2 年远月合约价，历史已交割合约可回溯）"
    else:
        feasi = "数据可用但 IC 样本不足"

    md = f"""# P11-2 期限结构斜率因子探测（数据源可行性 + 小规模 RankIC）

- 日期：2026-08-20
- 状态：数据源可行性探测 + 小规模 IC 验证（P1；P4-2 登记方向）

## 1. 数据源可行性结论

**PandaData 网关（mcp__pandadata__call_pandadata）可用**。实测方法与结论：

| 方法 | 用途 | 实测结果 |
|---|---|---|
| `get_future_dominant` | date→主力合约映射（近月） | 可用；返回 `date/symbol/trading_code`（如 20241120→CU2412.SHF，20241121→CU2501.SHF，滚动可见） |
| `get_future_term_structure` | 指定合约收盘价 | 可用；**含已交割历史合约**（如 CU2501.SHF 拉 2024-11 数据正常返回） |
| `get_future_free_spread` / `get_future_calendar_arbitrage` | 双合约价差/价比 | 存在；但按**单日期**查询（每日期一次调用），拉长序列成本高 |

**合约代码格式**（已实测 2 个交易所后缀）：
- SHFE：`CU2503.SHF` / `RB2503.SHF`
- DCE：`I2503.DCE`

**网关限制**：单次响应约 **200 行上限**（实测 2 合约 × 3.6 年请求被截断到 200 行），
需按 ~6 个月窗口分批拉取；`get_future_term_structure` 一次可传多合约但行数合计受限。

**备选数据源（未深入）**：tdx（历史记录显示 tdx 无法补已交割合约）、wind-finance。
PandaData 已满足期限结构斜率所需数据，无需强推替代源。

## 2. 因子构造（小规模验证）

- 品种：CU / RB / I（SHFE×2 + DCE×1）
- 窗口：2024-09-02 → 2026-08-17（约 2 年）
- 近月价：本地主力连续（close_pcr 口径，与生产一致）
- 远月价：PandaData 指定合约，按 ~6 个月窗口滚动（2503→2509→2603→2609）
- **slope = (远月收盘 - 近月收盘) / 近月收盘**
- 验证口径：品种内时序 Spearman RankIC（slope_t vs fwd_h），h∈{{5,10,20}}，
  嵌套 cal_split=0.5，门槛 |OOS IC|>=0.03 且 IS/OOS 同号

### 远月数据覆盖（从会话转录提取，可复现）

```
{far_cov_txt}
```

### 斜率面板统计

- 行数：{len(panel)} | 品种数：{panel['symbol'].nunique()} | {panel['date'].min().date()} ~ {panel['date'].max().date()}
- slope 水平（均值/标准差见运行输出；品种内为负/正取决于现货升贴水结构）

## 3. RankIC 验证结果（嵌套 cal_split=0.5）

| h | IS IC | OOS IC | OOS t | n品种 | 判定 |
|---|---|---|---|---|---|
{chr(10).join(md_rows) if md_rows else "（样本不足）"}

判定门槛：|OOS IC| >= {IC_THRESHOLD} 且 IS/OOS 同号 → PASS；同号但弱 → WEAK；异号 → REVERSED。

### 每品种全样本段 IC（slope vs fwd）

```
{per_sym_txt}
```

### 每 (品种, 远月窗口) IC（h=10，规避远月切换/水平跳变污染）

```
{win_txt}
```

窗口级 IC 均值：{f"{wics.mean():+.4f}" if not wdf.empty and len(wics) >= 2 else "n/a"}
（t={f"{t_w:+.2f}" if not wdf.empty and len(wics) >= 2 else "n/a"}，n={len(wics) if not wdf.empty and len(wics) >= 2 else 0}）

### 斜率动量 IC（slope 5/10 日变化，P4-2 基差动量替代的对应口径）

| 因子 | h | IS IC | OOS IC | 判定 |
|---|---|---|---|---|
{chr(10).join(f"| {r['factor']} | {r['h']} | {r['is_ic']:+.4f} | {r['oos_ic']:+.4f} | {r['verdict']} |" for r in mom_rows) if mom_rows else "（样本不足）"}

## 4. 结论与建议

- 数据源：**可行**。期限结构斜率因子所需数据（主力映射 + 指定合约历史价）可完整获取。
- 口径限制：本地近月为后复权连续价（close_pcr），与远月原始合约价存在累计复权因子
  导致**水平不可比**；但窗口内时间变化经 rank 变换仍有效（IC 为秩相关不受常数缩放
  影响）。若正式立项，建议近月也用指定合约原始价（拉取主力合约收盘），以消除该限制。
- IC 验证（小规模 3 品种 × 2 年）：见上表。若 OOS IC 显著（|IC|>=0.03 且同号）则建议立项
  构造完整 18 品种面板做正式验证；若 IC≈0 或异号则建议暂缓，改探测仓单/库存代理方向。
- 本探测为小样本（3 品种），结论仅供立项决策参考，不构成生产配置依据。
"""
    ART.mkdir(exist_ok=True)
    (ART / "p11_term_structure_probe.md").write_text(md, encoding="utf-8")

    # 同时落盘斜率面板与 IC 明细（便于复核）
    panel.to_csv(ART / "p11_ts_slope_panel.csv", index=False)
    if psdf is not None and not psdf.empty:
        psdf.to_csv(ART / "p11_ts_ic_detail.csv", index=False)
    print(f"[OK] 面板 → artifacts/p11_ts_slope_panel.csv | IC 明细 → artifacts/p11_ts_ic_detail.csv")
    print("=" * 100)
    print(f"[DONE] 总耗时 {time.time()-t0:.0f}s")
    print("=" * 100)


if __name__ == "__main__":
    main()
