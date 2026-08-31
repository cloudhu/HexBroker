#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P步-A 本地替代管线：tqsdk_source 拉取日线 + 主湖 k 锚定换算，
产出与 pandadata close_pcr **完全同格式** 的 persisted JSON，
供 p6_4_apply_persisted_dir.py 无改动消费。

2026-08-30 立项动因：pandadata 网关 500009「单日总流量超限」是盘前刷新的
单点故障（2026-08-28 停摆根因）。本脚本用采集器同源模块（tqsdk_source，
自有账号、无配额网关）替代 pandadata 拉取，主湖自身作为复权口径锚。

口径体系（2026-08-30 周五样本 18 品种三方对质实证）
--------------------------------------------------
- 主湖 ``close``/``adj_close`` = 后复权主力连续（p6_4 close_pcr 口径），
  ``raw_close`` = 未复权名义价；后复权价 = 名义价 × k，
  **k 在同一主力段内为常数（实测波动 ~1e-16）、段间跳变**；
- tqsdk ``KQ.m`` bar = 未复权名义价 ≈ 主湖 ``raw_close``；
- pandadata close_pcr ≡ 主湖 close（周五样本逐日分毫不差）——湖与
  pandadata 无口径分歧。主连换月时点分歧（jm0 实测 2026-08-30 与真实
  合约逐日对照）：湖/pandadata 0817 已切 jm2701，tqsdk KQ.m 0819 才切
  （官方规则「持仓量与成交量均为最大后，下一交易日开盘后切换」偏保守，
  官方实证 KQ.m = 当日主力合约原始价、硬拼接不复权）。

算法（v2：对齐过滤 + 接缝校验）
------------------------------
1. 重叠日（窗口 ∩ 湖）逐日实测 ratio = lake.close / lake.raw_close，
   以及对齐性 aligned = |tqsdk.close / lake.raw_close - 1| ≤ RAW_ALIGN_TOL；
2. k = 「最新对齐稳定段」均值：从最后一个重叠日向前回溯，要求逐日
   aligned 且 ratio 相对末值波动 ≤ K_TOL；段长 < MIN_OVERLAP → 该品种失败；
3. 接缝校验：湖最新日必须在对齐稳定段内（否则 tqsdk 与湖在接缝处
   基准不一致 —— 正是换月领先窗口 → 拒绝扩展，回退 pandadata）；
4. 发射 = 扩展日（湖最新日之后；采集湖 1d 分区优先，tqsdk 兜底）
   + 对齐稳定段重叠日（幂等重发，实测与湖偏差 ~1e-10）；
   **未对齐日绝不发射**（p6_4 merge keep="last" 会覆盖湖内历史值）；
5. 换月疑点守卫：扩展区价格跳变 > 限幅×1.02 或 OI 跳变 > 45% → 拒绝。

幂等性：对齐日重发值与湖偏差 ~1e-10，远低于 p6_4 重叠容差 1%；
扩展日为湖中不存在的新日期，merge 即纯追加。

用法
----
  # 生产（交易日 08:00，当日 bar 未成型 → 排除今日）
  python scripts/refresh_pull_local.py --exclude-today
  # 生产（交易日 20:30；默认自动：asof=今天且已过 15:30 → 含今日）
  python scripts/refresh_pull_local.py
  # 验证（周日对质周五 pandadata 样本，写独立测试目录）
  python scripts/refresh_pull_local.py --asof 2026-08-28 \
      --out-dir artifacts/p6_4_pull_20260828_localtest

退出码：0 全部品种落盘 | 2 tqsdk 整体拉取失败 | 3 部分品种失败
（明细见 ``_LOCAL_REPORT.md``，调用方须整体回退 pandadata，禁止部分成功）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PROCESSED_DIR = PROJECT_ROOT / "data" / "raw" / "processed"
COLLECTOR_DIR = PROJECT_ROOT / "data" / "collector_poc"

# 与 p6_4_apply_persisted_dir.SAFE_COLS 保持一致（apply 端做安全子集）
SAFE_COLS = [
    "date", "underlying_symbol", "open", "high", "low", "close",
    "volume", "open_interest",
]

