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
    probe_files,
    resolve_cache_paths,
    stale_of,
    unknown_of,
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
    # 空列表 ≠ 通过（D2 修复：未探测到文件属"无法判定"，必须出横幅）
    assert format_banner([]) != ""


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
    assert cfg["paper"]["signal_refresh"]["auto_enabled"] is False
    assert cfg["paper"]["signal_refresh"]["threshold"] == 0


# ---- 2026-08-28 缺陷 D1/D2 回归锁（假绿防护，P0） ----
def test_signal_refresh_lives_under_paper_section():
    """D1：signal_refresh 必须位于 paper: 段内。

    _load_paper_config 只把 paper 子段传给下游，顶层同键**永远读不到** →
    配置静默失效（自动刷新/阈值全部回退默认）。
    """
    cfg = yaml.safe_load(Path("configs/paper.yaml").read_text(encoding="utf-8"))
    assert "signal_refresh" not in cfg, "signal_refresh 误置于顶层（paper_cfg 读不到）"
    assert "signal_refresh" in cfg["paper"]


def test_probe_files_missing_path_is_unknown_not_silent(tmp_path: Path):
    """D2：缺失文件必须产出"无法判定"探测项，不得静默丢弃（空列表=假绿）。"""
    ok = _mk_cache(tmp_path, "ok.parquet", datetime.now())
    probes = probe_files([str(ok / "ok.parquet"), str(tmp_path / "ghost.parquet")], threshold=0)
    assert len(probes) == 2, "缺失路径被静默丢弃 → 会退化成假绿"
    ghost = [p for p in probes if p.name == "ghost.parquet"][0]
    assert ghost.exists is False and ghost.unknown is True
    assert unknown_of(probes) == [ghost]


def test_banner_flags_unknown_and_empty():
    """无法判定（含探测结果为空）必须出横幅——"没查到"≠"通过"。"""
    assert "无法判定" in format_banner([FreshnessProbe("a", None, None, 0, exists=False)])
    assert "无法判定" in format_banner([])
    # 全部新鲜且可判定 → 无横幅
    assert format_banner([FreshnessProbe("a", "2026-08-28 00:00", 0, 0)]) == ""


def test_production_paths_are_probeable():
    """D2 端到端锁：真实 paper.yaml 解析出的缓存路径必须**全部可探测**。

    缺陷原状：接线用 cache_dir=data/signal_caches（目录不存在）→ probe 返回 []
    → 打印"检查通过"。本用例确保解析口径与 SignalEngine 一致且文件真实存在。
    """
    cfg = yaml.safe_load(Path("configs/paper.yaml").read_text(encoding="utf-8"))
    paths = resolve_cache_paths(cfg["paper"])
    assert paths, "paper.signal_caches 解析为空"
    probes = probe_files(paths, threshold=0)
    assert len(probes) == len(paths)
    missing = [p.name for p in probes if not p.exists or p.fd is None]
    assert not missing, f"存在无法判定的生产缓存：{missing}"


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
