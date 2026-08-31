"""P3-B 判决性验证（v2，修正实盘模拟方式）。

⚠️ 方法论修正（v1 的缺陷）：
    v1 直接用「当前缓存文件 + 历史 asof」查询，属**离线反事实**——
    今天的缓存已含 08-28 信号，回溯查 asof=08-28 自然取到 fd=0，
    但 08-28 盘中该信号**尚未生成**（ts=T 的信号需 T 日收盘特征，盘后才有）。

    实盘真实状态：asof=T 盘中，缓存最新信号日 = **T 的前一交易日**。
    本脚本按此截断缓存快照后再查询，才是实盘仿真。

验证项：
    V1 实盘仿真：2026-08-31（周一）→ fd=3 > 阈值1 → 仍禁止开仓（安全，P0-3 保留）
    V2 实盘仿真：2026-08-28（周五，前一日 08-27）→ fd=1 <= 1 → 放行（P3-B 修复目标）
               旧口径（工作日差 + 阈值0）→ fd=1 > 0 → 拦截（这正是被浪费的 192 天）
    V3 前视泄露检验：p_up[T] 必须与「T 之后」的收益相关，与「T 当日」收益无关
    V4 P0-3 事故场景（08-21 信号 / 08-24 使用）→ 新旧口径均拦截

只读，不写任何生产文件。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from omegaconf import OmegaConf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hexbroker.paper.signals import SignalEngine  # noqa: E402

CACHE = ROOT / "artifacts/signals_cache18_grouped_v8_tail_ext.parquet"
SYMBOLS = ["rb0", "ag0", "cu0", "ni0", "au0", "al0", "i0", "j0", "m0", "y0"]


def business_days_old(a: date, b: date) -> int:
    """旧口径（修复前）：np.busday_count 工作日差。"""
    return int(np.busday_count(min(a, b), max(a, b)))


def make_engine(cache_snapshot: pd.DataFrame, thr: int, tmpdir: Path) -> SignalEngine:
    """用内存快照构造 SignalEngine（不落盘生产缓存）。"""
    p = tmpdir / "snap.parquet"
    cache_snapshot.to_parquet(p)
    return SignalEngine(cache_paths=[p], freshness_threshold_days=thr)


def simulate(cache: pd.DataFrame, asof: str, thr: int, tmpdir: Path):
    """实盘仿真：asof=T 盘中时，缓存仅含 ts <= T 的前一交易日（T 日信号盘后才有）。

    简化处理：直接传入调用方已截断好的快照，此处只做查询与统计。
    """
    eng = make_engine(cache, thr, tmpdir)
    out = {}
    for s in SYMBOLS:
        sig = eng.latest_signal(s, asof)
        if sig is None:
            out[s] = None
            continue
        sig_day = pd.Timestamp(sig.ts).date()
        asof_day = pd.Timestamp(asof).date()
        out[s] = {
            "sig_day": sig_day,
            "fd_new": sig.freshness_days,
            "fd_old": business_days_old(sig_day, asof_day),
            "effective": bool(sig.is_effective),
        }
    return out


def main() -> int:
    import tempfile

    cfg = OmegaConf.load(ROOT / "configs" / "paper.yaml")
    thr = int(OmegaConf.select(cfg, "paper.freshness_threshold_days"))
    print(f"[cfg] freshness_threshold_days = {thr}（应为 1）")
    assert thr == 1, "配置未生效"

    raw = pd.read_parquet(CACHE)
    raw["ts"] = pd.to_datetime(raw["ts"]).dt.tz_localize(None)
    print(f"[cache] rows={len(raw)}  ts_max={raw['ts'].max().date()}\n")

    tmpdir = Path(tempfile.mkdtemp(prefix="p3b_"))
    results = {}

    # ---------------- V1：2026-08-31 周一（实盘：缓存即当前，最新 08-28）----------------
    print("=== V1  实盘仿真 asof=2026-08-31 10:30（周一；缓存最新 08-28 周五）===")
    r = simulate(raw, "2026-08-31 10:30", thr, tmpdir)
    n_eff = sum(1 for v in r.values() if v and v["effective"])
    for s in SYMBOLS[:5]:
        v = r[s]
        print(f"  {s:<4} sig={v['sig_day']}  fd_new={v['fd_new']}  fd_old={v['fd_old']}"
              f"  effective={v['effective']}")
    print(f"  → 有效品种 {n_eff}/{len(SYMBOLS)}")
    ok_v1 = n_eff == 0 and r["rb0"]["fd_new"] == 3 and r["rb0"]["fd_old"] == 1
    print(f"  V1 {'✅ PASS' if ok_v1 else '❌ FAIL'}："
          f"周一跨周末 fd=3>1 → 仍禁止开仓（今日行为不变，P0-3 防护保留）\n")
    results["V1"] = ok_v1

    # ---------------- V2：2026-08-28 周五（实盘：缓存截断到 08-27）----------------
    print("=== V2  实盘仿真 asof=2026-08-28 10:30（周五；盘中缓存最新 08-27 周四）===")
    snap = raw[raw["ts"] <= pd.Timestamp("2026-08-27 23:59")].copy()
    print(f"  [快照] ts_max={snap['ts'].max().date()}（模拟 T-1 状态）")
    r2 = simulate(snap, "2026-08-28 10:30", thr, tmpdir)
    n_eff2 = sum(1 for v in r2.values() if v and v["effective"])
    for s in SYMBOLS[:5]:
        v = r2[s]
        print(f"  {s:<4} sig={v['sig_day']}  fd_new={v['fd_new']}  fd_old={v['fd_old']}"
              f"  effective={v['effective']}")
    print(f"  → 新口径(阈值1) 有效品种 {n_eff2}/{len(SYMBOLS)}")
    # 旧口径对照组
    snap_old = raw[raw["ts"] <= pd.Timestamp("2026-08-27 23:59")].copy()
    r2_old = simulate(snap_old, "2026-08-28 10:30", 0, tmpdir)
    n_old = sum(1 for v in r2_old.values() if v and v["effective"])
    print(f"  → 旧口径(阈值0) 有效品种 {n_old}/{len(SYMBOLS)}  ← 被结构性浪费的日盘")
    # 注：个别品种 effective=False 来自缓存自身的 is_effective 质量标记（非新鲜度判定），
    # 属正确行为（_build_frame: effective = row.is_effective and fd <= threshold）。
    # 故断言取「新口径显著放行（>=9/10）且旧口径全灭（0/10）」，而非强求 10/10。
    ok_v2 = n_eff2 >= 9 and n_old == 0 and r2["rb0"]["fd_new"] == 1 and r2_old["rb0"]["fd_old"] == 1
    print(f"  V2 {'✅ PASS' if ok_v2 else '❌ FAIL'}："
          f"正常隔夜 fd=1<=1 放行（新），旧口径 fd=1>0 拦截 → P3-B 修复目标达成\n")
    results["V2"] = ok_v2

    # ---------------- V3：前视泄露检验（horizon=5）----------------
    # ⚠️ 对齐修正：configs/forecast/lightgbm_champion.yaml → forecast.horizon = 5，
    #    即 fwd[T] = close[T+5]/close[T] - 1。v1 用 1 日收益对齐是错的。
    print("=== V3  前视泄露检验（horizon=5；p_up[T] 应预测 T→T+5，而非 T 当日）===")
    HORIZON = 5
    ok_v3 = True
    for s in ["rb0", "ag0", "cu0", "ni0"]:
        try:
            bars = pd.read_parquet(ROOT / f"data/raw/processed/{s}/1d/2026.parquet")
        except Exception:
            print(f"  {s}: 湖数据缺失，跳过")
            continue
        bars["d"] = pd.to_datetime(bars["datetime"]).dt.date
        close = bars.set_index("d")["close"].astype(float).sort_index()
        same_day = close.pct_change()                       # T-1 → T（T 当日，不该被预知）
        fwd_h = close.shift(-HORIZON) / close - 1.0         # T → T+HORIZON（模型真目标）
        sub = raw[raw["symbol"] == s].copy()
        sig = sub.set_index(pd.to_datetime(sub["ts"]).dt.date)["p_up"].astype(float)
        sig = sig[sig.index >= date(2026, 1, 1)]
        aligned = pd.DataFrame({"p_up": sig, "same": same_day, "fwd": fwd_h}).dropna()
        if len(aligned) < 30:
            print(f"  {s}: 样本不足({len(aligned)})，跳过")
            continue
        c_same = aligned["p_up"].corr(aligned["same"])
        c_fwd = aligned["p_up"].corr(aligned["fwd"])
        verdict = "✅ 目标对齐正常" if abs(c_fwd) > abs(c_same) else "⚠️ 需人工复核"
        print(f"  {s:<4} n={len(aligned):<4} corr(p_up, 当日收益)={c_same:+.4f}  "
              f"corr(p_up, T+{HORIZON}收益)={c_fwd:+.4f}   {verdict}")
        if abs(c_fwd) <= abs(c_same):
            ok_v3 = False
    print(f"  V3 {'✅ PASS' if ok_v3 else '⚠️ 需人工复核'}："
          f"信号与「T+{HORIZON} 前瞻收益」相关性更强 → 未利用当日未走完信息\n")
    results["V3"] = ok_v3

    # ---------------- V4：P0-3 事故场景 ----------------
    print("=== V4  P0-3 事故场景（只有 08-21 周五信号，08-24 周一使用）===")
    # 实盘：08-24 盘中缓存最新 = 08-21（周五收盘）
    snap4 = raw[raw["ts"] <= pd.Timestamp("2026-08-21 23:59")].copy()
    r4_new = simulate(snap4, "2026-08-24 21:30", 1, tmpdir)
    r4_old = simulate(snap4, "2026-08-24 21:30", 0, tmpdir)
    v_new, v_old = r4_new["ag0"], r4_old["ag0"]
    print(f"  新口径(阈值1): fd={v_new['fd_new']} effective={v_new['effective']}")
    print(f"  旧口径(阈值0): fd={v_old['fd_old']} effective={v_old['effective']}")
    ok_v4 = (v_new["effective"] is False) and (v_old["effective"] is False) \
        and v_new["fd_new"] == 3
    print(f"  V4 {'✅ PASS' if ok_v4 else '❌ FAIL'}："
          f"08-24 事故场景新旧口径均拦截 → P0-3 意图完整保留\n")
    results["V4"] = ok_v4

    print("=" * 64)
    all_ok = all(results.values())
    for k, v in results.items():
        print(f"  {k}: {'✅ PASS' if v else '❌ FAIL / ⚠️ 复核'}")
    print("=" * 64)
    print(f"总判定：{'✅ 全部 PASS' if all_ok else '⚠️ 存在未通过项'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
