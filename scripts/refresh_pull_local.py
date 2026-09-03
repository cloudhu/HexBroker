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

接缝判定 v3（Part A+B，2026-09-02 主理人裁决实施）
--------------------------------------------------
旧版把「接缝日 ∉ 对齐稳定段」一律硬拒（SEAM_BASIS_CONFLICT 钝器），
换月日 tqsdk 领先窗口（官方保守切约、湖已先切新主力）被系统性误杀，
18/18 回退 pandadata —— 架空本脚本「去 pandadata 单点依赖」的立项目标
（实证：2026-08-31 rb0、2026-09-01 全品种、2026-09-02 rb0 连续三晨）。
现按接缝三分判定（``_seam_decision``）：

- 接缝日 ∈ 稳定段 → ``OK``（正常路径，不变）；
- 接缝日 ∉ 稳定段 且 **无扩展区**（ext 空 = 湖不滞后于 tqsdk）→ 换月
  领先窗口 → **软放行** ``ROLLOVER_LEAD_WINDOW``。前提 fail-closed：
  湖必须确实含有接缝日行，缺失即硬拒（绝不静默跳过数据）；
- 接缝日 ∉ 稳定段 且 **存在扩展区** → 湖滞后、扩展基准不可信 →
  **仍硬拒**（安全护栏不削弱）。

每个品种的接缝状态额外落盘 ``_SEAM_STATUS.json``（机器可读），编排层
（Part B）据此对领先窗口品种做 pandadata **定向**补数——把「换月日全量
18 品种回退」降为「仅领先窗口品种接缝日校正」，且退出码=0 时不再触发
全量兜底。

幂等性：对齐日重发值与湖偏差 ~1e-10，远低于 p6_4 重叠容差 1%；
扩展日为湖中不存在的新日期，merge 即纯追加。

Tier-2 备源通道（2026-09-03 P0 事故修复）
----------------------------------------
死锁现场：主连换月窗口内 tqsdk 与湖的名义价分歧（rb0 实测 08-31/09-01 偏差
-1.69%/-1.61%）→ 对齐稳定段被击穿 → 主路判 ``SCALE_UNSTABLE`` /
``SEAM_BASIS_CONFLICT`` → 该品种**永不前进**；而湖不前进，稳定段就永远长
不出 ``MIN_OVERLAP`` 天 → **主路自愈不了**。又因 pandadata MCP 未接线
（``~/.workbuddy/mcp.json`` = ``{"mcpServers": {}}``），兜底是死路 —— 全线
18 品种停摆。

本通道的处置：主路判定失败的品种**不再原地打转**，改走
:mod:`hexbroker.data.failover` 早已定义的 **Tier 2 备源**——

1. :class:`~hexbroker.data.backup.BackupRawFetcher(sources=("sina","akshare"),
   save=False)` 拉**名义价**（``save=False`` 是硬性要求：备源绝不污染主湖，
   否则主源恢复后分不清哪些是权威数据）；
2. :func:`~hexbroker.data.graft.graft_adjusted` 把名义价**续接**到湖内既有
   后复权序列上。⛔ **严禁自己乘 k** —— 换月时 k 会跳变，手工外推必错；
3. 成功即按主路**同格式**落盘 ``<sym0>.json``，``_SEAM_STATUS.json`` 记
   ``BACKUP_SINA``，报告「来源」列标 ``备源``；
4. 备源失败（拉取失败 / 无锚点 / 无新增日 / 锚点陈旧 / 接缝跨源断裂 /
   缺 OHLC）→ 该品种**仍计失败**（退出码 3），绝不静默成功。

⚠️ 备源是**新增的第二数据源**，不是放宽主路判定：
``RAW_ALIGN_TOL`` / ``K_TOL`` / ``MIN_OVERLAP`` 一个字都不动。

用法
----
  # 生产（交易日 08:00，当日 bar 未成型 → 排除今日）
  python scripts/refresh_pull_local.py --exclude-today
  # 生产（交易日 20:30；默认自动：asof=今天且已过 15:30 → 含今日）
  python scripts/refresh_pull_local.py
  # 验证（周日对质周五 pandadata 样本，写独立测试目录）
  python scripts/refresh_pull_local.py --asof 2026-08-28 \
      --out-dir artifacts/p6_4_pull_20260828_localtest

退出码：0 全部品种落盘（主路或备源任一成功） | 2 tqsdk 整体拉取失败 |
3 仍有品种主备皆失败（明细见 ``_LOCAL_REPORT.md``，调用方须整体回退
pandadata，禁止部分成功）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
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

