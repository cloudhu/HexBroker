#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-b 缓存内容卫生守卫：判定「信号缓存内容是否干净」——补数后校验（QA 观察项 2）。

定位（与 p6_4_backfill_guard 的分工）
----------------------------------
- ``p6_4_backfill_guard`` 管「缓存**旧不旧**」（交易日 lag vs 期望新鲜度）；
- **本脚本管「缓存内容**干净不干净**」：

  1. **脏信号日检测**（QA 观察项 1 的可持续化）—— 缓存里混入非交易日的信号行
     （生产实测：2020-10-02 / 2021-10-01 / 2022-04-04 / 2024-06-10）。
  2. **当日行完整性** —— 20:30 补数后，``paper.symbols`` 权威清单中每个品种
     是否都有当日信号行（缺品种 = 补数部分失败，引擎会对缺的品种走兜底）。
  3. **内容卫生** —— ``p_up`` 越界（不在 [0,1]）、关键列 NaN、``ts`` 不可解析。

⛔ 为什么它能缓解「脏行自证循环」而不是重演它
--------------------------------------------
QA 观察项 2 的机理：门禁用**同一份缓存**既做「交易日证据」（``_signal_days`` 喂给
``augment_calendar``）又做「被判对象」，缓存残留一行当日信号即可自证 ``fd=0`` 过闸。
**原理上门禁无法自检**。本脚本的破局点：**三项检查全部使用独立证据源**——

- 交易日证据 = 主湖并集日历（``load_trading_calendar``，来自 bar 落盘，与缓存无关）
  + 节假日表（``configs/paper.yaml``）；
- 品种清单 = ``paper.symbols``（配置权威，不是缓存的 ``symbol`` 去重）；
- **绝不把被检缓存自己的信号日喂给 ``augment_calendar``**（那才是自证循环）。

⛔ 只判定、不删改（与 p6_4 同哲学）
----------------------------------
脏行的剔除是策略决策（先例：P2-B ni0 2022-03-10 幽灵行由主理人拍板「剔除」），
本脚本**只报告**：脏行的品种/日期/行数明细进 stdout 与 ``--json``，
处置由主理人裁决或由后续脚本按裁决执行。

退出码契约（写进自动化判断逻辑）
------------------------------
- ``0``  OK —— 内容卫生全部通过
- ``2``  非交易日 —— 跳过（正常，不是失败）
- ``3``  CONFIG_ERROR —— 配置缺失/结构错误
- ``10`` NEED_ATTENTION —— 发现脏行 / 缺品种 / 坏值（明细见输出）
- ``11`` UNKNOWN —— 无法判定（缓存缺失 / 无 ts 列 / 解析失败）→ 需人工介入

用法
----
::

    python scripts/p6_5_cache_postfill_guard.py                     # 自动 asof+session
    python scripts/p6_5_cache_postfill_guard.py --session night     # 20:30 补数后
    python scripts/p6_5_cache_postfill_guard.py --json              # 供自动化解析
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hexbroker.diagnostics.signal_refresh import resolve_cache_paths  # noqa: E402
from hexbroker.paper.sessions import TradingSession  # noqa: E402
from hexbroker.paper.signals import (  # noqa: E402
    _to_date,
    augment_calendar,
    is_market_trading_day,
    load_trading_calendar,
)

# 退出码（与模块 docstring 契约一致）
EXIT_OK = 0
EXIT_NOT_TRADING_DAY = 2
EXIT_CONFIG_ERROR = 3
EXIT_NEED_ATTENTION = 10
EXIT_UNKNOWN = 11

DAY_CLOSE = time(15, 0)  # 与 signals.DAY_SESSION_CLOSE 同值；此处仅用于 session 推断


