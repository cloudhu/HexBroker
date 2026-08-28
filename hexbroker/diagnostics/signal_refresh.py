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
from typing import Any, Iterable, List, Optional

from .health_check import signal_freshness_days

REFRESH_CMD = "python scripts/p22_tail_ext.py --skip-eval"

# 与 scripts/paper_trading_main._validate / build_components_safe 保持一致：
# 生产信号缓存为「显式文件列表」，不是目录。DEFAULT_CACHE 仅在配置缺失时兜底。
DEFAULT_CACHE = "artifacts/signals_cache18_grouped_v8.parquet"


@dataclass(frozen=True)
class FreshnessProbe:
    """单个缓存文件的新鲜度探测结果。

    ``exists=False`` 或 ``fd=None`` 表示**无法判定**（文件缺失 / 无 ts 列 /
    解析失败）——与「新鲜」是两回事，绝不能当成通过（2026-08-28 假绿缺陷教训）。
    """

    name: str
    latest: Optional[str]
    fd: Optional[int]
    threshold: int
    exists: bool = True

    @property
    def stale(self) -> bool:
        return self.fd is not None and self.fd > self.threshold

    @property
    def unknown(self) -> bool:
        """无法判定新鲜度（缺失/不可读）——必须显性告警，不得静默计为通过。"""
        return (not self.exists) or self.fd is None


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


def unknown_of(probes: List[FreshnessProbe]) -> List[FreshnessProbe]:
    """无法判定新鲜度的探测项（缺失/无 ts 列/解析失败）。"""
    return [p for p in probes if p.unknown]


def resolve_cache_paths(paper_cfg: Any) -> List[str]:
    """解析生产信号缓存路径——**与 SignalEngine 同口径**。

    镜像 ``scripts/paper_trading_main`` 中 ``_validate`` / ``build_components_safe``
    的解析逻辑（signal_caches 列表优先，兼容旧单键 signal_cache）。口径必须一致，
    否则探测的是"另一批文件"，告警就失去意义。
    """
    try:
        caches = paper_cfg.get("signal_caches") or [
            paper_cfg.get("signal_cache", DEFAULT_CACHE)
        ]
    except Exception:  # noqa: BLE001 — 配置结构异常 → 兜底默认路径
        caches = [DEFAULT_CACHE]
    if isinstance(caches, str):
        caches = [caches]
    return [str(c) for c in caches if c]


def probe_files(
    paths: Iterable[str | Path],
    threshold: int = 0,
    asof: Any = None,
) -> List[FreshnessProbe]:
    """按**显式文件路径**探测新鲜度（生产接线主入口）。

    与 :func:`probe`（目录扫描）的关键差别：**不静默丢弃**。文件缺失 / 无 ``ts``
    列 / 解析失败 → 产出 ``fd=None`` 的探测项（记为"无法判定"），由
    :func:`format_banner` 显性告警。目录扫描在路径错配时返回空列表，会被调用方
    误判为"全部通过"——这正是 2026-08-28 假绿缺陷的成因。
    """
    out: List[FreshnessProbe] = []
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001 — pandas 不可用 → 全部记为无法判定
        return [
            FreshnessProbe(Path(p).name, None, None, int(threshold), exists=True)
            for p in paths
        ]
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            out.append(FreshnessProbe(p.name, None, None, int(threshold), exists=False))
            continue
        try:
            df = pd.read_parquet(p)
            if "ts" not in df.columns or df.empty:
                out.append(FreshnessProbe(p.name, None, None, int(threshold)))
                continue
            latest_ts = pd.to_datetime(df["ts"]).max()
        except Exception:  # noqa: BLE001
            out.append(FreshnessProbe(p.name, None, None, int(threshold)))
            continue
        out.append(
            FreshnessProbe(
                p.name,
                latest_ts.strftime("%Y-%m-%d %H:%M"),
                signal_freshness_days(latest_ts, asof),
                int(threshold),
            )
        )
    return out


def format_banner(probes: List[FreshnessProbe]) -> str:
    """生成醒目横幅（stale 或 unknown 时）；全部新鲜且可判定 → 空串。

    未探测到任何文件（空列表）= 配置/接线错误，同样视为"无法判定"处理，
    杜绝"什么都没查到却报告通过"的假绿。
    """
    stale = stale_of(probes)
    unknown = unknown_of(probes)
    if not stale and not unknown:
        return "" if probes else _banner_unknown([])
    parts: List[str] = []
    if stale:
        parts += [
            "=" * 64,
            "⛔ 信号缓存陈旧 —— 主源信号已过期，今日将【不会开仓】",
            "=" * 64,
        ]
        for p in stale:
            parts.append(f"  · {p.name}  最新={p.latest}  fd={p.fd}>阈值{p.threshold}")
        parts.append(f"  → 修复命令：{REFRESH_CMD}")
        parts.append("  （刷新后重跑 health_check 验证 fd=0 再启动交易）")
        parts.append("=" * 64)
    if unknown:
        parts.append(_banner_unknown(unknown))
    return "\n".join(parts)


def _banner_unknown(unknown: List[FreshnessProbe]) -> str:
    lines = [
        "=" * 64,
        "⚠️ 信号缓存新鲜度【无法判定】——未探测到有效信号文件，存在假绿风险",
        "=" * 64,
    ]
    if unknown:
        for p in unknown:
            why = "文件缺失" if not p.exists else "缺少 ts 列或解析失败"
            lines.append(f"  · {p.name}  —— {why}")
    else:
        lines.append("  · 探测结果为空：signal_caches 配置可能为空或路径错配")
    lines.append("  → 请核对 configs/paper.yaml → paper.signal_caches")
    lines.append("  （该状态下门禁形同虚设，必须修复后再启动交易）")
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
