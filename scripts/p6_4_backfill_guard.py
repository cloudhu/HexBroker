#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-b 补刷守卫：判定「信号缓存是否落后于当前会话应有的新鲜度」。

背景（2026-08-28 夜盘事故）
--------------------------
20:30 夜盘刷新任务因 pandadata 连接器未接入而 **0/18 失败**，缓存停在 08-27
（fd=1）。此后系统没有任何机制会在依赖恢复后自动重试——只能等到次日 08:00
任务，代价是**整个夜盘**主源过期（技术兜底或 0 开仓）。

本脚本补上「检测」这一环，**只判定、不拉取**：

- 拉取必须调用 pandadata MCP，由 AI 会话驱动，纯脚本做不到；
- p22 全量重训约 8 分钟，**远超单次自动化时长**，因此重试不能串行等待在
  一次执行里，必须由多个独立触发点（21:30 / 22:30 …）各自先问守卫；
- 守卫在自动化重试点前置调用：NEED_BACKFILL 才跑完整管线，
  **幂等防抖**——已经刷好的话，重试点是零成本空转，不会白跑 8 分钟。

期望新鲜度（--session）
----------------------
⛔ fd 是**交易日 lag（P3-C 口径），可为负**；见下方常量 ``SESSION_EXPECT_FD``
的权威注释。**不要按自然日差或工作日差去理解**。

- ``night``（默认，20:30 及之后）：当日 15:00 已收盘且 20:30 已刷新，
  缓存应覆盖**当日** → signal 日 == asof 日 → **期望 fd = -1**
- ``day``（08:00 日盘前）：当日尚未收盘，用上一交易日信号（标准 T+1）→ **期望 fd = 0**

⚠️ 历史坑（2026-08-31 修订）：本文档段此前写的是 ``night→fd=0 / day→fd=1``，
那是 P3-C 之前的口径残留，与代码常量矛盾，会直接误导运维判断。
**负值不是异常**：``fd < 0`` 表示信号比「标准 T+1」更新，必须保留符号，
否则夜盘守卫无法区分「已刷新」与「刷新漏跑」（两者都会被截断成 0）。

系统级 fd = 所有缓存中**最好**的那个（``min(fd)``）。理由：只要有一个缓存达到
期望新鲜度，引擎即可正常交易；用最差值会把"兜底文件本就陈旧"误判成需补刷。

退出码契约（写进自动化判断逻辑）
------------------------------
- ``0``  OK —— 无需补刷（已达标）
- ``2``  非交易日 —— 跳过（正常，不是失败）
- ``10`` NEED_BACKFILL —— 落后于期望，需执行补刷
- ``11`` UNKNOWN —— 无法判定（缓存缺失 / 无 ts 列 / 解析失败）→ **需人工介入，
  绝不当成 OK**（2026-08-28 假绿缺陷教训：静默计为通过 = 门禁失效）
- ``3``  配置或探测异常

用法
----
    python scripts/p6_4_backfill_guard.py                      # 夜盘口径
    python scripts/p6_4_backfill_guard.py --session day        # 日盘口径
    python scripts/p6_4_backfill_guard.py --expect-fd 0 --json # 机器可读
    python scripts/p6_4_backfill_guard.py --asof 2026-08-28    # 指定基准日（可测）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hexbroker.diagnostics.signal_refresh import (  # noqa: E402
    format_banner,
    probe_files,
    resolve_cache_paths,
)
from hexbroker.paper.sessions import TradingSession  # noqa: E402

# 退出码（与模块 docstring 契约一致）
EXIT_OK = 0
EXIT_NOT_TRADING_DAY = 2
EXIT_CONFIG_ERROR = 3
EXIT_NEED_BACKFILL = 10
EXIT_UNKNOWN = 11