# 换月疑点守卫阈值：日价格跳变（按品种近似限幅放宽 2%）
PRICE_JUMP_PCT: dict[str, float] = {
    "cu0": 0.12, "sc0": 0.12, "ni0": 0.12,  # 有色/能源波动大，防误报
}
PRICE_JUMP_DEFAULT = 0.095
OI_JUMP_PCT = 0.45          # OI 单日跳变守卫（换月换仓特征）
K_TOL = 5e-4                # k 段内常数容差（相对）
RAW_ALIGN_TOL = 0.005       # tqsdk vs 湖 raw_close 对齐容差（相对 0.5%）
MIN_OVERLAP = 3             # k 实测最少对齐稳定日


def _load_lake_window(sym0: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """读主湖 <sym0>/1d/{year}.parquet 的窗口段（不存在返回空帧）。"""
    frames = []
    for year in range(start.year, end.year + 1):
        pf = PROCESSED_DIR / sym0 / "1d" / f"{year}.parquet"
        if not pf.exists():
            continue
        df = pd.read_parquet(pf)
        if "datetime" not in df.columns or df.empty:
            continue
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="datetime", keep="last")
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)].sort_values("datetime")


def _load_collector_daily(sym0: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """读采集湖 <sym0>/1d/*.parquet 的窗口段（采集服务联测后的日线分区）。"""
    day_dir = COLLECTOR_DIR / sym0 / "1d"
    if not day_dir.is_dir():
        return pd.DataFrame()
    frames = []
    for pf in sorted(day_dir.glob("*.parquet")):
        try:
            df = pd.read_parquet(pf)
        except Exception:  # noqa: BLE001 - 单文件损坏不阻断
            continue
        if df.empty:
            continue
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="datetime", keep="last")
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)].sort_values("datetime")


def _underlying_upper(tq_symbol: str) -> str:
    """KQ.m@SHFE.ag → AG（对齐 pandadata 样本的大写基础代码）。"""
    return tq_symbol.split("@")[1].split(".")[-1].upper()


