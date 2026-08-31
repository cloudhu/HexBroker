"""IC 统计效度复核 —— 移动分块自助法（moving-block bootstrap）。

为什么需要这个工具
-------------------
朴素的 IC 显著性检验（均值 t 检验 / 符号检验）在本项目上会**系统性高估显著性**，
因为两重独立性假设都不成立：

1. **标签重叠**：``forecast.horizon = 5`` → 相邻标签共用 4/5 的收益区间，
   实测 lag-1 自相关 **ρ ≈ +0.76** → 有效样本量 68 → ≈9。
2. **截面相关**：18 个商品同涨同跌，平均两两相关 **ρ̄ ≈ +0.29**
   → 有效品种数 18 → ≈3。

因此「18 个品种」不是 18 次独立抽签，「68 个标签」也不是 68 次独立观测。

本工具的做法
------------
**整截面一起重抽**（吸收截面相关）+ **按连续块重抽**（吸收时序自相关），
在不假设任何参数分布的前提下给出均值 IC 的置信区间与双尾 p 值。

首役结果（2026-08-31，2026 年 18 品种）
--------------------------------------
- 朴素：均值 RankIC = -0.0824，符号检验 4/18 → p = 0.031（看似显著）
- 分块自助：**95% CI = [-0.130, +0.028] → 含 0，双尾 p ≈ 0.147 → 不显著**
- 2025 对照：均值 +0.0068，CI = [-0.063, +0.114]；**两年 CI 大幅重叠 → 无法区分**

→ 「2026 年模型转弱」的指控因证据不足而撤回（原报告已就地更正）。

⛔ 方法论铁律
------------
**均值 t 检验与符号检验必须合并解读，且都必须先校正有效样本量。**
仅凭 |t| < 2 或 p < 0.05 下结论，在重叠标签 + 相关截面上都会被误导。

用法
----
    python scripts/ic_audit_block_bootstrap.py                 # 默认 2026 vs 2025
    python scripts/ic_audit_block_bootstrap.py --years 2024 2025 2026
    python scripts/ic_audit_block_bootstrap.py --block 15 --n-boot 5000

只读，不写任何生产文件。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
LAKE = ROOT / "data/raw/processed"
HORIZON = 5          # 与 configs/forecast/lightgbm_champion.yaml 一致
MIN_OBS = 8          # 单品种最少样本数，低于此不参与截面均值
SEED = 20260831


def load_close(sym: str) -> pd.Series:
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
        raise FileNotFoundError(f"无 K 线数据: {sym}")
    s = pd.concat(parts).sort_index()
    return s[~s.index.duplicated(keep="last")]


def build_panel(year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (p_up 宽表, 前向收益宽表)，index=日期，columns=品种。"""
    cache = pd.read_parquet(CACHE)
    cache["d"] = pd.to_datetime(cache["ts"]).dt.date
    rows = []
    for sym in sorted(cache["symbol"].unique()):
        close = load_close(sym)
        fwd = close.shift(-HORIZON) / close - 1
        g = cache[cache["symbol"] == sym].sort_values("d")
        j = pd.DataFrame({"p": g.set_index("d")["p_up"], "f": fwd}).dropna()
        j = j[j.index >= pd.Timestamp(f"{year}-01-01").date()]
        j = j[j.index < pd.Timestamp(f"{year + 1}-01-01").date()]
        if j.empty:
            continue
        j["symbol"] = sym
        rows.append(j.reset_index())
    if not rows:
        raise ValueError(f"{year} 年无可用样本")
    pnl = pd.concat(rows).rename(columns={"index": "d"})
    return pnl.pivot(index="d", columns="symbol", values="p"), pnl.pivot(
        index="d", columns="symbol", values="f"
    )


