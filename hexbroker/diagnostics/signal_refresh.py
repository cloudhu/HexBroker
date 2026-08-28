"""信号新鲜度门禁 · 启动期硬告警 + 可选自动刷新（B+C 防再发批次）。

背景（2026-08-28 停摆事故）：阈值=0 的隔夜过期门禁要求"每个交易日刷新缓存"，
漏跑即全天停摆——且系统不报错、不崩溃，只输出情报事件，表现为"看起来在跑却不交易"。

本模块提供三件事（全部默认零行为变更 / fail-safe）：
  1. ``probe``——探测信号缓存新鲜度（fd=工作日差），复出``signal_freshness_days``同口径；
  2. ``format_banner``——fd>阈值时生成**醒目横幅**（C1：让"不交易"变显性）；
  3. ``maybe_auto_refresh``——配置开启时才 subprocess 跑 ``p22_tail_ext.py --skip-eval``
     （默认关；超时/失败一律降级为告警，**绝不阻断启动**）。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from .health_check import signal_freshness_days

REFRESH_CMD = "python scripts/p22_tail_ext.py --skip-eval"


@dataclass(frozen=True)
class FreshnessProbe:
    """单个缓存文件的新鲜度探测结果。"""

    name: str
    latest: Optional[str]
    fd: Optional[int]
    threshold: int

    @property
    def stale(self) -> bool:
        return self.fd is not None and self.fd > self.threshold


def probe(
    cache_dir: str | Path = "data/signal_caches",
    threshold: int = 0,
    asof: Any = None,
) -> List[FreshnessProbe]:
    """遍历缓存目录（*.parquet）给出新鲜度；目录缺失/解析失败 → 空列表（fail-safe）。"""
    out: List[FreshnessProbe] = []
    try:
        import pandas as pd

        d = Path(cache_dir)
        if not d.exists():
            return out
        for p in sorted(d.glob("*.parquet")):
            try:
                df = pd.read_parquet(p)
                if "ts" not in df.columns or df.empty:
                    continue
                latest_ts = pd.to_datetime(df["ts"]).max()
            except Exception:
                continue
            fd = signal_freshness_days(latest_ts, asof)
            out.append(
                FreshnessProbe(
                    name=p.name,
                    latest=latest_ts.strftime("%Y-%m-%d %H:%M"),
                    fd=fd,
                    threshold=int(threshold),
                )
            )
    except Exception:
        return out  # 依赖不可用 → 跳过检查（不阻断）
    return out


def stale_of(probes: List[FreshnessProbe]) -> List[FreshnessProbe]:
    return [p for p in probes if p.stale]


def format_banner(probes: List[FreshnessProbe]) -> str:
    """生成醒目横幅（stale 时）；无 stale → 空串。"""
    stale = stale_of(probes)
    if not stale:
        return ""
    lines = [
        "=" * 64,
        "⛔ 信号缓存陈旧 —— 主源信号已过期，今日将【不会开仓】",
        "=" * 64,
    ]
    for p in stale:
        lines.append(f"  · {p.name}  最新={p.latest}  fd={p.fd}>阈值{p.threshold}")
    lines.append(f"  → 修复命令：{REFRESH_CMD}")
    lines.append("  （刷新后重跑 health_check 验证 fd=0 再启动交易）")
    lines.append("=" * 64)
    return "\n".join(lines)


def maybe_auto_refresh(
    probes: List[FreshnessProbe],
    enabled: bool = False,
    timeout_sec: int = 900,
    root: str | Path = ".",
) -> tuple[bool, str]:
    """stale 且 enabled 时自动跑刷新脚本；失败/超时降级为告警，绝不抛异常。

    返回 ``(refreshed, message)``：refreshed=True 表示刷新命令执行成功（exit 0）。
    """
    if not enabled:
        return False, "自动刷新未启用（signal_refresh.auto_enabled=false）"
    if not stale_of(probes):
        return False, "缓存新鲜，无需刷新"
    try:
        r = subprocess.run(
            [sys.executable, "scripts/p22_tail_ext.py", "--skip-eval"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, f"自动刷新超时（>{timeout_sec}s），请手动执行：{REFRESH_CMD}"
    except Exception as e:  # noqa: BLE001
        return False, f"自动刷新异常：{type(e).__name__}: {e}；请手动执行：{REFRESH_CMD}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return False, f"自动刷新失败 exit={r.returncode}：{' | '.join(tail)}"
    return True, "自动刷新成功（建议重跑 health_check 验证 fd=0）"
