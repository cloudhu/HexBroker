"""P6 任务二：引擎 A 信号端覆盖率诊断（只读分析，不修改生产代码）。

背景
----
``artifacts/signals_cache18_grouped_v2.parquet`` 仅 976 个信号日、7952 条信号，
且覆盖率随时间骤降（2019-21 日均 13 品种 → 2024 5.7 → 2026 仅 2.2 品种/天）。
本脚本定位根因，输出量化证据（月度/年度统计表 + 数据缺口表 + 低谷月品种构成），
并把统计表落盘 ``artifacts/p6_coverage_diag.csv``。

生产链路（只读引用）
--------------------
group_modeling_v2.py → build_group_signals → walk_forward_lightgbm
（scripts/refine_lightgbm_champion.py）→ 每折叠只保留测试窗信号，且
``cal_split=0.5`` 时仅返回测试窗后半（``_calibrate_and_split`` 返回 ``sigs[k:]``）。

运行
----
    python scripts/p6_signal_coverage_diag.py
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SIGNAL_CACHE = ROOT / "artifacts" / "signals_cache18_grouped_v2.parquet"
OUT_CSV = ROOT / "artifacts" / "p6_coverage_diag.csv"
SYMBOLS18 = [
    "au0", "ag0", "m0", "cu0", "rb0", "i0",
    "al0", "zn0", "ni0", "hc0", "y0", "p0", "j0", "jm0", "sr0", "cf0", "ta0", "sc0",
]


def load_signal_cache() -> pd.DataFrame:
    sig = pd.read_parquet(SIGNAL_CACHE)
    sig["ts"] = pd.to_datetime(sig["ts"])
    return sig


def load_data_days() -> pd.DataFrame:
    """每个品种本地 1d parquet 的交易日数（按年）与起止日期。"""
    rows = []
    for sym in SYMBOLS18:
        parts = []
        for fp in sorted(glob.glob(str(ROOT / "data" / "raw" / "processed" / sym / "1d" / "*.parquet"))):
            parts.append(pd.read_parquet(fp, columns=["datetime"]))
        if not parts:
            rows.append({"sym": sym, "n_total": 0, "start": None, "end": None, **{str(y): 0 for y in range(2018, 2027)}})
            continue
        d = pd.concat(parts)
        d["datetime"] = pd.to_datetime(d["datetime"])
        yr = d["datetime"].dt.year.value_counts()
        rows.append({
            "sym": sym,
            "n_total": int(len(d)),
            "start": d["datetime"].min(),
            "end": d["datetime"].max(),
            **{str(y): int(yr.get(y, 0)) for y in range(2018, 2027)},
        })
    return pd.DataFrame(rows)


def print_table(df: pd.DataFrame, title: str) -> None:
    print(f"\n=== {title} ===")
    print(df.to_string(index=False))


def main() -> None:
    sig = load_signal_cache()
    print("=" * 76)
    print("P6 任务二：引擎 A 信号端覆盖率诊断（只读）")
    print("=" * 76)

    # ---- 0. 总体 ----
    print("\n[0] 信号缓存总体")
    print(f"  文件: {SIGNAL_CACHE}")
    print(f"  总信号: {len(sig)} | 唯一信号日: {sig['ts'].nunique()} | 品种: {sig['symbol'].nunique()}")
    print(f"  时间范围: {sig['ts'].min().date()} -> {sig['ts'].max().date()}")
    print(f"  is_effective: {sig['is_effective'].value_counts(dropna=False).to_dict()}")

    # ---- 1. 年度统计 ----
    sig = sig.copy()
    sig["year"] = sig["ts"].dt.year
    sig["ym"] = sig["ts"].dt.to_period("M")
    yearly = (
        sig.groupby("year")
        .agg(n_signals=("symbol", "count"), n_symbols=("symbol", "nunique"), n_days=("ts", "nunique"))
        .reset_index()
    )
    yearly["avg_symbols_per_day"] = (yearly["n_signals"] / yearly["n_days"]).round(2)
    print_table(yearly, "年度覆盖统计（覆盖率随时间下降）")

    # ---- 2. 月度统计 ----
    monthly = (
        sig.groupby("ym")
        .agg(n_signals=("symbol", "count"), n_symbols=("symbol", "nunique"), n_days=("ts", "nunique"))
        .reset_index()
    )
    monthly["avg_symbols_per_day"] = (monthly["n_signals"] / monthly["n_days"]).round(2)
    monthly["ym"] = monthly["ym"].astype(str)
    print_table(monthly, "月度覆盖统计（锯齿：高峰月 ~15 品种 / 低谷月 1-4 品种）")

    # ---- 3. 分品种 ----
    per_sym = (
        sig.groupby("symbol")
        .agg(n_signals=("symbol", "count"), first_ts=("ts", "min"), last_ts=("ts", "max"), n_days=("ts", "nunique"))
        .reset_index()
        .sort_values("symbol")
    )
    per_sym["first_ts"] = per_sym["first_ts"].dt.date
    per_sym["last_ts"] = per_sym["last_ts"].dt.date
    print_table(per_sym, "分品种信号统计（每品种 ~400-448 条 = 折叠数 × 每折输出 16 条）")

    # ---- 4. 本地数据缺口（根因 2/3 证据） ----
    dd = load_data_days()
    print("\n[4] 本地数据缺口（各品种各年交易日数 vs 当年最大）")
    yrs = [str(y) for y in range(2018, 2027)]
    dd["max_per_year"] = dd[yrs].max(axis=1)
    dd["gap_total"] = dd.apply(lambda r: sum(int(r.max_per_year) - int(r[y]) for y in yrs), axis=1)
    gap_cols = [f"gap{y}" for y in range(2018, 2027)]
    for y in yrs:
        dd[f"gap{y}"] = dd["max_per_year"] - dd[y]
    show_cols = ["sym", "n_total", "start", "end", "gap_total", "max_per_year"] + [f"gap{y}" for y in range(2018, 2027)]
    dd2 = dd[show_cols].copy()
    dd2["start"] = dd2["start"].dt.date
    dd2["end"] = dd2["end"].dt.date
    print_table(dd2, "数据缺口表（gap=当年最大交易日 - 该品种交易日；>0 即缺数据）")

    # ---- 5. 低谷月品种构成（验证相位漂移：低谷=数据缺口品种的 run 错开） ----
    print("\n[5] 低谷月品种构成（验证折叠相位漂移）")
    low_months = ["2022-02", "2023-02", "2023-05", "2024-01", "2024-05", "2025-01", "2026-03"]
    for m in low_months:
        g = sig[sig["ym"].astype(str) == m]
        print(f"  {m}: n={len(g):4d} symbols={sorted(g['symbol'].unique())}")

    # ---- 6. is_effective 分布 + 校准区间 ----
    print("\n[6] is_effective 与校准区间")
    print(f"  False 条数: {int((~sig['is_effective']).sum())} / {len(sig)}")
    eff = sig[~sig["is_effective"]]
    if len(eff):
        print(f"  False 的 |p_up-0.5| 范围: {abs(eff['p_up']-0.5).min():.4f} ~ {abs(eff['p_up']-0.5).max():.4f}（阈值 0.05）")

    # ---- 7. 折叠机制验证：每品种信号数 ≈ 折叠数 × 每折输出 ----
    print("\n[7] 折叠机制验证（walk_forward 只保留测试窗后半）")
    n_folds_est = {}
    for sym in SYMBOLS18:
        r = dd[dd["sym"] == sym]
        if len(r) == 0 or r["n_total"].iloc[0] == 0:
            n_folds_est[sym] = 0
            continue
        n = int(r["n_total"].iloc[0])
        # train_len=250, test_len=60, 每折输出 ≈ (60 - 30 lookback + 1) * 0.5(cal_split)
        folds = max(0, (n - 250) // 60)
        per_fold_out = int((60 - 30 + 1) * 0.5)  # 16
        n_folds_est[sym] = folds * per_fold_out
    est = pd.DataFrame([{"symbol": s, "est_signal_upper": n_folds_est[s]} for s in SYMBOLS18])
    merged = per_sym.merge(est, on="symbol", how="left")
    merged["coverage_of_est"] = (merged["n_signals"] / merged["est_signal_upper"].replace(0, np.nan)).round(2)
    print_table(merged, "每品种信号数 vs 折叠估算上限（≈1.0 说明每折只输出后半 16 条）")

    # ---- 8. 落盘 CSV（窄长格式，便于任何工具读取） ----
    low = sig[sig["ym"].astype(str).isin(low_months)].groupby("ym").agg(
        n_signals=("symbol", "count"), symbols=("symbol", lambda x: ",".join(sorted(set(x))))
    ).reset_index()
    long_rows: list[dict] = []

    def melt_table(k: str, df: pd.DataFrame, id_col: str, value_cols: list[str]) -> None:
        for _, r in df.iterrows():
            for vc in value_cols:
                long_rows.append({"k": k, "id": str(r[id_col]), "metric": vc, "value": r[vc]})

    melt_table("monthly", monthly, "ym", ["n_signals", "n_symbols", "n_days", "avg_symbols_per_day"])
    melt_table("yearly", yearly, "year", ["n_signals", "n_symbols", "n_days", "avg_symbols_per_day"])
    melt_table("per_symbol", per_sym, "symbol", ["n_signals", "n_days", "first_ts", "last_ts"])
    melt_table("data_gap", dd2, "sym", ["n_total", "gap_total", "max_per_year"] + [f"gap{y}" for y in range(2018, 2027)])
    for _, r in low.iterrows():
        long_rows.append({"k": "low_month", "id": str(r["ym"]), "metric": "symbols", "value": r["symbols"]})
        long_rows.append({"k": "low_month", "id": str(r["ym"]), "metric": "n_signals", "value": r["n_signals"]})
    out = pd.DataFrame(long_rows)
    out.to_csv(OUT_CSV, index=False)
    print(f"\n[OK] 统计表已落盘（窄长格式）: {OUT_CSV}")

    # ---- 9. 根因结论 ----
    print("\n" + "=" * 76)
    print("根因结论")
    print("=" * 76)
    print("""
