#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""成交审计日志 → 中文交易意图转化脚本（运维可读化）。

把模拟盘审计日志（默认 ``data/paper/trades.log``）中的结构化 JSON 行
（``{"event": "trade"}`` / ``{"event": "plan_change"}``）逐条翻译为运维友好的
中文交易意图，便于值班/复盘时快速读懂机器日志。

与 ``TRADE|`` / ``PLAN|`` 机器摘要行去重——优先采用结构化 JSON 行（含开平/今平信息），
机器摘要行仅作冗余，默认忽略。

用法::

    # 默认读取 data/paper/trades.log，打印中文意图到 stdout
    python scripts/trade_log_chinese.py

    # 指定日志文件 + 落盘
    python scripts/trade_log_chinese.py /path/to/trades.log --out trades.zh.txt

    # 仅看某日及之后
    python scripts/trade_log_chinese.py --since 2026-08-24

    # 输出按日分组的 Markdown 复盘报告
    python scripts/trade_log_chinese.py --markdown --out report.md

从 stdin 读取（管道）::

    cat trades.log | python scripts/trade_log_chinese.py -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# 允许以脚本方式运行（python scripts/xxx.py）时导入包内模块
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hexbroker.paper.trade_intent import plan_change_intent, trade_intent  # noqa: E402

_AUDIT_EVENTS = {"trade", "plan_change"}

# 真实日志行形如 ``2026-08-24 09:00:01 | {"event": "trade", ...}``
# （loguru sink 格式 ``{time} | {message}``，message 为 JSON），故在整行内抽取 JSON。
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _is_json_audit(line: str):
    """若行内含结构化审计 JSON（event in _AUDIT_EVENTS）则返回 dict，否则 None。"""
    m = _JSON_RE.search(line)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and obj.get("event") in _AUDIT_EVENTS:
        return obj
    return None


def convert_lines(lines):
    """逐行产出 ``(event_type, chinese_text)``。"""
    for raw in lines:
        obj = _is_json_audit(raw)
        if obj is None:
            continue
        ev = obj["event"]
        if ev == "trade":
            yield ("trade", trade_intent(obj))
        elif ev == "plan_change":
            yield ("plan_change", plan_change_intent(obj))


def _day_of(text: str) -> str:
    """从中文意图文本前缀（``YYYY-MM-DD ...``）提取日期，用于分组/过滤。"""
    s = text.strip()
    return s[:10] if (len(s) >= 10 and s[0].isdigit()) else "未知日期"


def build_markdown(items) -> str:
    """按日分组的中文意图 Markdown 报告。"""
    by_day: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ev_type, text in items:
        by_day[_day_of(text)].append((ev_type, text))

    out: list[str] = ["# 模拟盘中文交易意图报告", ""]
    total = len(items)
    out.append(f"> 共转化 {total} 条记录（成交 + 计划变更）。")
    out.append("")
    for day in sorted(by_day):
        out.append(f"## {day}")
        out.append("")
        for ev_type, text in by_day[day]:
            prefix = "[成交]" if ev_type == "trade" else "[计划]"
            out.append(f"- {prefix} {text}")
        out.append("")
    return "\n".join(out)


def _read_source(path_arg: str):
    """返回可迭代的行序列：``-`` 表示 stdin，否则文件（支持相对 ROOT）。"""
    if path_arg == "-":
        return sys.stdin
    p = Path(path_arg)
    if not p.exists():
        alt = ROOT / path_arg
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError(f"日志文件不存在: {path_arg}")
    return p.open("r", encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="成交审计日志 → 中文交易意图转化（运维可读化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "logfile",
        nargs="?",
        default="data/paper/trades.log",
        help="审计日志路径（默认 data/paper/trades.log；'-' 表示从 stdin 读取）",
    )
    ap.add_argument("--out", help="输出文件路径（默认打印到 stdout）")
    ap.add_argument("--markdown", action="store_true", help="输出按日分组的 Markdown 报告")
    ap.add_argument("--since", help="仅输出该日期(YYYY-MM-DD)及之后的记录")
    args = ap.parse_args(argv)

    try:
        src = _read_source(args.logfile)
    except FileNotFoundError as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 2

    with src if src is sys.stdin else src:
        items = list(convert_lines(src if src is sys.stdin else src))

    if args.since:
        since = args.since
        items = [(t, x) for (t, x) in items if _day_of(x) >= since]

    if not items:
        out = "(无成交/计划变更审计记录)"
    elif args.markdown:
        out = build_markdown(items)
    else:
        out = "\n".join(text for _, text in items)

    if args.out:
        Path(args.out).write_text(out + "\n", encoding="utf-8")
        print(f"[OK] 已转化 {len(items)} 条中文意图 → {args.out}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
