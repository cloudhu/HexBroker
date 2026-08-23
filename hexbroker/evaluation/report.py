"""报告生成（§3.6 / §8.5）。"""

from __future__ import annotations

import json
from dataclasses import asdict

from .metrics import MetricsReport


def format_report(report: MetricsReport) -> str:
    """把绩效报告格式化为可读文本。"""
    d = report.to_dict()
    lines = [
        "=" * 48,
        "HexFutures-AI 回测绩效报告",
        "=" * 48,
        f"  最终权益      : {d['final_equity']:.2f}",
        f"  总收益率      : {d['total_return'] * 100:+.2f}%",
        f"  年化收益      : {d['annual_return'] * 100:+.2f}%",
        f"  Sharpe        : {d['sharpe']:.3f}",
        f"  Sortino       : {d['sortino']:.3f}",
        f"  最大回撤      : {d['max_drawdown'] * 100:.2f}%",
        f"  Calmar        : {d['calmar']:.3f}",
        f"  年化波动      : {d['volatility'] * 100:.2f}%",
        f"  胜率          : {d['win_rate'] * 100:.1f}%",
        f"  盈亏比(PF)    : {d['profit_factor']:.2f}",
        f"  样本数        : {d['n_bars']}",
        "=" * 48,
    ]
    return "\n".join(lines)


def report_to_json(report: MetricsReport, path: str) -> None:
    """把报告序列化到 JSON。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2, default=str)