根因 1（结构性，所有品种共有）：walk_forward 只保留每折叠测试窗的后半段。
  - walk_forward_lightgbm 每折叠对测试窗 predict（test_len=60），build_windows 损耗
    lookback=30 → 约 31 个信号；cal_split=0.5 时 _calibrate_and_split 用测试窗前 50%
    拟合校准器并返回 sigs[k:]（后半）→ 每折叠实际只输出约 16 个交易日信号。
  - 信号呈"run"块状（每 ~60 交易日输出 ~16 交易日），总覆盖率仅 ~25-27%，
    976 个信号日 < 全时段 ~2100 交易日。

根因 2（主因，覆盖率随时间下降）：品种数据缺口不同 → 整数索引折叠网格日历相位漂移。
  - 折叠网格基于品种自身整数索引（splitter.split(feat.index)），测试窗日期 =
    "该品种第 N 个交易日"。各品种中途数据缺口不同（cu0/rb0/i0 2022 缺 28 天；
    au0/ag0/m0 2024 缺 ~111 天；cu0/rb0 2024 缺 ~130 天）→ 同一折叠序号映射到不同
    日历日 → 各品种 run 块在日历上错开。
  - 2020-2021 数据完整 → 全部 18 品种 run 对齐 → 18 品种/天；
    2022 起 cu0/rb0/i0 相位落后 → 高峰月 15 品种 / 低谷月 3 品种（恰为 cu0/i0/rb0）；
    2024 年 cu0/rb0 再漂移 → 低谷月仅剩 i0（1 品种）。