# ---- Tier-2 备源通道常量（2026-09-03 P0） --------------------------------
# 顺序即优先级。新浪与 akshare 是**同一上游**，此处只作解析层冗余（防接口
# 格式变更），不构成上游冗余 —— 真正的上游冗余只能由交易所官方源提供。
BACKUP_SOURCES: tuple[str, ...] = ("sina", "akshare")
BACKUP_LOOKBACK_DAYS = 60       # 备源窗口向前扩展天数（保证与湖有重叠锚点）
BACKUP_MAX_GRAFT_DAYS = 10      # 单品种单次续接天数上限（备源只应急救短窗口）
BACKUP_MAX_ANCHOR_GAP_DAYS = 7  # 锚点日与首个续接日最大间隔（防陈旧锚点外推）
BACKUP_STATUS_PREFIX = "BACKUP_"  # _SEAM_STATUS.json 状态名前缀（→ BACKUP_SINA）
BACKUP_SOURCE_TAG = "backup"      # 报告「扩展源/来源」列取值


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


def _augment_ext_with_collector(
    sym0: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    ext: pd.DataFrame,
    lake_last: pd.Timestamp,
) -> tuple[str, pd.DataFrame]:
    """用采集湖 POC 日线增强扩展区（tqsdk 扩展日之后、湖最后日之前）。

    返回 ``(ext_source, ext_rows)``。采集湖 POC 无日线分区/空帧（无 ``datetime`` 列）
    时安全回落 tqsdk（``ext_source='tqsdk'``、``ext_rows=ext``），不会因 ``.set_index``
    抛 ``KeyError`` 导致全品种失败（见 2026-08-31 夜盘前刷新 18/18 失败根因）。
    """
    ext_source = "tqsdk"
    ext_rows: pd.DataFrame = ext
    if ext.empty:
        return ext_source, ext_rows
    col = _load_collector_daily(sym0, start, end)
    if "datetime" not in col.columns:
        return ext_source, ext_rows  # 采集湖 POC 无日线 → 回落 tqsdk
    col = col.set_index("datetime")
    col_ext = col[col.index > lake_last]
    if not col_ext.empty:
        ext_source = "collector"
        ext_rows = col_ext.reindex(ext.index).combine_first(
            ext[~ext.index.isin(col_ext.index)])
    return ext_source, ext_rows


def _underlying_upper(tq_symbol: str) -> str:
    """KQ.m@SHFE.ag → AG（对齐 pandadata 样本的大写基础代码）。"""
    return tq_symbol.split("@")[1].split(".")[-1].upper()


def _base_symbol(sym0: str, tq_symbols: dict | None = None) -> str:
    """``sym0`` → 大写基础合约代码（RB/HC/AG…），对齐 pandadata 样本口径。

    ``tq_symbols`` 缺失该品种时按 ``sym0`` 去尾 ``0`` 兜底（``rb0`` → ``RB``），
    保证备源通道不会因符号表缺项而整批失败（R22：不引入新的全停失效模式）。
    """
    tq = (tq_symbols or {}).get(sym0)
    if tq:
        return _underlying_upper(tq)
    return (sym0[:-1] if sym0.endswith("0") and len(sym0) > 1 else sym0).upper()