def mean_ic(piv_p: pd.DataFrame, piv_f: pd.DataFrame) -> float:
    """截面均值 RankIC（各品种 Spearman 后取均值）。"""
    vals = []
    for s in piv_p.columns:
        m = piv_p[s].notna() & piv_f[s].notna()
        if int(m.sum()) >= MIN_OBS:
            vals.append(stats.spearmanr(piv_p.loc[m, s], piv_f.loc[m, s]).statistic)
    return float(np.mean(vals)) if vals else float("nan")


def per_symbol_ic(piv_p: pd.DataFrame, piv_f: pd.DataFrame) -> dict[str, float]:
    out = {}
    for s in piv_p.columns:
        m = piv_p[s].notna() & piv_f[s].notna()
        if int(m.sum()) >= MIN_OBS:
            out[s] = float(stats.spearmanr(piv_p.loc[m, s], piv_f.loc[m, s]).statistic)
    return out


def block_bootstrap(
    piv_p: pd.DataFrame, piv_f: pd.DataFrame, block: int, n_boot: int, rng: np.random.Generator
) -> np.ndarray:
    """移动分块自助：整截面一起重抽 + 按连续块重抽。"""
    dates = piv_p.index.to_numpy()
    n = len(dates)
    if n <= block:
        raise ValueError(f"样本数 {n} <= 块长 {block}，无法分块自助")
    n_blocks = int(np.ceil(n / block))
    out = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block + 1, n_blocks)
        idx = np.concatenate([np.arange(a, a + block) for a in starts])[:n]
        bp = piv_p.iloc[idx].set_axis(dates)
        bf = piv_f.iloc[idx].set_axis(dates)
        v = mean_ic(bp, bf)
        if not np.isnan(v):
            out.append(v)
    return np.array(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="IC 分块自助法显著性复核")
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2026])
    ap.add_argument("--block", type=int, default=10, help="块长（交易日），建议 >= horizon")
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args()

    rng = np.random.default_rng(SEED)
    print("=" * 70)
    print(f"IC 分块自助法复核   块长 L={args.block}   自助 {args.n_boot} 次   horizon={HORIZON}")
    print("=" * 70)

    results = {}
    for y in args.years:
        piv_p, piv_f = build_panel(y)
        obs = mean_ic(piv_p, piv_f)
        ics = per_symbol_ic(piv_p, piv_f)
        k = sum(1 for v in ics.values() if v > 0)
        n_sym = len(ics)
        t_naive = stats.ttest_1samp(list(ics.values()), 0.0).statistic
        p_sign = stats.binomtest(k, n_sym, 0.5).pvalue

        boots = block_bootstrap(piv_p, piv_f, args.block, args.n_boot, rng)
        lo, hi = np.percentile(boots, [2.5, 97.5])
        p_boot = 2 * min((boots <= 0).mean(), (boots >= 0).mean())
        results[y] = (lo, hi)

        print(f"\n【{y}】样本 {len(piv_p)} 日 × {n_sym} 品种")
        print(f"  均值 RankIC      = {obs:+.4f}")
        print(f"  正 IC 品种       = {k}/{n_sym}")
        print(f"  朴素均值 t       = {t_naive:+.2f}")
        print(f"  朴素符号检验 p   = {p_sign:.4f}   ⛔ 未校正，倾向高估显著性")
        print(f"  分块自助 95% CI  = [{lo:+.4f}, {hi:+.4f}]")
        print(f"  分块自助双尾 p   = {p_boot:.3f}")
        print(f"  → 判定：{'✅ 显著（CI 不含 0）' if not (lo <= 0 <= hi) else '🟡 不显著（CI 含 0）'}")

    if len(results) > 1:
        print("\n" + "=" * 70)
        print("年度间 CI 是否重叠（重叠 = 无法区分）")
        ys = sorted(results)
        for a, b in zip(ys, ys[1:]):
            la, ha = results[a]
            lb, hb = results[b]
            overlap = not (ha < lb or hb < la)
            print(f"  {a} vs {b}: {'重叠 → 无法区分' if overlap else '不重叠 → 可区分'}")
    print("\n只读，未写任何生产文件。")


if __name__ == "__main__":
    main()