def load_paper_cfg(config_path: Path) -> Any:
    """读取 ``configs/paper.yaml`` 的 ``paper`` 段（不存在 → 抛错，由 main 转 exit 3）。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    paper = raw.get("paper")
    if not isinstance(paper, dict):
        raise ValueError(f"配置缺少 paper 段：{config_path}")
    return paper


class _AttrView:
    """让普通 dict 支持属性访问 + ``.get``（复用 ``TradingSession.from_config``）。

    与 ``p6_4_backfill_guard._AttrView`` 同款：生产侧 paper_cfg 是 omegaconf
    ``DictConfig``（属性+.get 双支持）；本脚本用 ``yaml.safe_load`` 拿到纯 dict，
    属性访问会 AttributeError。保证与引擎同口径（同一份 holidays）。
    """

    def __init__(self, data: Any) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self._data, dict):
            return self._data.get(key, default)
        return default

    def __getattr__(self, name: str) -> Any:
        try:
            data = object.__getattribute__(self, "_data")
        except AttributeError:  # pragma: no cover
            raise AttributeError(name)
        if isinstance(data, dict) and name in data:
            return data[name]
        raise AttributeError(name)


def config_symbols(paper_cfg: Any) -> List[str]:
    """``paper.symbols`` 的键 = 品种权威清单（**独立于缓存**，自证循环破局点之一）。

    ⚠️ 语义层级（2026-08-31 实测）：``paper.symbols`` 是**模拟盘交易品种**
    （3 个：ag0/c0/rb0），而信号缓存覆盖 **18 个研究品种** —— 两者不是一回事。
    「当日行完整性」按交易品种查（缺品种 = 引擎该品种走兜底）；
    「脏行检测」按整个缓存查（18 品种全量）。勿把「只报 3 个品种缺当日」
    当成工具漏检。
    """
    try:
        symbols = paper_cfg.get("symbols")
    except Exception:  # noqa: BLE001
        return []
    if isinstance(symbols, dict):
        return sorted(symbols.keys())
    return []


def _independent_calendar(ref_day: date) -> tuple:
    """生效日历（独立证据）：主湖并集 + 独立交易日判定增补当日。

    ⛔⛔ 刻意**不传** ``signal_days`` —— 被检缓存自己的信号日绝不能参与日历构造，
    否则「脏行自证循环」在本工具里原样复现。
    """
    return augment_calendar(load_trading_calendar(), ref_day)


def inspect_cache(
    df: pd.DataFrame,
    calendar: tuple,
    ref_day: date,
    expect_symbols: List[str],
    check_today_coverage: bool,
) -> Dict[str, Any]:
    """单缓存内容体检（纯函数，可独立测试）。

    Returns
    -------
    dict with keys:
        dirty_rows    非交易日信号行 [{symbol, day, n}]
        missing_today check_today_coverage 时，无当日行的品种
        bad_values    内容越界/NaN [{col, symbol, ts, value}]
        unknown       无法判定原因（None=可判定）
    """
    out: Dict[str, Any] = {
        "dirty_rows": [],
        "missing_today": [],
        "bad_values": [],
        "unknown": None,
    }
    if "ts" not in df.columns or df.empty:
        out["unknown"] = "无 ts 列或空表"
        return out
    try:
        ts = pd.to_datetime(df["ts"])
    except Exception as exc:  # noqa: BLE001
        out["unknown"] = f"ts 解析失败: {exc}"
        return out
    days = ts.dt.date

    cal_set = set(calendar)
    cal_max = calendar[-1] if calendar else None

    # ---- 检查 1：脏信号日（独立证据：主湖 ∪ 独立交易日判定；不喂缓存信号日）----
    day_counts = (
        pd.DataFrame({"symbol": df["symbol"], "day": days})
        .groupby(["symbol", "day"])
        .size()
        .reset_index(name="n")
    )
    for _, row in day_counts.iterrows():
        d = row["day"]
        if d > ref_day:
            # ⛔ 未来行：无论是否交易日都是污染（与 augment_calendar 截断层同语义
            # —— 未来日期不得改变 R 的判定）。必须先于 is_market_trading_day 判定，
            # 否则「> cal_max 且是交易日」会把未来行洗白（测试 test_future_day_row_flagged）。
            out["dirty_rows"].append(
                {"symbol": str(row["symbol"]), "day": d.isoformat(), "n": int(row["n"])}
            )
            continue
        if d in cal_set:
            continue  # 主湖铁证：该日有 bar
        if cal_max is not None and d > cal_max and is_market_trading_day(d):
            continue  # 当日新补：独立交易日判定通过
        out["dirty_rows"].append(
            {"symbol": str(row["symbol"]), "day": d.isoformat(), "n": int(row["n"])}
        )
    out["dirty_rows"].sort(key=lambda r: (r["day"], r["symbol"]))

    # ---- 检查 2：当日行完整性（品种清单来自配置，不是缓存去重）----
    if check_today_coverage:
        have = set(df.loc[days == ref_day, "symbol"].astype(str))
        out["missing_today"] = sorted(s for s in expect_symbols if s not in have)

    # ---- 检查 3：内容卫生 ----
    if "p_up" in df.columns:
        p_up = pd.to_numeric(df["p_up"], errors="coerce")
        bad = df.loc[p_up.isna() | (p_up < 0) | (p_up > 1)]
        for _, r in bad.iterrows():
            out["bad_values"].append(
                {
                    "col": "p_up",
                    "symbol": str(r.get("symbol")),
                    "ts": str(r.get("ts")),
                    "value": str(r.get("p_up")),
                }
            )
    for col in ("exp_ret",):
        if col in df.columns and pd.to_numeric(df[col], errors="coerce").isna().any():
            bad = df.loc[pd.to_numeric(df[col], errors="coerce").isna()]
            for _, r in bad.head(20).iterrows():  # 明细截断，防刷屏
                out["bad_values"].append(
                    {
                        "col": col,
                        "symbol": str(r.get("symbol")),
                        "ts": str(r.get("ts")),
                        "value": str(r.get(col)),
                    }
                )
    return out


def judge(paper_cfg: Any, asof: date, session: str) -> Dict[str, Any]:
    """核心判定：返回结构化结果（供 --json 输出与退出码映射）。"""
    paths = resolve_cache_paths(paper_cfg)
    symbols = config_symbols(paper_cfg)
    if not paths:
        return {"verdict": "UNKNOWN", "reason": "signal_caches 解析为空", "paths": []}
    if not symbols:
        return {
            "verdict": "UNKNOWN",
            "reason": "paper.symbols 缺失或结构错误（当日行完整性无权威清单可比）",
            "paths": paths,
        }

    try:
        calendar = _independent_calendar(asof)
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "UNKNOWN", "reason": f"主湖日历加载失败: {exc}"}

    check_today = session == "night"  # 夜盘补数后才要求当日行覆盖
    caches: List[Dict[str, Any]] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            caches.append({"name": p.name, "verdict": "UNKNOWN", "reason": "文件缺失"})
            continue
        try:
            df = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            caches.append({"name": p.name, "verdict": "UNKNOWN", "reason": f"读取失败: {exc}"})
            continue
        rep = inspect_cache(df, calendar, asof, symbols, check_today)
        if rep["unknown"]:
            caches.append({"name": p.name, "verdict": "UNKNOWN", "reason": rep["unknown"]})
            continue
        clean = not rep["dirty_rows"] and not rep["missing_today"] and not rep["bad_values"]
        caches.append(
            {
                "name": p.name,
                "verdict": "OK" if clean else "NEED_ATTENTION",
                "rows": int(len(df)),
                **rep,
            }
        )

    if any(c["verdict"] == "UNKNOWN" for c in caches):
        verdict, reason = "UNKNOWN", "存在无法判定的缓存（明细见 caches）"
    elif any(c["verdict"] == "NEED_ATTENTION" for c in caches):
        verdict, reason = "NEED_ATTENTION", "存在脏行/缺品种/坏值（明细见 caches）"
    else:
        verdict, reason = "OK", "缓存内容卫生全部通过"
    return {
        "verdict": verdict,
        "reason": reason,
        "asof": asof.isoformat(),
        "session": session,
        "expect_symbols_n": len(symbols),
        "paths": paths,
        "caches": caches,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="缓存内容卫生守卫（补数后校验）")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "paper.yaml"))
    ap.add_argument("--asof", default=None, help="基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument(
        "--session",
        choices=["night", "day", "auto"],
        default="auto",
        help="night=检查当日行覆盖；day=不检查；auto=按 asof 是否已过 15:00 推断",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON（供自动化解析）")
    args = ap.parse_args(argv)

    try:
        paper_cfg = load_paper_cfg(Path(args.config))
    except Exception as exc:  # noqa: BLE001
        print(f"CONFIG_ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    asof = _to_date(args.asof) if args.asof else date.today()
    session = args.session
    if session == "auto":
        now = datetime.now()
        session = "night" if (now.date() == asof and now.time() >= DAY_CLOSE) else "day"

    # 非交易日跳过（与 p6_4 同口径：复用 TradingSession + holidays）
    try:
        if not TradingSession.from_config(_AttrView(paper_cfg)).is_trading_day(asof):
            print(f"NOT_TRADING_DAY: {asof} 非交易日，跳过（正常）")
            return EXIT_NOT_TRADING_DAY
    except Exception as exc:  # noqa: BLE001
        print(f"UNKNOWN: 交易日判定失败 {exc}")
        return EXIT_UNKNOWN

    result = judge(paper_cfg, asof, session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[P6-5 缓存内容卫生] asof={asof} session={session}")
        print(f"  判定: {result['verdict']}  —— {result['reason']}")
        for c in result.get("caches", []):
            print(f"  ── {c['name']}: {c['verdict']}")
            for r in c.get("dirty_rows", []) or []:
                print(f"     ⛔ 脏行(非交易日): {r['symbol']} @ {r['day']} ×{r['n']}")
            if c.get("missing_today"):
                print(f"     ⛔ 当日缺品种: {', '.join(c['missing_today'])}")
            for b in c.get("bad_values", []) or []:
                print(f"     ⛔ 坏值: {b['col']} {b['symbol']} @ {b['ts']} = {b['value']}")
            if c.get("reason"):
                print(f"     原因: {c['reason']}")
        if result["verdict"] == "NEED_ATTENTION":
            print("  ⛔ 脏行处置属策略决策，须主理人裁决后另行执行（本工具只判定不删改）")
    return {
        "OK": EXIT_OK,
        "NEED_ATTENTION": EXIT_NEED_ATTENTION,
        "UNKNOWN": EXIT_UNKNOWN,
    }[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
