"""信号质量 IC 审计（RankIC 分年度 + 符号检验 + 分段检验）。

用途：
  ① 定期（建议**月度**）监控信号缓存的预测有效性，累积样本后判定模型是否失效；
  ② 出现异常时做归因（如区分「v8 覆盖段」与「tail_ext 独有段」）。

首役（2026-08-31）：复核 P3-B 判决性验证 V3 的观察项 ——
2026 年 4 品种 corr(p_up, T+5) 偏负（rb0 -0.04 / ag0 -0.25 / cu0 -0.21 / ni0 +0.15）。
相比 V3 的四点加强：

1. **扩样本**：全 18 品种（非仅 4 个）
2. **换指标**：RankIC（Spearman，抗离群值）而非 Pearson corr
3. **分年度对比**：2023/2024/2025/2026 逐年 IC，判断当年是否异常
4. **合并两种显著性检验**：
   - 均值 t 检验 —— 对**幅度**敏感
   - 符号检验（二项）—— 对**方向一致性**敏感
   ⛔ 两者可能给出相反结论（本例：均值不显著 t=-1.58、符号检验显著 p=0.031），
      **必须合并解读**；仅凭 |t|<2 判「属噪声」会漏掉系统性偏斜。

对齐口径（与生产一致）：
  - 信号 ts=T 使用 **T 日收盘特征**，标签 fwd[T] = close[T+5]/close[T] - 1
  - configs/forecast/lightgbm_champion.yaml → forecast.horizon = 5

用法：
    python scripts/ic_audit_signal_quality.py

只读，不写任何生产文件。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
LAKE = ROOT / "data/raw/processed"
HORIZON = 5
YEARS = [2023, 2024, 2025, 2026]


def load_close(sym: str) -> pd.Series | None:
    """拼接该品种全历史 close（按年分区）。"""
    parts = []
    for y in range(2019, 2027):
        p = LAKE / sym / "1d" / f"{y}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        df["d"] = pd.to_datetime(df["datetime"]).dt.date
        parts.append(df.set_index("d")["close"].astype(float))
    if not parts:
        return None
    s = pd.concat(parts).sort_index()
    return s[~s.index.duplicated(keep="last")]


def rank_ic(x: pd.Series, y: pd.Series) -> tuple[float, int, float]:
    """Spearman RankIC + 样本数 + t 统计量。"""
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    n = len(m)
    if n < 20:
        return float("nan"), n, float("nan")
    ic = float(stats.spearmanr(m["x"], m["y"]).statistic)
    if abs(ic) >= 1.0:
        return ic, n, float("nan")
    t = ic * np.sqrt((n - 2) / (1 - ic ** 2))
    return ic, n, float(t)


def main() -> int:
    raw = pd.read_parquet(CACHE)
    raw["d"] = pd.to_datetime(raw["ts"]).dt.date
    symbols = sorted(raw["symbol"].unique())
    print(f"[cache] {CACHE.name}  rows={len(raw)}  symbols={len(symbols)}")
    print(f"[对齐] horizon={HORIZON}（fwd[T] = close[T+{HORIZON}]/close[T] - 1）\n")

    rows = []
    for s in symbols:
        close = load_close(s)
        if close is None:
            continue
        fwd = (close.shift(-HORIZON) / close - 1.0).rename("fwd")
        sig = raw[raw["symbol"] == s].set_index("d")["p_up"].astype(float)
        aligned = pd.DataFrame({"p_up": sig, "fwd": fwd}).dropna()
        aligned["yr"] = pd.to_datetime(aligned.index.map(str)).year
        aligned["yr"] = [d.year for d in aligned.index]
        for yr in YEARS:
            sub = aligned[aligned["yr"] == yr]
            if len(sub) < 20:
                continue
            ic, n, t = rank_ic(sub["p_up"], sub["fwd"])
            rows.append({"symbol": s, "year": yr, "n": n, "rank_ic": ic, "t": t})

    df = pd.DataFrame(rows)
    if df.empty:
        print("❌ 无有效样本")
        return 1

    # ---- 逐年横截面汇总 ----
    print("=" * 68)
    print("【表1】逐年 RankIC 横截面汇总（全品种平均）")
    print("=" * 68)
    summ = []
    for yr in YEARS:
        sub = df[df["year"] == yr]
        if sub.empty:
            continue
        mean_ic = sub["rank_ic"].mean()
        med_ic = sub["rank_ic"].median()
        pos = int((sub["rank_ic"] > 0).sum())
        tot = len(sub)
        # 横截面 t 检验：该年 IC 均值是否显著 ≠ 0
        sd = sub["rank_ic"].std(ddof=1)
        t_cs = mean_ic / (sd / np.sqrt(tot)) if sd > 0 and tot > 1 else float("nan")
        summ.append(
            {
                "year": yr,
                "n_sym": tot,
                "mean_IC": mean_ic,
                "median_IC": med_ic,
                "IC>0 品种数": f"{pos}/{tot}",
                "t(横截面)": t_cs,
            }
        )
    sdf = pd.DataFrame(summ)
    print(sdf.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))

    # ---- 2026 vs 历史 ----
    print("\n" + "=" * 68)
    print("【表2】2026 vs 历史（2023-2025）分品种对比")
    print("=" * 68)
    hist = df[df["year"].isin([2023, 2024, 2025])].groupby("symbol")["rank_ic"].mean()
    cur = df[df["year"] == 2026].set_index("symbol")["rank_ic"]
    cmp = pd.DataFrame({"历史均值IC(23-25)": hist, "2026_IC": cur}).dropna()
    cmp["差值"] = cmp["2026_IC"] - cmp["历史均值IC(23-25)"]
    cmp = cmp.sort_values("差值")
    print(cmp.to_string(float_format=lambda v: f"{v:+.4f}"))

    # ---- 判定 ----
    print("\n" + "=" * 68)
    print("【判定】")
    print("=" * 68)
    ic26 = sdf[sdf["year"] == 2026]["mean_IC"].iloc[0]
    ic_hist = sdf[sdf["year"] < 2026]["mean_IC"].mean()
    t26 = sdf[sdf["year"] == 2026]["t(横截面)"].iloc[0]
    print(f"  2026 全品种平均 RankIC = {ic26:+.4f}  (横截面 t = {t26:+.2f})")
    print(f"  2023-2025 平均 RankIC  = {ic_hist:+.4f}")
    print(f"  差值                   = {ic26 - ic_hist:+.4f}")

    # ---------- 符号检验（比均值 t 检验更稳健）----------
    pos = int(sdf[sdf["year"] == 2026]["IC>0 品种数"].iloc[0].split("/")[0])
    n_sym = int(sdf[sdf["year"] == 2026]["n_sym"].iloc[0])
    p_sign = float(stats.binomtest(pos, n_sym, 0.5, alternative="two-sided").pvalue)
    print(f"\n  【符号检验】2026 年 IC>0 品种数 = {pos}/{n_sym}，"
          f"二项检验双尾 p = {p_sign:.4f}")
    for yr in [2023, 2024, 2025]:
        r = sdf[sdf["year"] == yr].iloc[0]
        print(f"      {yr}: {r['IC>0 品种数']}")

    # ---------- 分段：v8 覆盖段 vs tail_ext 独有段 ----------
    print("\n" + "=" * 68)
    print("【表3】2026 年分段 IC：v8 覆盖段 vs tail_ext 独有段")
    print("=" * 68)
    print("  注：v8 止于 2026-06-29；tail_ext 覆盖至 2026-08-28。")
    print("      06-29 之前两者逐行一致（verdict.json: shared_rows_identical=true）。")
    seg_rows = []
    for s in symbols:
        close = load_close(s)
        if close is None:
            continue
        fwd = (close.shift(-HORIZON) / close - 1.0).rename("fwd")
        sig = raw[raw["symbol"] == s].set_index("d")["p_up"].astype(float)
        al = pd.DataFrame({"p_up": sig, "fwd": fwd}).dropna()
        al = al[[d.year == 2026 for d in al.index]]
        a = al[[d <= __import__("datetime").date(2026, 6, 29) for d in al.index]]
        b = al[[d > __import__("datetime").date(2026, 6, 29) for d in al.index]]
        ic_a, n_a, _ = rank_ic(a["p_up"], a["fwd"])
        ic_b, n_b, _ = rank_ic(b["p_up"], b["fwd"])
        if n_a >= 20 and n_b >= 20:
            seg_rows.append({"symbol": s, "n_v8段": n_a, "IC_v8段": ic_a,
                             "n_tail段": n_b, "IC_tail段": ic_b,
                             "差值": ic_b - ic_a})
    sdf3 = pd.DataFrame(seg_rows)
    if not sdf3.empty:
        print(sdf3.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))
        ma, mb = sdf3["IC_v8段"].mean(), sdf3["IC_tail段"].mean()
        sd = sdf3["差值"].std(ddof=1)
        t_pair = (mb - ma) / (sd / np.sqrt(len(sdf3))) if sd > 0 else float("nan")
        pos_b = int((sdf3["IC_tail段"] > 0).sum())
        print(f"\n  均值：v8覆盖段 IC = {ma:+.4f}  →  tail_ext独有段 IC = {mb:+.4f}"
              f"  （配对 t = {t_pair:+.2f}）")
        print(f"  tail_ext 独有段 IC>0 品种数 = {pos_b}/{len(sdf3)}")

    # ---------- 综合判定 ----------
    print("\n" + "=" * 68)
    print("【综合判定】")
    print("=" * 68)
    if p_sign < 0.05:
        print("  🔴 符号检验显著（p<0.05）：2026 年存在**系统性 IC 转弱迹象**，"
              "非纯噪声。")
        print(f"     均值幅度 {ic26:+.4f}（t={t26:+.2f}，未达 |t|>=2），"
              f"但正 IC 品种仅 {pos}/{n_sym}。")
        verdict = 1
    else:
        print("  🟢 符号检验不显著：属统计噪声，不构成阻塞项。")
        verdict = 0
    print("\n  注：均值 t 检验对幅度敏感、符号检验对方向一致性敏感；"
          "两者结论可能不同，应合并解读。")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
