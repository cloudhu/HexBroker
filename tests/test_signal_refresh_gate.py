"""B+C 防再发批次测试：信号新鲜度探测 / 醒目横幅 / 可选自动刷新 / 0 开仓显性汇总。

P3-C（2026-08-31）口径同步说明
------------------------------
新鲜度由「自然日差」改为**交易日历 lag** 后，凡用 ``datetime.now()`` ± N 天
构造缓存时间戳的用例都失去了确定性：

- 判定依赖**当日是否交易日**、**当前时刻是否过了日盘收盘**，两者都随运行时刻变化；
- CI 主湖缺失 → 日历为空 → 全部判「无法判定」。

故本文件的探测类用例统一改为：**注入迷你日历 + 显式 ``asof``**。
``asof`` 一律取 ``2026-08-28 21:30``（周五夜盘，当日日盘已收盘 → R = 08-28），
于是 lag 对信号日的映射是确定的：

=================  ==========  ==========================
信号日             lag         含义
=================  ==========  ==========================
2026-08-28         **0**       当日已刷新（标准 T+1 之上）
2026-08-27         1           落后 1 个交易日
2026-08-25         3           落后 3 个交易日
=================  ==========  ==========================
"""
from __future__ import annotations

import subprocess
from datetime import date, datetime
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

# 迷你日历区间 / 固定基准时刻（见模块 docstring）
CAL_START = date(2026, 8, 1)
CAL_END = date(2026, 9, 30)
ASOF_NIGHT = datetime(2026, 8, 28, 21, 30)  # 周五夜盘，当日日盘已收盘 → R = 08-28
ASOF_DAY = datetime(2026, 8, 28, 10, 30)  # 周五日盘，未收盘 → R = 08-27


def _cache_dir(tmp_path: Path) -> Path:
    """缓存目录（不存在则创建）。"""
    d = tmp_path / "caches"
    d.mkdir(exist_ok=True)
    return d


def _mk_cache(d: Path, name: str, ts) -> Path:
    """在**已存在的**缓存目录 ``d`` 下写入一个缓存文件，返回目录本身。

    ⛔ 不要在这里再拼 ``caches`` 子目录：原实现 ``d = tmp_path / "caches"`` 会把
    第二次调用落到 ``caches/caches/``，文件落在被探测目录之外而被静默跳过
    （2026-08-31 修复：原用例未断言探测条数，缺陷一直潜伏）。
    """
    pd.DataFrame({"symbol": ["rb0"], "ts": [pd.Timestamp(ts)], "p_up": [0.6]}).to_parquet(d / name)
    return d


# ---- probe ----
def test_probe_detects_stale_and_fresh(use_calendar, tmp_path: Path):
    """落后 3 个交易日 → stale；当日已刷新 → fresh（同一目录内对照）。"""
    use_calendar(CAL_START, CAL_END)
    d = _cache_dir(tmp_path)
    _mk_cache(d, "old.parquet", "2026-08-25 15:00")  # lag=3
    _mk_cache(d, "new.parquet", "2026-08-28 15:00")  # lag=0
    probes = probe(d, threshold=0, asof=ASOF_NIGHT)
    assert len(probes) == 2
    stale = stale_of(probes)
    assert any(p.name == "old.parquet" and p.stale and p.fd == 3 for p in stale)
    assert all(not p.stale and p.fd == 0 for p in probes if p.name == "new.parquet")


def test_probe_missing_dir_returns_empty(tmp_path: Path):
    assert probe(tmp_path / "nope", threshold=0) == []


def test_probe_threshold_tolerance(use_calendar, tmp_path: Path):
    """落后 1 个交易日：阈值 0 → stale；阈值 1 → 容忍放行。

    ⛔ 与 P3-B 之前的语义**相反**：旧口径「隔夜（相邻交易日）= 陈旧」，
    新口径下「相邻交易日」正是标准 T+1（lag=0），**不是**陈旧。
    故判据必须用 lag=1 的样本，隔夜样本已不再具备鉴别力。
    """
    use_calendar(CAL_START, CAL_END)
    d = _cache_dir(tmp_path)
    _mk_cache(d, "y.parquet", "2026-08-27 15:00")  # lag=1
    assert stale_of(probe(d, threshold=0, asof=ASOF_NIGHT)) != []
    assert stale_of(probe(d, threshold=1, asof=ASOF_NIGHT)) == []


def test_probe_overnight_is_fresh_not_stale(use_calendar, tmp_path: Path):
    """⛔ 回归护栏：隔夜信号（上一交易日收盘 → 当日执行）必须判**新鲜**。

    P3-B 自然日差口径下跨周末 fd=3 > 阈值 1 → 误判陈旧，是周一/假期后首日
    主源被静默禁用的直接原因（全历史实测误拦 21.36%）。
    """
    use_calendar(CAL_START, CAL_END)
    d = _cache_dir(tmp_path)
    _mk_cache(d, "overnight.parquet", "2026-08-27 15:00")  # 上一交易日
    probes = probe(d, threshold=0, asof=ASOF_DAY)  # 日盘 10:30 → R = 08-27 → lag=0
    assert [p.fd for p in probes] == [0]
    assert stale_of(probes) == []
    assert unknown_of(probes) == []


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
# P1-2（2026-09-01）：``maybe_auto_refresh`` 新增**时间依赖**——交易时段内直接跳过，
# 不再拉起子进程。故下列用例必须**钉死时钟**，否则套件会随运行时段时好时坏
# （例如在 13:20–15:00 日盘下午窗口跑就会全红）。
# 取周二 12:05 = 午休补刷窗口（is_cache_rewrite_blocked=False，确定性允许）。
ALLOWED_NOW = datetime(2026, 9, 1, 12, 5, 0)