# --session → 期望 fd（**交易日 lag**，P3-C 口径，可为负）：
#   night = -1：夜盘 20:30 刷新后缓存应覆盖**当日**（signal 日就是 asof 日 → lag=-1）。
#               若刷新漏跑，缓存停在上一交易日 → lag=0 > -1 → NEED_BACKFILL。
#   day   =  0：日盘用**上一交易日**收盘信号（标准 T+1 → lag=0）。
#               周一用周五信号、假期后首日用节前信号同样 lag=0 → 放行（P3-B 正是误拦此处）。
# ⛔ 负值不是异常：lag<0 表示信号比「标准 T+1」更新（来自当日或之后）。
#    必须保留符号，否则夜盘守卫无法区分「已刷新」与「刷新漏跑」（两者都会被截断成 0）。
SESSION_EXPECT_FD: Dict[str, int] = {"night": -1, "day": 0}


class _AttrView:
    """让普通 dict 支持属性访问 + ``.get``，以复用 ``TradingSession.from_config``。

    生产侧 paper_cfg 是 omegaconf ``DictConfig``（同时支持属性与 ``.get``）；
    本脚本用 ``yaml.safe_load`` 拿到的是纯 dict，属性访问会 AttributeError。
    与其另写一套交易日逻辑（口径漂移风险），不如做个最小适配，
    **保证与引擎同口径**（同一份 holidays_2026 + weekday<5）。
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


def load_paper_cfg(config_path: Path) -> Any:
    """读取 ``configs/paper.yaml`` 的 ``paper`` 段（不存在 → 抛错，由 main 转 exit 3）。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    paper = raw.get("paper")
    if not isinstance(paper, dict):
        raise ValueError(f"配置缺少 paper 段：{config_path}")
    return paper


def is_trading_day(paper_cfg: Any, d: date) -> bool:
    """交易日判定，**与生产引擎同口径**（复用 TradingSession + holidays_2026）。"""
    session = TradingSession.from_config(_AttrView(paper_cfg))
    return bool(session.is_trading_day(d))


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def judge(
    paper_cfg: Any,
    asof: date,
    expect_fd: int,
) -> Dict[str, Any]:
    """核心判定：返回结构化结果字典（供 --json 输出与退出码映射）。"""
    paths = resolve_cache_paths(paper_cfg)
    if not paths:
        return {
            "verdict": "UNKNOWN",
            "reason": "paper.signal_caches 解析为空（配置或接线错误）",
            "asof": asof.isoformat(),
            "expect_fd": expect_fd,
            "paths": [],
        }

    probes = probe_files(paths, threshold=0, asof=asof)
    if len(probes) != len(paths):  # 同 2026-08-28 修复口径：绝不静默丢路径
        return {
            "verdict": "UNKNOWN",
            "reason": "probe_files 静默丢弃路径（会退化成假绿）",
            "asof": asof.isoformat(),
            "expect_fd": expect_fd,
            "paths": paths,
        }

    known = [p for p in probes if p.fd is not None]
    unknown = [p for p in probes if p.fd is None]

    detail = [
        {
            "name": p.name,
            "latest": p.latest,
            "fd": p.fd,
            "exists": p.exists,
        }
        for p in probes
    ]

    if not known:
        return {
            "verdict": "UNKNOWN",
            "reason": "所有缓存均无法判定新鲜度（缺失/无 ts 列/解析失败）",
            "asof": asof.isoformat(),
            "expect_fd": expect_fd,
            "probes": detail,
            "banner": format_banner(probes),
        }

    sys_fd = min(p.fd for p in known)  # 系统级新鲜度 = 最好的那个
    verdict = "OK" if sys_fd <= expect_fd else "NEED_BACKFILL"
    result: Dict[str, Any] = {
        "verdict": verdict,
        "asof": asof.isoformat(),
        "expect_fd": expect_fd,
        "system_fd": sys_fd,
        "lag": max(0, sys_fd - expect_fd),
        "probes": detail,
    }
    if unknown:
        result["unknown"] = [p.name for p in unknown]
        result["banner"] = format_banner(probes)
    return result