根因 3（直接原因，2026 骤降）：15/18 品种本地数据止于 2026-02-24。
  - 仅 au0/ag0/m0 的 2026.parquet 到 2026-08-14；其余 15 品种 2026 仅 31 天 →
    2026-02-24 后只剩 3 品种能产出信号 → 2026 年 2.24 品种/天。
  - 且 au0 等 2024 缺 ~110 天 → 整数索引 1930 对应的日历日提前到 2026-06-10，
    即使数据到 08-14，最后一个测试窗输出也只到 06-10。

is_effective（280 条 False）：is_effective = |p_up - 0.5| > 0.05（校准后重算）。
  280 条 False = 校准（Platt）后概率落在 [0.45, 0.55] 的弱信号，占 3.5%，非异常。

exp_ret 校准链路：exp_ret 由 LightGBM 均值模型直接输出，不经过 Platt/Isotonic
  校准（校准只改 p_up 与 is_effective）；但 cal_split=0.5 会丢弃每折叠测试窗前半
  信号 → 覆盖率结构性减半。
""")

    print("=" * 76)
    print("修复方案（2+ 可执行选项）")
    print("=" * 76)
    print("""
方案 A（推荐，主修信号端）：walk_forward 全窗输出拼接。
  - 在 _calibrate_and_split 增加"校准后返回全部信号"模式（用前 cal_split 比例
    拟合校准器、评估全部），或直接 cal_split=None（全测试窗拟合校准器 + 输出全部）。
  - 每折叠输出从 16 条 → 31 条，覆盖率翻倍；预计每个品种 ~900 条信号，976 天 → ~1900 天。
  - 改动点：scripts/refine_lightgbm_champion.py 的 _calibrate_and_split / walk_forward
    调用参数；group_modeling_v2.py 传 cal_split=None。注意保持嵌套零泄漏口径。

方案 B（补数据，治 2026 骤降）：补齐 15 个品种 2026-02-24 后的本地数据到 2026-08-17，
  并回补 cu0/rb0/i0 2022 年 28 天、au0/ag0/m0/cu0/rb0 2024 年 110-130 天的缺口
  （疑似 fetch 失败/停牌误删）。数据完整后各品种折叠相位自然对齐。

方案 C（信号缓存重建）：在方案 A+B 落地后重跑 group_modeling_v2.py 重建
  signals_cache18_grouped_v2.parquet；或在重建前为下游 p5_engineA_cross_section.py
  增加"当日品种数 < min_symbols 时降级"的保护（P5 已实现 min_symbols=3 的防御）。

方案 D（评估窗延长）：把 test_len 从 60 提到 120+，每折叠输出更宽 → run 更宽、
  覆盖率上升；但不解决数据缺口导致的相位漂移，仅作辅助。
""")


if __name__ == "__main__":
    main()
