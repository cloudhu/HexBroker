"""P0-10 年度口径一致性审计 v2（只读探针，零写入，外部参照校准）。

背景（37 号文档 §6.5.7 + 本日探针 v1 取证）：
- 湖内 ``raw_close`` 列恒等于 ``adj_close``（p6_4 close_pcr 管线的映射），
  **不携带名义价校准信息**，年内 k≡1.0 检测必须用外部名义价。
- 权威参照：``BackupRawFetcher(save=False)`` 拉新浪/akshare 名义价。

检测：
1. **外部校准**：k = lake.adj_close / 外部名义价。分年度统计
   [k_min, k_max, k 全年是否恒定]，k≡1 且湖内其他年度 k≠1 → 名义价冒充。
2. **内部边界跳变**：adj 序列在年度边界的跳变幅度（无外部数据时的自查线），
   交叉核对边界两日 ``is_rollover`` 标记。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker.data.backup import BackupRawFetcher  # noqa: E402

ROOT = Path("data/raw/processed")
FREQ = "1d"
START, END = "2018-01-01", "2026-08-28"
K_TOL = 1e-6
FLAT1_TOL = 5e-4  # k 恒为 1 的容差（名义价判定）
JUMP_WARN = 0.10  # 年度边界 adj 跳变告警阈值（单日涨幅物理上限量级）


def _years(sym: str) -> list[int]:
    d = ROOT / sym / FREQ
    return sorted(int(p.stem) for p in d.glob("*.parquet") if p.stem.isdigit())


def _load(sym: str, year: int) -> pd.DataFrame:
    return pd.read_parquet(ROOT / sym / FREQ / f"{year}.parquet")


def audit_symbol(sym: str, fetcher: BackupRawFetcher) -> dict:
    res: dict = {"symbol": sym, "err": "", "years": {}, "boundaries": []}
    try:
        nom = fetcher.fetch_close_series([sym], START, END)[sym]
    except Exception as exc:  # noqa: BLE001 - 逐品种隔离
        res["err"] = f"{type(exc).__name__}: {exc}"
        return res
    nom.index = pd.to_datetime(nom.index)
    nom = nom[~nom.index.duplicated()].sort_index().astype(float)

    adj_all: dict[int, pd.Series] = {}
    roll_all: dict[int, pd.Series] = {}
    for y in _years(sym):
        df = _load(sym, y)
        dt = pd.to_datetime(df["datetime"])
        adj = df["adj_close"].astype(float)
        adj.index = dt
        adj_all[y] = adj[~adj.index.duplicated()].sort_index()
        roll_all[y] = pd.Series(
            df["is_rollover"].fillna(False).astype(bool).values, index=dt
        )
        # 外部校准：本年度内逐日 k
        common = adj.index.intersection(nom.index)
        if common.empty:
            res["years"][y] = None
            continue
        k = (adj.loc[common] / nom.loc[common]).dropna()
        if k.empty:
            res["years"][y] = None
            continue
        flat1 = bool((k - 1.0).abs().max() <= FLAT1_TOL)
        # k 在年内分段恒定（换月日跳变）是后复权的**正常形态**，不告警；
        # 此处只统计形态供人工判读。
        stable = bool((k / k.median() - 1.0).abs().max() <= K_TOL)
        res["years"][y] = {
            "k_med": round(float(k.median()), 6),
            "k_min": round(float(k.min()), 6),
            "k_max": round(float(k.max()), 6),
            "flat1": flat1, "stable": stable, "n": int(len(k)),
        }

    # 内部边界跳变（不依赖外部数据）。**只看相邻年**：中间年度缺失
    # （如 rb0/2020 已隔离）时的跨界拼接跨度 >1 年，幅度大属正常累积。
    ys = sorted(adj_all)
    for a, b in zip(ys, ys[1:]):
        if b - a != 1:
            continue
        sa, sb = adj_all[a], adj_all[b]
        if sa.empty or sb.empty:
            continue
        prev, nxt = float(sa.iloc[-1]), float(sb.iloc[0])
        if prev <= 0:
            continue
        jump = nxt / prev - 1.0
        if abs(jump) > JUMP_WARN:
            res["boundaries"].append(
                (f"{a}->{b}", round(prev, 2), round(nxt, 2), f"{jump:+.2%}",
                 bool(roll_all[a].iloc[-1]), bool(roll_all[b].iloc[0]))
            )
    return res


def main() -> None:
    syms = sorted(p.name for p in ROOT.iterdir() if p.is_dir())
    fetcher = BackupRawFetcher(save=False)
    for sym in syms:
        r = audit_symbol(sym, fetcher)
        if r["err"]:
            print(f"{sym:<6} ❌ 外部参照拉取失败：{r['err']}")
            continue
        parts = []
        bad_flat = []
        for y, s in r["years"].items():
            if s is None:
                parts.append(f"{y}:无重叠")
                continue
            tag = "🚨k≡1" if s["flat1"] else ""
            parts.append(f"{y}:{s['k_med']}{tag}")
            if s["flat1"]:
                bad_flat.append(y)
        bnd = ("; ".join(
            f"{b} {c}->{d} ({e}) roll={f}/{g}"
            for b, c, d, e, f, g in r["boundaries"]) or "-")
        flag = "🚨" if bad_flat else ("⚠️" if r["boundaries"] else "✅")
        print(f"{sym:<6}{flag} 名义年={bad_flat or '-'}")
        print(f"       k明细: {' | '.join(parts)}")
        print(f"       边界跳变(>10%): {bnd}")


if __name__ == "__main__":
    main()