def _render_human(result: Dict[str, Any], session: str) -> str:
    lines: List[str] = []
    lines.append(
        f"[补刷守卫] session={session} expect_fd={result.get('expect_fd')} "
        f"asof={result.get('asof')}"
    )
    for p in result.get("probes", []) or []:
        fd = p.get("fd")
        lines.append(
            f"  · {p['name']:<48} fd={'-' if fd is None else fd}  "
            f"latest={p.get('latest') or 'N/A'}"
        )
    verdict = result["verdict"]
    if verdict == "NEED_BACKFILL":
        lines.append(
            f"判定: ⛔ NEED_BACKFILL —— 系统级 fd={result['system_fd']} > 期望 "
            f"{result['expect_fd']}，落后 {result['lag']} 个交易日"
        )
        lines.append(
            "建议: 执行补刷（pandadata 拉 18 品种 → "
            "scripts/p6_4_apply_persisted_dir.py --trading-day）"
        )
    elif verdict == "OK":
        lines.append(
            f"判定: ✅ OK —— 系统级 fd={result['system_fd']} 已达标，无需补刷（重试点零成本空转）"
        )
    elif verdict == "UNKNOWN":
        lines.append(f"判定: 🔴 UNKNOWN —— {result.get('reason')}（需人工介入，绝不当 OK）")
    else:
        lines.append(f"判定: {verdict} —— {result.get('reason', '')}")
    if result.get("unknown"):
        lines.append(f"  无法判定的缓存: {', '.join(result['unknown'])}")
    if result.get("banner"):
        lines.append(result["banner"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="P1-b 补刷守卫：判定信号缓存是否落后于当前会话应有的新鲜度（只判定，不拉取）",
    )
    ap.add_argument(
        "--session",
        choices=sorted(SESSION_EXPECT_FD),
        default="night",
        help="会话窗口：night=夜盘（期望 lag=-1，缓存已覆盖当日）；day=日盘前（期望 lag=0，隔夜信号）",
    )
    ap.add_argument(
        "--expect-fd",
        type=int,
        default=None,
        help="显式覆盖期望 fd（优先于 --session）",
    )
    ap.add_argument(
        "--asof",
        default=None,
        help="基准日 YYYY-MM-DD（默认今天；指定后可确定性测试）",
    )
    ap.add_argument(
        "--config",
        default="configs/paper.yaml",
        help="配置文件路径（默认 configs/paper.yaml）",
    )
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", help="仅输出退出码，不打人类可读文本")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    expect_fd = args.expect_fd if args.expect_fd is not None else SESSION_EXPECT_FD[args.session]

    try:
        paper_cfg = load_paper_cfg(Path(args.config))
    except Exception as exc:  # noqa: BLE001 — 配置不可读 → exit 3，且必须显性
        print(f"[补刷守卫] 🔴 配置异常：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    asof = _to_date(args.asof) if args.asof else date.today()

    try:
        trading = is_trading_day(paper_cfg, asof)
    except Exception as exc:  # noqa: BLE001 — 交易日判定失败 → 同配置异常处理
        print(f"[补刷守卫] 🔴 交易日判定失败：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not trading:
        result = {
            "verdict": "SKIP_NOT_TRADING_DAY",
            "asof": asof.isoformat(),
            "expect_fd": expect_fd,
            "reason": f"{asof.isoformat()} 非交易日，无需补刷",
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif not args.quiet:
            print(_render_human(result, args.session))
        return EXIT_NOT_TRADING_DAY

    result = judge(paper_cfg, asof, expect_fd)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_render_human(result, args.session))

    return {
        "OK": EXIT_OK,
        "NEED_BACKFILL": EXIT_NEED_BACKFILL,
        "UNKNOWN": EXIT_UNKNOWN,
    }.get(result["verdict"], EXIT_CONFIG_ERROR)


if __name__ == "__main__":
    sys.exit(main())