def test_auto_refresh_disabled_by_default():
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    refreshed, msg = maybe_auto_refresh(probes, enabled=False, now=ALLOWED_NOW)
    assert refreshed is False and "未启用" in msg


def test_auto_refresh_skips_when_fresh():
    refreshed, msg = maybe_auto_refresh(
        [FreshnessProbe("a", "x", fd=0, threshold=0)], enabled=True, now=ALLOWED_NOW
    )
    assert refreshed is False and "无需刷新" in msg


def test_auto_refresh_success(monkeypatch, tmp_path: Path):
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    monkeypatch.setattr(
        "hexbroker.diagnostics.signal_refresh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="ok", stderr=""),
    )
    refreshed, msg = maybe_auto_refresh(probes, enabled=True, root=tmp_path, now=ALLOWED_NOW)
    assert refreshed is True and "成功" in msg


def test_auto_refresh_failure_and_timeout_never_raise(monkeypatch, tmp_path: Path):
    probes = [FreshnessProbe("a", "x", fd=2, threshold=0)]
    monkeypatch.setattr(
        "hexbroker.diagnostics.signal_refresh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
    )
    refreshed, msg = maybe_auto_refresh(probes, enabled=True, root=tmp_path, now=ALLOWED_NOW)
    assert refreshed is False and "失败" in msg

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("hexbroker.diagnostics.signal_refresh.subprocess.run", _timeout)
    refreshed2, msg2 = maybe_auto_refresh(probes, enabled=True, root=tmp_path, now=ALLOWED_NOW)
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


def test_probe_files_missing_path_is_unknown_not_silent(use_calendar, tmp_path: Path):
    """D2：缺失文件必须产出"无法判定"探测项，不得静默丢弃（空列表=假绿）。"""
    use_calendar(CAL_START, CAL_END)
    ok = _mk_cache(_cache_dir(tmp_path), "ok.parquet", "2026-08-28 15:00")
    probes = probe_files(
        [str(ok / "ok.parquet"), str(tmp_path / "ghost.parquet")],
        threshold=0,
        asof=ASOF_NIGHT,
    )
    assert len(probes) == 2, "缺失路径被静默丢弃 → 会退化成假绿"
    ghost = [p for p in probes if p.name == "ghost.parquet"][0]
    assert ghost.exists is False and ghost.unknown is True
    # 存在且可判定的那个不得被算进 unknown
    assert unknown_of(probes) == [ghost]


def test_banner_flags_unknown_and_empty():
    """无法判定（含探测结果为空）必须出横幅——"没查到"≠"通过"。"""
    assert "无法判定" in format_banner([FreshnessProbe("a", None, None, 0, exists=False)])
    assert "无法判定" in format_banner([])
    # 全部新鲜且可判定 → 无横幅
    assert format_banner([FreshnessProbe("a", "2026-08-28 00:00", 0, 0)]) == ""


def test_production_paths_are_probeable():
    """D2 端到端锁：真实 paper.yaml 解析出的缓存路径必须**可被探测，且缺失必告警**。

    缺陷原状：接线用 cache_dir=data/signal_caches（目录不存在）→ probe 返回 []
    → 打印"检查通过"。

    2026-08-28 二次修订（CI 假失败修复）：原实现强断言"文件必须存在"，但
    ``artifacts/`` 与 ``*.parquet`` 均被 .gitignore 排除（数据产物本就不该入库），
    CI 全新 checkout 里必然不存在 → 假失败。改为三段式：

      A 常量断言（CI / 本地均成立）：解析非空；probe_files 绝不静默丢弃路径。
      B 文件齐备（本地）：全部可判定新鲜度。
      C 文件缺失（CI）：**不 skip** —— 正面验证"缺失被判为 unknown 且出横幅"。
        这恰是 D2 反假绿的内核（"没查到" ≠ "通过"），在 CI 上反而锁得更死。
    """
    cfg = yaml.safe_load(Path("configs/paper.yaml").read_text(encoding="utf-8"))
    paths = resolve_cache_paths(cfg["paper"])
    assert paths, "paper.signal_caches 解析为空"

    # A：解析口径与 SignalEngine 一致，且 probe_files 绝不静默丢弃路径
    probes = probe_files(paths, threshold=0)
    assert len(probes) == len(paths), "probe_files 静默丢弃路径 → 会退化成 D2 假绿"
    assert all(p.name for p in probes)

    if all(p.exists for p in probes):
        # B（本地）：文件齐备 → 必须全部可判定新鲜度
        missing = [p.name for p in probes if not p.exists or p.fd is None]
        assert not missing, f"存在无法判定的生产缓存：{missing}"
    else:
        # C（CI：数据产物未入库）：缺失必须显性告警，绝不能当成"通过"
        assert all(p.unknown for p in probes if not p.exists), "缺失文件未被判为 unknown"
        banner = format_banner(probes)
        assert "无法判定" in banner, "缓存缺失却未告警 → D2 假绿复现"
        assert "检查通过" not in banner


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