def _atomic_write_text(path: Path, text: str) -> None:
    """G5 原子写：``tmp + os.replace``（2026-09-03 P0 补齐）。

    ⛔ 绝不删除：沙箱 safe-delete 钩子会拦截 ``unlink``/``remove`` 并路由至
    回收站（项目已因此丢过生产文件）。写盘失败时原档未被 ``os.replace``
    触碰、tmp 保留供排查 —— 孤儿 tmp 为 ``mkstemp`` 随机名，不会被按扩展名
    的数据扫描命中。

    与 :func:`hexbroker.utils.io._atomic_write` 语义一致（mkstemp + os.close
    + 写 + os.replace）。此处**刻意**保留本地实现而非导入：本脚本一贯保持
    **零顶层 hexbroker 依赖**（主源模块全部惰性导入），顶层引入会让
    ``hexbroker`` 导入失败从「单品种降级」升级为「整脚本崩溃」—— 违反
    R22（不引入新的全停失效模式）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, str(path))


def _backup_load_anchor(sym0: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """备源续接锚点：湖内既有 ``close``（后复权）+ ``raw_close``（名义价）。

    窗口向前扩展 ``BACKUP_LOOKBACK_DAYS`` 天 —— graft 依赖 raw 与湖 adj 的
    **重叠日期**定锚点，按原窗口请求会无交集而必然失败（与
    ``FailoverOrchestrator._overlap_start`` 同机理）。

    返回空帧表示**无锚点**（调用方须按红线拒绝，绝不自造后复权）。
    """
    bk_start = start - pd.Timedelta(days=BACKUP_LOOKBACK_DAYS)
    lake = _load_lake_window(sym0, bk_start, end)
    if lake.empty or "close" not in lake.columns:
        return pd.DataFrame()
    keep = [c for c in ("close", "raw_close") if c in lake.columns]
    df = lake.set_index("datetime")[keep].astype(float).copy()
    df.index = pd.to_datetime(df.index).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if "raw_close" not in df.columns:
        df["raw_close"] = float("nan")
    return df[(df["close"] > 0) & (df["close"] == df["close"])]


def _backup_pull_symbol(
    sym0: str,
    base: str,
    start: pd.Timestamp,
    end_eff: pd.Timestamp,
    jump_lim: float,
    *,
    fetcher=None,
) -> tuple[list[list], dict]:
    """Tier-2 备源通道：免费源名义价 + ``graft_adjusted`` 续接出后复权扩展日。

    用于主路判定失败（``SCALE_UNSTABLE`` / ``SEAM_BASIS_CONFLICT`` /
    ``NO_OVERLAP`` …）的品种 —— 这些品种靠主路**自愈不了**（湖不前进则对齐
    稳定段永远长不出 ``MIN_OVERLAP`` 天），必须换数据源。

    参数
    ----
    sym0 : 品种代码（``rb0`` / ``hc0`` …）。
    base : 大写基础合约代码（``RB``），对齐 pandadata 口径。
    start : 拉取窗口起始（脚本 ``start``，非扩展后的备源起点）。
    end_eff : 发射上界（已按 ``include_today`` 处理；不含当日 bar）。
    jump_lim : 该品种价格跳变限幅（复用主路 ``PRICE_JUMP_PCT``）。
    fetcher : 注入式备源拉取器（鸭子类型：只需 ``fetch_raw``），**仅供测试**。
        None 时自建 ``BackupRawFetcher(sources=BACKUP_SOURCES, save=False)``。

    返回
    ----
    ``(rows, info)`` —— ``rows`` 为 pandadata 同口径的发射行（**仅新增日**，
    不重发重叠日：备源只负责把湖往前推，重叠日本就在湖里且逐位一致）；
    ``info`` 为报告/状态落盘用的诊断字典。

    抛出
    ----
    ValueError : 任何一环不可信即抛（fail-closed）。错误串统一 ``BACKUP_*``
        前缀，便于调用方归因与告警分级。
    """
    try:  # 惰性导入：备源模块不可用时只让该品种失败，不拖垮整批（R22）
        from hexbroker.data.backup import BackupRawFetcher
        from hexbroker.data.graft import DEFAULT_ALIGN_TOL, graft_adjusted
    except Exception as exc:  # noqa: BLE001 - 导入失败必须归因而非崩溃
        raise ValueError(
            f"BACKUP_IMPORT: 备源模块不可用：{type(exc).__name__}: {exc}") from exc

    if fetcher is None:
        # save=False 是硬性要求：备源数据绝不污染主湖（否则主源恢复后分不清
        # 哪些是权威数据）。cross_check=False：sina/akshare 同一上游，交叉
        # 校验只能发现解析层分叉，却要多一次网络往返 —— 不划算。
        fetcher = BackupRawFetcher(
            sources=BACKUP_SOURCES, save=False, cross_check=False)
    bk_start = start - pd.Timedelta(days=BACKUP_LOOKBACK_DAYS)

    # ---- 1) 备源名义价 ---------------------------------------------------
    try:
        pulls = fetcher.fetch_raw([sym0], str(bk_start.date()), str(end_eff.date()))
    except Exception as exc:  # noqa: BLE001 - 第三方源异常类型不可控
        raise ValueError(
            f"BACKUP_FETCH: 备源（{'+'.join(BACKUP_SOURCES)}）拉取失败："
            f"{type(exc).__name__}: {exc}") from exc
    pull = (pulls or {}).get(sym0)
    if pull is None or getattr(pull, "close", None) is None:
        raise ValueError("BACKUP_EMPTY: 备源未返回该品种名义价")

    raw = pd.Series(pull.close).astype(float)
    raw.index = pd.to_datetime(raw.index).normalize()
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    raw = raw[(raw.index >= bk_start) & (raw.index <= end_eff)]
    raw = raw[(raw > 0) & (raw == raw)]
    if raw.empty:
        raise ValueError("BACKUP_EMPTY: 备源名义价在窗口内无有效正价格")

    # ---- 2) 完整 OHLC（缺列会写出零价 bar，绝不降级） ---------------------
    frame = getattr(pull, "frame", None)
    if frame is None or frame.empty:
        raise ValueError(
            "BACKUP_NO_OHLC: 备源未返回 OHLC 原始帧 —— 融合端 coerce_schema 对"
            "缺失浮点列一律 fillna(0.0)，只给 close 会写出 open/high/low=0 的"
            "零价 bar（红线：宁可不补，不可写错口径）")
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    missing_cols = [c for c in ("open", "high", "low", "close") if c not in frame.columns]
    if missing_cols:
        raise ValueError(f"BACKUP_NO_OHLC: 备源帧缺少列 {missing_cols}")

    # ---- 3) 锚点（湖内既有后复权序列） -----------------------------------
    anchor = _backup_load_anchor(sym0, start, end_eff)
    if anchor.empty:
        raise ValueError(
            "BACKUP_NO_ANCHOR: 湖内无该品种既有后复权序列，备源名义价无法续接"
            "（红线：备源绝不自造后复权）")
    adj_hist = anchor["close"]

    # ---- 4) 续接（⛔ 严禁自己乘 k —— 换月时 k 会跳变） --------------------
    try:
        res = graft_adjusted(
            adj_hist, raw,
            max_graft_days=BACKUP_MAX_GRAFT_DAYS,
            lookback=BACKUP_LOOKBACK_DAYS,
            tol=DEFAULT_ALIGN_TOL,
        )
    except Exception as exc:  # noqa: BLE001 - HexDataError 等统一归因
        raise ValueError(
            f"BACKUP_GRAFT: 续接失败：{type(exc).__name__}: {exc}") from exc

    align = res.alignment if isinstance(res.alignment, dict) else {}
    n_align = int(align.get("n", 0) or 0)
    if n_align < MIN_OVERLAP:
        raise ValueError(
            f"BACKUP_ALIGN_INSUFFICIENT: 备源与湖重叠样本 {n_align} < "
            f"{MIN_OVERLAP}（锚点不可信，拒绝续接）")
    if not res.new_dates:
        raise ValueError("BACKUP_NO_NEW_DATES: 备源无湖内缺失的新日期（湖已覆盖）")

    new_dates = [pd.Timestamp(d) for d in res.new_dates]
    t0 = pd.Timestamp(res.anchor_date)
    # 锚点必须落在**湖的最末一根 bar** 上（QA R27 fresh-eyes 加护栏，2026-09-03）。
    #
    # graft 的 ``t0 = common.max()`` 只是「两源重叠的最新日」。若备源在湖末日
    # 有缺口（sina/akshare 单日缺数很常见），t0 会**退到湖末日之前**——而换月
    # 恰好发生在湖末日时，k0 取到的就是**旧段**复权比，外推出的新 bar 相对正确
    # 值整段偏移（rb0 量级实测 +1.72%，即 p43 cu0/ni0 定罪的幽灵台阶复活）。
    #
    # ⚠️ 现有护栏全部接不住：n_align 只看重叠样本数（重叠区全在旧段 → 比值恒定、
    # graft 零告警）；anchor_gap_days 只限间隔（跨一次换月只需 2 天，远小于阈值
    # BACKUP_MAX_ANCHOR_GAP_DAYS=7）；接缝名义价校验只查名义价连续性，查不出
    # 复权比跳段。故必须显式要求「锚点 = 湖末日」。
    lake_last = pd.Timestamp(anchor.index.max())
    if t0 != lake_last:
        raise ValueError(
            f"BACKUP_ANCHOR_NOT_LATEST: 备源在湖末日 {lake_last.date()} 无数据，"
            f"锚点退到 {t0.date()} —— 该日复权比不保证属于当前复权段（换月窗口内"
            f"会取到旧段 k，写出幽灵台阶）；备源必须覆盖湖末日，否则拒绝续接")
    gap_days = int((new_dates[0] - t0).days)
    if gap_days > BACKUP_MAX_ANCHOR_GAP_DAYS:
        raise ValueError(
            f"BACKUP_ANCHOR_STALE: 锚点日 {t0.date()} 与首个续接日 "
            f"{new_dates[0].date()} 间隔 {gap_days} 天 > "
            f"{BACKUP_MAX_ANCHOR_GAP_DAYS} 天（陈旧锚点外推不可信）")

    # ---- 5) 接缝跨源名义价连续性（复用主路 P-NEW 护栏，同口径） -----------
    # 备源若在续接段换了主力而湖未换，k 锚即失效 → 名义价必然在接缝处跳空。
    # 口径不可用（湖无 raw_close 列）时该护栏 fail-open 放行并留痕（R22）。
    if t0 in set(anchor.index):
        seam_raw = float(anchor.loc[t0, "raw_close"])
    else:
        seam_raw = float(raw.loc[t0])
    break_err = _seam_nominal_break(
        seam_raw, float(raw.loc[new_dates[0]]), new_dates[0], jump_lim)
    if break_err:
        raise ValueError(f"BACKUP_{break_err}")

    # ---- 6) 发射（仅新增日；k 由 graft 结果反解，不手工外推） -------------
    # k_d = grafted_close / raw_close 精确还原 graft 的分段因子（换月时会自动
    # 跳段，这正是「严禁自乘 k」的价值所在）。OHLC 按同一 k_d 缩放，保证
    # open<=close<=high 的包络关系不被破坏。
    # 取值校验（QA R27 fresh-eyes 加护栏，2026-09-03）：现有 ``BACKUP_NO_OHLC``
    # 只查**列是否存在**，查不出**值是否可用**。而 sina/akshare 缺字段有两种
    # 常见编码：``NaN`` 与 ``0.0``。二者都会被融合端
    # ``p6_4_fill_gaps.coerce_schema`` 的 ``fillna(0.0)`` 落成 0.0 ——
    # 即工程师在 ``RawPull.frame`` 注释里亲手定为红线的「open/high/low=0 零价
    # bar」，只是换了条路进来（缺列被挡了，NaN/0 值没挡）。
    # 另：``json.dumps`` 默认 ``allow_nan=True``，NaN 会写成非标准字面量 ``NaN``
    # （严格 JSON 解析器直接拒绝，Python 解析器接受后再被 coerce_schema 填 0）。
    rows: list[list] = []
    for d in new_dates:
        if d not in set(frame.index):
            raise ValueError(f"BACKUP_NO_OHLC: 备源帧缺少 {d.date()} 的 OHLC 行")
        o_v = float(frame.loc[d, "open"])
        h_v = float(frame.loc[d, "high"])
        l_v = float(frame.loc[d, "low"])
        raw_v = float(raw.loc[d])
        for _name, _v in (("open", o_v), ("high", h_v), ("low", l_v)):
            if _v != _v or _v <= 0.0:  # NaN 或 非正
                raise ValueError(
                    f"BACKUP_BAD_OHLC: 备源 {d.date()} 的 {_name}={_v!r} 非有限正值"
                    f" —— 融合端 coerce_schema 会 fillna(0.0) 成零价 bar，"
                    f"宁可不补（红线：不可写错口径）")
        k_d = float(res.series.loc[d]) / raw_v
        vol = float(frame.loc[d, "volume"]) if "volume" in frame.columns else 0.0
        oi = float(frame.loc[d, "open_interest"]) if "open_interest" in frame.columns else 0.0
        for _name, _v in (("volume", vol), ("open_interest", oi)):
            if _v != _v or _v < 0.0:  # NaN 或 负
                raise ValueError(
                    f"BACKUP_BAD_OHLC: 备源 {d.date()} 的 {_name}={_v!r} 非法"
                    f"（NaN/负数，会污染主湖）")

        # 包络自洽（名义价口径）：low ≤ min(open, close) 且 high ≥ max(open, close)。
        # 同 k_d 缩放（k_d > 0）本身保序，但保不住**备源自带的脏数据**——
        # 脏 bar 一旦落湖，下游指标（ATR/布林/最高最低）会静默消化。
        if not (l_v <= min(o_v, raw_v) and h_v >= max(o_v, raw_v)):
            raise ValueError(
                f"BACKUP_OHLC_INCOHERENT: 备源 {d.date()} 包络破坏 "
                f"low={l_v} open={o_v} high={h_v} close={raw_v}（名义价口径）"
                f" —— 拒绝落盘")
        rows.append([
            d.strftime("%Y%m%d"), base,
            round(o_v * k_d, 6),
            round(h_v * k_d, 6),
            round(l_v * k_d, 6),
            round(raw_v * k_d, 6),
            vol, oi,
        ])

    source_name = str(getattr(pull, "source", "") or BACKUP_SOURCE_TAG)
    info = {
        "source": source_name,
        "k": float(res.anchor_ratio),
        "anchor_date": str(t0.date()),
        "anchor_gap_days": gap_days,
        "new_dates": [d.strftime("%Y%m%d") for d in new_dates],
        "n_align": n_align,
        # 续接告警（重叠区比值非恒定 = 窗口内有换月）**不阻断**但必须留痕：
        # rb0/hc0 的换月日天然落在重叠区内，硬卡会让备源永远救不了场。
        "warnings": [str(w) for w in (res.warnings or [])],
        "seam_status": f"{BACKUP_STATUS_PREFIX}{source_name.upper()}",
    }
    return rows, info


def _seam_decision(
    seam_date: pd.Timestamp,
    *,
    tail_dates: set,
    ext_empty: bool,
    lake_has_seam: bool,
) -> tuple[str, str | None]:
    """接缝三分判定（Part A，2026-09-02）：返回 ``(seam_status, 硬拒原因)``。

    - 接缝日 ∈ 对齐稳定段 → ``OK``（正常路径）；
    - 接缝日 ∉ 稳定段 且 **无扩展区**（ext 空 = 湖不滞后于 tqsdk）→
      换月领先窗口（tqsdk 官方保守切约、湖已先切新主力）→ 软放行
      ``ROLLOVER_LEAD_WINDOW``。前提 fail-closed：湖必须**确实含有**
      接缝日行，缺失即硬拒（绝不静默跳过数据）；
    - 接缝日 ∉ 稳定段 且 **存在扩展区** → 湖滞后且扩展基准不可信，
      用 k 锚定扩展会写出错口径 bar → 硬拒，回退 pandadata。
    """
    if seam_date in tail_dates:
        return "OK", None
    if ext_empty:
        if not lake_has_seam:
            return "", (
                f"SEAM_BASIS_CONFLICT: 接缝日 {seam_date.date()} 湖内缺失行"
                f"（领先窗口软放行前提不成立）→ fail-closed 拒绝，回退 pandadata")
        return "ROLLOVER_LEAD_WINDOW", None
    return "", (
        f"SEAM_BASIS_CONFLICT: 接缝日 {seam_date.date()} 与 tqsdk 名义价"
        f"不对齐且存在扩展区（主连换月时点分歧窗口）→ 拒绝扩展，回退 pandadata")


def _seam_nominal_break(
    seam_raw_close: float,
    ext_first_close: float,
    ext_first_date,
    jump_lim: float,
) -> str | None:
    """P-NEW 防再发护栏（2026-09-03）：接缝**跨源**名义价连续性检查。

    「``seam∈tail`` + ``ext`` 非空」是 p43 定罪的幽灵排放路径（cu0/ni0 旧约
    bar × 当时 k 锚 → ``k = adj_close / raw_close`` 幽灵台阶）：现有
    ``ROLLOVER_SUSPECT`` 守卫只查 tqs 系列内部连续性（``shift(1)`` 同源对比），
    接不住**跨源**断裂——湖内接缝日 ``raw_close`` 是 sina 权威名义价，tqs
    扩展区若仍报旧约，二者在接缝处必然偏离超限。超限即拒绝（该品种失败 →
    调用方回退 pandadata，禁止部分落盘当成功）。

    fail-open 语义（新增护栏设计原则 R22：宁可少一道护栏，不可全停）：
    口径不可用（seam_raw ≤ 0 / NaN / 非数值）→ 返回 None 放行，退回既有
    ROLLOVER_SUSPECT（价格/OI 跳变）守卫把关。

    返回 ``None`` = 通过；返回错误串 = 调用方 ``raise ValueError(err)``。
    """
    try:
        seam_raw = float(seam_raw_close)
        first_close = float(ext_first_close)
    except (TypeError, ValueError):
        return None
    if (
        seam_raw != seam_raw or first_close != first_close  # NaN
        or seam_raw <= 0 or first_close <= 0
    ):
        return None
    dev = abs(first_close / seam_raw - 1.0)
    if dev > jump_lim * 1.02:
        return (
            f"SEAM_NOMINAL_BREAK: 扩展区首日 {ext_first_date} 名义价 "
            f"{first_close:.2f} 与湖接缝日 raw_close {seam_raw:.2f} 偏差 "
            f"{dev:.2%} > 限幅 {jump_lim:.1%}×1.02（接缝跨源断裂，疑旧约×k锚"
            f"幽灵排放，P-NEW 防再发护栏）→ 拒绝扩展，回退 pandadata"
        )
    return None


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

    # 备源发射上界：与主路同口径（--exclude-today 时当日 bar 未成型，绝不发射）
    emit_end = end if include_today else end - pd.Timedelta(days=1)

    # ---- 2) 逐品种：对齐过滤 + 接缝校验 + 落盘 ---------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    seam_status_map: dict[str, str] = {}
    backup_detail: dict[str, dict] = {}

    for sym0 in symbols:
        primary_err = ""
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

            # 接缝校验 + 换月领先窗口识别（Part A，2026-09-02 裁决实施）。
            # 湖比 tqsdk 新（asof 当日 bar 被 --exclude-today 排除等场景）属正常，
            # 接缝取 min(lake_last, tqs_last)；扩展只发生在 tqsdk 有湖后数据时。
            lake_last = lk.index.max()
            tqs_last = tqs.index.max()
            seam_date = min(lake_last, tqs_last)
            ext = tqs[tqs.index > lake_last]          # 扩展区（仅湖滞后时非空）
            seam_status, seam_err = _seam_decision(
                seam_date,
                tail_dates=set(tail_dates),
                ext_empty=ext.empty,
                lake_has_seam=seam_date in set(lk.index),
            )
            if seam_err:
                raise ValueError(seam_err)

            # 扩展区换月疑点守卫
            ext_source, ext_rows = _augment_ext_with_collector(sym0, start, end, ext, lake_last)
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

            # P-NEW 防再发护栏（2026-09-03）：接缝跨源名义价连续性——
            # 「seam∈tail + ext 非空」排放路径（cu0/ni0 幽灵台阶事故）的专用守卫。
            seam_raw_guard = (
                float(lk.loc[seam_date, "raw_close"])
                if seam_date in lk.index else float("nan")
            )
            if not ext_rows.empty:
                break_err = _seam_nominal_break(
                    seam_raw_guard,
                    ext_rows["close"].iloc[0],
                    ext_rows.index[0],
                    jump_lim,
                )
                if break_err:
                    raise ValueError(break_err)

            # 发射：扩展日 + 对齐稳定段重叠日（未对齐日绝不发射）
            base = _base_symbol(sym0, TQ_SYMBOLS)
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
            _atomic_write_text(out_dir / f"{sym0}.json",
                               json.dumps(payload, ensure_ascii=False))
            ok.append(sym0)
            seam_status_map[sym0] = seam_status
            n_conflict = len(overlap_dates) - len([d for d in overlap_dates if d in set(tail_dates)])
            report.append(
                f"| {sym0} | ✅ | {k:.6f} | {len(tail_dates)} | {k_dev:.1e} | "
                f"{len(ext_rows)} | {ext_source} | {n_conflict} | "
                f"{rows[0][0]}~{rows[-1][0]} | {seam_status} | 主路 tqsdk |")
        except ValueError as exc:
            primary_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            primary_err = f"UNEXPECTED: {exc}"

        if not primary_err:
            continue

        # ---- 2b) Tier-2 备源通道（2026-09-03 P0）：主路失败 → 换数据源 -----
        # 主路失败品种靠主路自愈不了（湖不前进 → 对齐稳定段永远长不出
        # MIN_OVERLAP 天），故改走 sina/akshare 名义价 + graft 续接。
        # ⛔ 备源是**新增的第二数据源**，不是放宽主路判定 —— RAW_ALIGN_TOL /
        # K_TOL / MIN_OVERLAP 一个字都不动。
        # ⚠️ 落盘**必须**包在 try 内（QA R27 fresh-eyes 修正）：原实现把
        # ``json.dumps`` / ``_atomic_write_text`` 留在 try 之外，与主路分支不对称
        # —— 主路的落盘在 try 内（异常只让该品种失败），备源分支却会让序列化/
        # 写盘异常**穿透 main()**，导致整脚本崩溃、18 品种全停（违反 R22：
        # 不引入新的全停失效模式）。
        try:
            bk_rows, bk_info = _backup_pull_symbol(
                sym0,
                _base_symbol(sym0, TQ_SYMBOLS),
                start,
                emit_end,
                PRICE_JUMP_PCT.get(sym0, PRICE_JUMP_DEFAULT),
            )
            payload = {"result": {"type": "dataframe",
                                  "columns": SAFE_COLS, "rows": bk_rows}}
            # allow_nan=False：纵深防御。即便上游校验漏掉某个 NaN，也让落盘
            # **响亮失败**（计该品种失败 → 退出码 3），而不是写出含非标准
            # ``NaN`` 字面量的 json —— 后者会被下游严格解析器整批拒收，或被
            # Python 解析器接受后由 coerce_schema fillna(0.0) 填成零价 bar。
            _atomic_write_text(
                out_dir / f"{sym0}.json",
                json.dumps(payload, ensure_ascii=False, allow_nan=False))
        except ValueError as exc:
            failed.append((sym0, f"{primary_err} → 备源亦失败：{exc}"))
            report.append(
                f"| {sym0} | ⛔ | - | - | - | - | - | - | {primary_err} | - | "
                f"主路 tqsdk / 备源失败：{exc} |")
            continue
        except Exception as exc:  # noqa: BLE001
            failed.append((sym0, f"{primary_err} → 备源异常：UNEXPECTED: {exc}"))
            report.append(
                f"| {sym0} | ⛔ | - | - | - | - | - | - | {primary_err} | - | "
                f"主路 tqsdk / 备源异常：UNEXPECTED: {exc} |")
            continue
        ok.append(sym0)
        seam_status_map[sym0] = bk_info["seam_status"]
        backup_detail[sym0] = bk_info
        report.append(
            f"| {sym0} | ✅备源 | {bk_info['k']:.6f} | - | - | {len(bk_rows)} | "
            f"{BACKUP_SOURCE_TAG}:{bk_info['source']} | - | "
            f"{bk_rows[0][0]}~{bk_rows[-1][0]} | {bk_info['seam_status']} | "
            f"备源 {bk_info['source']}（主路失败：{primary_err}） |")

    # ---- 3) 报告 --------------------------------------------------------
    lines = [
        f"# 本地拉取管线报告（asof={asof.date()}，窗口 {start.date()}~{end.date()}，"
        f"today_bar={'含' if include_today else '排'}）",
        "",
        f"- 拉取通道：tqsdk_source（{bf.metadata.get('fetch_seconds', '?')}s）"
        f"+ 主湖 k 锚定 + 采集湖优先扩展",
        f"- 兜底通道：Tier-2 备源（{'+'.join(BACKUP_SOURCES)} 名义价 + graft 续接，"
        f"save=False 不落湖）—— 仅主路判定失败的品种触发",
        f"- 结果：成功 {len(ok)} / 失败 {len(failed)}（共 {len(symbols)}），"
        f"其中走备源 {len(backup_detail)} 个",
        f"- 落盘目录：{out_dir}",
        "",
        "| 品种 | 状态 | k | 对齐稳定日 | k波动 | 扩展行 | 扩展源 | 基准冲突日 | 发射区间 | 接缝 | 来源 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *report,
    ]
    if backup_detail:
        lines += [
            "",
            "## 🟡 备源补数明细（Tier-2，续接段为临时值）",
            "",
            "| 品种 | 来源 | 锚点日 | k | 续接日 | 重叠样本 | 告警 |",
            "|---|---|---|---|---|---|---|",
            *[
                f"| {s} | {i['source']} | {i['anchor_date']} | {i['k']:.6f} | "
                f"{', '.join(i['new_dates'])} | {i['n_align']} | "
                f"{'；'.join(i['warnings']) or '无'} |"
                for s, i in sorted(backup_detail.items())
            ],
            "",
            "> ⚠️ 续接段未经主源（pandadata）真值校验。主源恢复后**必须**用主源"
            "重建该窗口，本段不可视为权威值。",
        ]
    if failed:
        lines += [
            "",
            "## ⛔ 失败明细（主路 + Tier-2 备源**皆**失败；调用方须回退 pandadata，"
            "禁止部分落盘当成功）",
            "",
            *[f"- **{s}**: {reason}" for s, reason in failed],
        ]
    _atomic_write_text(out_dir / "_LOCAL_REPORT.md", "\n".join(lines))
    # Part B 机器可读接缝状态（编排层据此对领先窗口品种定向补数；
    # 文件名带 "_" 前缀，p6_4_apply_persisted_dir 的 glob 会排除 _*.json）
    lead_window = sorted(s for s, v in seam_status_map.items()
                         if v == "ROLLOVER_LEAD_WINDOW")
    backup_syms = sorted(backup_detail)
    _atomic_write_text(
        out_dir / "_SEAM_STATUS.json",
        json.dumps({"asof": asof.strftime("%Y-%m-%d"),
                    "statuses": seam_status_map,
                    "lead_window": lead_window,
                    # 2026-09-03 P0 新增：备源补数品种（续接段为临时值，主源
                    # 恢复后须重建）。状态名形如 BACKUP_SINA。
                    "backup": backup_syms,
                    "backup_detail": backup_detail},
                   ensure_ascii=False, indent=2))
    print("\n".join(lines))

    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(main())