def main() -> int:
    ap = argparse.ArgumentParser(description="P步-A 本地拉取管线（tqsdk + 主湖 k 锚定）")
    ap.add_argument("--asof", default=None, help="基准交易日 YYYY-MM-DD，默认今天")
    ap.add_argument("--window-days", type=int, default=14,
                    help="窗口日历日数（与 pandadata 拉取窗口一致，默认 14）")
    ap.add_argument("--out-dir", default=None,
                    help="落盘目录，默认 artifacts/p6_4_pull_<asofYYYYMMDD>")
    ap.add_argument("--include-today", action="store_true",
                    help="强制含 asof 当日 bar（20:30 夜盘前场景）")
    ap.add_argument("--exclude-today", action="store_true",
                    help="强制排除 asof 当日 bar（08:00 场景，当日 bar 未成型）")
    ap.add_argument("--symbols", default=None, help="逗号分隔品种子集（默认 18 全集）")
    args = ap.parse_args()

    from hexbroker.data.sources.tqsdk_source import TQ_SYMBOLS, TqsdkSource

    asof = pd.Timestamp(args.asof) if args.asof else pd.Timestamp(date.today())
    start = asof - pd.Timedelta(days=args.window_days - 1)
    end = asof
    out_dir = Path(args.out_dir) if args.out_dir else \
        PROJECT_ROOT / "artifacts" / f"p6_4_pull_{asof.strftime('%Y%m%d')}"

    symbols = sorted(TQ_SYMBOLS) if not args.symbols else \
        [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 含/排今日：显式旗标 > 自动（asof=今天且本地时间已过 15:30 → 含）
    if args.include_today:
        include_today = True
    elif args.exclude_today:
        include_today = False
    else:
        now = datetime.now()
        include_today = (asof.date() == date.today()) and \
            (now.hour > 15 or (now.hour == 15 and now.minute >= 30))

    # ---- 1) tqsdk 一次会话拉全部品种 ------------------------------------
    src = TqsdkSource()
    try:
        bf = src.fetch_bars(symbols, str(start.date()), str(end.date()), freq="1d")
    except Exception as exc:  # noqa: BLE001
        print("=" * 66)
        print(f"⛔ tqsdk 整体拉取失败：{exc}")
        print("→ 处置：调用方回退 pandadata 管线（自动化 prompt 内置兜底步骤）")
        print("=" * 66)
        return 2
    tq = bf.df.reset_index()
    tq["datetime"] = pd.to_datetime(tq["datetime"])
    tq = tq[tq["datetime"].dt.normalize() <= end]
    if not include_today:
        tq = tq[tq["datetime"].dt.normalize() < asof]
    tq = tq.sort_values(["symbol", "datetime"])

    # ---- 2) 逐品种：对齐过滤 + 接缝校验 + 落盘 ---------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    for sym0 in symbols:
        try:
            lake = _load_lake_window(sym0, start, end)
            tqs = tq[tq["symbol"] == sym0].set_index("datetime").sort_index()
            if tqs.empty:
                raise ValueError("TQSDK_EMPTY: tqsdk 窗口内无 bar")
            if lake.empty:
                raise ValueError("NO_OVERLAP: 主湖窗口内无该品种数据")
            lk = lake.set_index("datetime")
            overlap_dates = lk.index.intersection(tqs.index)
            if len(overlap_dates) < MIN_OVERLAP:
                raise ValueError(f"NO_OVERLAP: 重叠日 {len(overlap_dates)} < {MIN_OVERLAP}")

            # 逐日：ratio（湖内复权比）与 aligned（tqsdk vs 湖名义价）
            ratios, aligned = {}, {}
            for d in overlap_dates:
                raw = float(lk.loc[d, "raw_close"])
                close = float(lk.loc[d, "close"])
                if raw > 0 and close == close:
                    ratios[d] = close / raw
                    aligned[d] = abs(float(tqs.loc[d, "close"]) / raw - 1.0) <= RAW_ALIGN_TOL

            # 最新对齐稳定段：从末尾回溯（aligned 且 ratio 稳定）
            tail_dates: list = []
            k_last: float | None = None
            for d in reversed(overlap_dates):
                if d not in ratios:
                    continue
                if k_last is None:
                    if not aligned[d]:
                        continue  # 末尾未对齐日（换月领先窗口）跳过继续找
                    k_last = ratios[d]
                    tail_dates.append(d)
                    continue
                if aligned[d] and abs(ratios[d] / k_last - 1.0) <= K_TOL:
                    tail_dates.append(d)
                elif not aligned[d]:
                    continue  # 未对齐日不截断稳定段（仅从 k 段排除）
                else:
                    break   # 对齐但比值跳变 → 真换月台阶，稳定段到此为止
            tail_dates = sorted(tail_dates)
            if len(tail_dates) < MIN_OVERLAP:
                raise ValueError(
                    f"SCALE_UNSTABLE: 对齐稳定重叠日仅 {len(tail_dates)} < {MIN_OVERLAP}")
            k = sum(ratios[d] for d in tail_dates) / len(tail_dates)
            k_dev = max(abs(ratios[d] / k - 1.0) for d in tail_dates)

            # 接缝校验：两源共同最后日必须对齐（否则扩展基准不可信）。
            # 湖比 tqsdk 新（asof 当日 bar 被 --exclude-today 排除等场景）属正常，
            # 接缝取 min(lake_last, tqs_last)；扩展只发生在 tqsdk 有湖后数据时。
            lake_last = lk.index.max()
            tqs_last = tqs.index.max()
            seam_date = min(lake_last, tqs_last)
            if seam_date not in set(tail_dates):
                raise ValueError(
                    f"SEAM_BASIS_CONFLICT: 接缝日 {seam_date.date()} 与 tqsdk 名义价"
                    f"不对齐（主连换月时点分歧窗口）→ 拒绝扩展，回退 pandadata")

            # 扩展区换月疑点守卫
            ext = tqs[tqs.index > lake_last]
            ext_source = "tqsdk"
            ext_rows: pd.DataFrame | None = ext
            if not ext.empty:
                col = _load_collector_daily(sym0, start, end).set_index("datetime")
                col_ext = col[col.index > lake_last]
                if not col_ext.empty:
                    ext_source = "collector"
                    ext_rows = col_ext.reindex(ext.index).combine_first(
                        ext[~ext.index.isin(col_ext.index)])
                prev_close = tqs["close"].shift(1)
                ret = (ext_rows["close"] / prev_close.loc[ext_rows.index] - 1.0).abs()
                jump_lim = PRICE_JUMP_PCT.get(sym0, PRICE_JUMP_DEFAULT)
                if bool((ret > jump_lim * 1.02).any()):
                    raise ValueError(f"ROLLOVER_SUSPECT: 扩展区 {ret.idxmax().date()} 价格跳变 "
                                     f"{ret.max():.2%} > 限幅{jump_lim:.1%}×1.02（疑换月，k 不可外推）")
                oi = ext_rows["open_interest"]
                oi_prev = tqs["open_interest"].shift(1).reindex(ext_rows.index)
                oi_ret = ((oi - oi_prev) / oi_prev.clip(lower=1.0)).abs()
                if bool((oi_ret > OI_JUMP_PCT).any()):
                    raise ValueError(f"ROLLOVER_SUSPECT: 扩展区 OI 单日跳变超 {OI_JUMP_PCT:.0%}"
                                     f"（疑换月，k 不可外推）")

            # 发射：扩展日 + 对齐稳定段重叠日（未对齐日绝不发射）
            base = _underlying_upper(TQ_SYMBOLS[sym0])
            emit_dates = sorted(set(tail_dates) | set(ext_rows.index))
            rows: list[list] = []
            for dt in emit_dates:
                src_row = ext_rows.loc[dt] if dt in ext_rows.index else tqs.loc[dt]
                rows.append([
                    dt.strftime("%Y%m%d"), base,
                    round(float(src_row["open"]) * k, 6), round(float(src_row["high"]) * k, 6),
                    round(float(src_row["low"]) * k, 6), round(float(src_row["close"]) * k, 6),
                    float(src_row["volume"]), float(src_row["open_interest"]),
                ])
            payload = {"result": {"type": "dataframe", "columns": SAFE_COLS, "rows": rows}}
            (out_dir / f"{sym0}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            ok.append(sym0)
            n_conflict = len(overlap_dates) - len([d for d in overlap_dates if d in set(tail_dates)])
            report.append(
                f"| {sym0} | ✅ | {k:.6f} | {len(tail_dates)} | {k_dev:.1e} | "
                f"{len(ext_rows)} | {ext_source} | {n_conflict} | "
                f"{rows[0][0]}~{rows[-1][0]} |")
        except ValueError as exc:
            failed.append((sym0, str(exc)))
            report.append(f"| {sym0} | ⛔ | - | - | - | - | - | - | {exc} |")
        except Exception as exc:  # noqa: BLE001
            failed.append((sym0, f"UNEXPECTED: {exc}"))
            report.append(f"| {sym0} | ⛔ | - | - | - | - | - | - | UNEXPECTED: {exc} |")

    # ---- 3) 报告 --------------------------------------------------------
    lines = [
        f"# 本地拉取管线报告（asof={asof.date()}，窗口 {start.date()}~{end.date()}，"
        f"today_bar={'含' if include_today else '排'}）",
        "",
        f"- 拉取通道：tqsdk_source（{bf.metadata.get('fetch_seconds', '?')}s）"
        f"+ 主湖 k 锚定 + 采集湖优先扩展",
        f"- 结果：成功 {len(ok)} / 失败 {len(failed)}（共 {len(symbols)}）",
        f"- 落盘目录：{out_dir}",
        "",
        "| 品种 | 状态 | k | 对齐稳定日 | k波动 | 扩展行 | 扩展源 | 基准冲突日 | 发射区间 |",
        "|---|---|---|---|---|---|---|---|---|",
        *report,
    ]
    if failed:
        lines += [
            "",
            "## ⛔ 失败明细（调用方须回退 pandadata，禁止部分落盘当成功）",
            "",
            *[f"- **{s}**: {reason}" for s, reason in failed],
        ]
    (out_dir / "_LOCAL_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(main())
