"""B+C 防再发批次测试：信号新鲜度探测 / 醒目横幅 / 可选自动刷新 / 0 开仓显性汇总。"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from hexbroker.diagnostics.signal_refresh import (
    FreshnessProbe,
    format_banner,
    maybe_auto_refresh,
    probe,
    stale_of,
)
from hexbroker.paper.scheduler import TradingScheduler


def _mk_cache(tmp_path: Path, name: str, ts: datetime) -> Path:
    d = tmp_path / "caches"
    d.mkdir(exist_ok=True)
    pd.DataFrame({"symbol": ["rb0"], "ts": [ts], "p_up": [0.6]}).to_parquet(d / name)
    return d


# ---- probe ----
def test_probe_detects_stale_and_fresh(tmp_path: Path):
    d = _mk_cache(tmp_path, "old.parquet", datetime.now() - timedelta(days=3))
    _mk_cache(d, "new.parquet", datetime.now())  # 同一目录再放一个新鲜缓存
    probes = probe(d, threshold=0)
    stale = stale_of(probes)
    assert any(p.name == "old.parquet" and p.stale for p in stale)
    assert all(not p.stale for p in probes if p.name == "new.parquet")


def test_probe_missing_dir_returns_empty(tmp_path: Path):
    assert probe(tmp_path / "nope", threshold=0) == []


def test_probe_threshold_one_tolerates_overnight(tmp_path: Path):
    d = _mk_cache(tmp_path, "y.parquet", datetime.now() - timedelta(days=1))
    assert stale_of(probe(d, threshold=0)) != []      # 阈值0：隔夜即过期
    assert stale_of(probe(d, threshold=1)) == []      # 阈值1：容忍隔夜


# ---- banner ----
def test_banner_lists_stale_and_fix_command():
    probes = [FreshnessProbe("a.parquet", "2026-08-27 00:00", fd=1, threshold=0)]
    banner = format_banner(probes)
    assert "不会开仓" in banner and "a.parquet" in banner
    assert "p22_tail_ext.py --skip-eval" in banner


def test_banner_empty_when_fresh():
    assert format_banner([FreshnessProbe("a", "2026-08-28 00:00", fd=0, threshold=0)]) == ""
    assert format_banner([]) == ""


# ---- 自动刷新 ----
def test_auto_refresh_disabled_by_default():
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    refreshed, msg = maybe_auto_refresh(probes, enabled=False)
    assert refreshed is False and "未启用" in msg


def test_auto_refresh_skips_when_fresh():
    refreshed, msg = maybe_auto_refresh([FreshnessProbe("a", "x", fd=0, threshold=0)], enabled=True)
    assert refreshed is False and "无需刷新" in msg


def test_auto_refresh_success(monkeypatch, tmp_path: Path):
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    monkeypatch.setattr(
        "hexbroker.diagnostics.signal_refresh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="ok", stderr=""),
    )
    refreshed, msg = maybe_auto_refresh(probes, enabled=True, root=tmp_path)
    assert refreshed is True and "成功" in msg


def test_auto_refresh_failure_and_timeout_never_raise(monkeypatch, tmp_path: Path):
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    monkeypatch.setattr(
        "hexbroker.diagnostics.signal_refresh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
    )
    refreshed, msg = maybe_auto_refresh(probes, enabled=True, root=tmp_path)
    assert refreshed is False and "失败" in msg

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("hexbroker.diagnostics.signal_refresh.subprocess.run", _timeout)
    refreshed2, msg2 = maybe_auto_refresh(probes, enabled=True, root=tmp_path)
    assert refreshed2 is False and "超时" in msg2


def test_real_config_auto_refresh_disabled():
    """仓库真实配置必须默认关自动刷新（生产数据操作需人工确认）。"""
    cfg = yaml.safe_load(Path("configs/paper.yaml").read_text(encoding="utf-8"))
    assert cfg["signal_refresh"]["auto_enabled"] is False
    assert cfg["signal_refresh"]["threshold"] == 0


# ---- C2：0 开仓显性汇总 ----
def test_warn_zero_open_emits_reason_distribution(monkeypatch):
    sched = TradingScheduler.__new__(TradingScheduler)  # 跳过 __init__（重依赖）
    sched._block_reasons = {"2026-08-28": {"no_intent": 120, "cost_gate_reject": 3}}
    sched._last_zero_open_alert = None
    calls: list[str] = []

    class _Log:
        def warning(self, tpl, *a, **k):
            calls.append(tpl.format(*a, **k) if a else tpl)

    sched_log = _Log()
    monkeypatch.setattr(sched, "_warn_zero_open", TradingScheduler._warn_zero_open.__get__(sched))
    
    # 直接调用并捕获日志输出（loguru 通过模块级 logger，改为校验不抛异常+去重逻辑）
    import hexbroker.paper.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "log", sched_log)
    sched._warn_zero_open("2026-08-28")
    assert any("今日开仓 0 笔" in c and "no_intent=120" in c for c in calls)
    # 去重：同指纹再调用不再输出
    sched._warn_zero_open("2026-08-28")
    assert sum("今日开仓 0 笔" in c for c in calls) == 1
