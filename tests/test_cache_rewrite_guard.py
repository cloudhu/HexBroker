"""P1-2 交易时段禁止重写生产信号缓存 —— 回归测试（2026-09-01）。

事故背景
--------
2026-09-01 **09:10:12**（日盘交易时段内）生产信号缓存
``artifacts/signals_cache18_grouped_v8_tail_ext.parquet`` 被重写，而模拟盘进程
**08:58** 启动时加载的是旧值（rb0 p_up 0.8667 / exp_ret 0.4480 → 重写后 0.999999 /
1.639979）。同一交易日存在两套信号，13:25 窗口重启后全部开仓与成本门禁结论被静默翻转。

取证补充的同类高危（尚未发生）：20:30 夜盘自动化 = 18 品种拉取 + 约 8 分钟全量重训，
而 rb0 夜盘 **21:00 开盘**、模拟盘 **20:55** 已启动 → 只要超时，落盘就跨入夜盘。
故禁写判定必须按**落盘时刻**裁定，而不是任务启动时刻。

测试安全约束（重要）
--------------------
本文件**绝不触碰生产缓存**：

* ``merge_tail_ext`` 相关用例一律把 ``V8_PATH`` monkeypatch 到不存在的临时路径，
  使「护栏放行」的唯一可能结果是 ``FileNotFoundError``（进入 IO 阶段），
  而「护栏拦截」的结果是 ``SystemExit`` —— 二者可区分，且任何情况下都不会写盘；
* ``main()`` 用例 monkeypatch 掉判定函数与训练函数，不产生任何 IO；
* 自动刷新用例 monkeypatch 掉 ``subprocess.run``，不会真的拉起 15 分钟的重建。
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import pytest

from hexbroker.diagnostics import signal_refresh as sr
from hexbroker.market.session import (
    CACHE_REWRITE_BLOCK_SESSIONS,
    is_cache_rewrite_blocked,
)

# 事故时刻（周二 09:10:12，日盘上午）
BLOCKED_TS = pd.Timestamp("2026-09-01 09:10:12")
# 午休补刷窗口（周二 12:05）
ALLOWED_TS = pd.Timestamp("2026-09-01 12:05:00")


# ===========================================================================
# 一、共享原语 is_cache_rewrite_blocked
# ===========================================================================
@pytest.mark.parametrize(
    "stamp,expected,desc",
    [
        # ---- 事故时刻与盘前 ----
        ("2026-09-01 09:10:12", True, "★事故时刻（周二日盘上午）"),
        ("2026-09-01 08:49:59", False, "盘前 08:50 之前"),
        ("2026-09-01 08:50:00", True, "禁区左边界 08:50（闭区间）"),
        ("2026-09-01 10:20:00", True, "上午小节休市（进程仍在运行）"),
        # ---- 午休补刷窗口 ----
        ("2026-09-01 11:30:00", True, "禁区右边界 11:30（闭区间）"),
        ("2026-09-01 11:30:01", False, "午休补刷窗口起点"),
        ("2026-09-01 12:00:00", False, "午休补刷窗口"),
        ("2026-09-01 13:19:59", False, "13:20 之前"),
        # ---- 日盘下午 ----
        ("2026-09-01 13:20:00", True, "禁区左边界 13:20（闭区间）"),
        ("2026-09-01 15:00:00", True, "禁区右边界 15:00（闭区间）"),
        ("2026-09-01 15:00:01", False, "收盘后补刷窗口"),
        # ---- 夜盘（含跨午夜） ----
        ("2026-09-01 20:49:59", False, "夜盘禁区之前"),
        ("2026-09-01 20:50:00", True, "禁区左边界 20:50"),
        ("2026-09-01 21:05:00", True, "★20:30 自动化超时跨入夜盘"),
        ("2026-09-01 23:59:00", True, "夜盘跨午夜前"),
        ("2026-09-02 01:00:00", True, "★夜盘跨午夜（周三 01:00 归属周二）"),
        ("2026-09-02 02:30:00", True, "夜盘右边界 02:30（闭区间）"),
        ("2026-09-02 02:30:01", False, "凌晨补刷窗口"),
        # ---- 非交易日 ----
        ("2026-09-05 10:00:00", False, "周六（非交易日）"),
        ("2026-09-06 22:00:00", False, "周日夜（无周日晚盘）"),
        ("2026-09-04 22:00:00", True, "周五夜盘"),
        ("2026-09-05 01:00:00", True, "★周五夜盘跨入周六凌晨（归属周五）"),
        # ---- 节假日（默认空表 → 保守误拦） ----
        ("2026-10-01 10:00:00", True, "节假日（默认空表→保守误拦）"),
    ],
)
def test_block_window_matrix(stamp: str, expected: bool, desc: str) -> None:
    """禁写窗口边界矩阵：闭区间边界 + 跨午夜归属 + 非交易日放行。"""
    assert is_cache_rewrite_blocked(pd.Timestamp(stamp)) is expected, desc


def test_holidays_eliminate_false_positive() -> None:
    """传入节假日表可消除「节假日被误拦」的保守副作用。

    默认空表的代价是节假日盘中也被拦（误拦），调用方可传 ``holidays`` 精确化。
    """
    ts = pd.Timestamp("2026-10-01 10:00:00")
    assert is_cache_rewrite_blocked(ts) is True
    assert is_cache_rewrite_blocked(ts, holidays={date(2026, 10, 1)}) is False


def test_unparsable_ts_is_allowed() -> None:
    """无法解析的时刻一律放行——禁区不得因解析失败而扩大。"""
    assert is_cache_rewrite_blocked(None) is False
    assert is_cache_rewrite_blocked("not-a-timestamp") is False


def test_block_sessions_shape() -> None:
    """三条禁区，且夜盘区间带 crosses_midnight。"""
    assert len(CACHE_REWRITE_BLOCK_SESSIONS) == 3
    assert CACHE_REWRITE_BLOCK_SESSIONS[2].crosses_midnight is True


# ===========================================================================
# 二、p22_tail_ext 硬护栏（唯一写入点）
# ===========================================================================
def test_merge_tail_ext_blocks_before_any_io(monkeypatch, tmp_path) -> None:
    """护栏位于函数第一条语句：SystemExit 而非 FileNotFoundError。

    把 ``V8_PATH`` 指向不存在的文件后，若护栏**未生效**会抛 FileNotFoundError；
    抛 SystemExit 即证明护栏先于一切 IO 生效。
    """
    import scripts.p22_tail_ext as p22

    monkeypatch.setattr(p22, "V8_PATH", tmp_path / "does_not_exist.parquet")
    with pytest.raises(SystemExit) as ei:
        p22.merge_tail_ext(now=BLOCKED_TS)
    msg = str(ei.value)
    assert "P1-2" in msg
    assert "未被改写" in msg


def test_merge_tail_ext_allows_outside_window(monkeypatch, tmp_path) -> None:
    """允许窗口内放行：表现为进入 IO 阶段（V8_PATH 不存在 → FileNotFoundError）。"""
    import scripts.p22_tail_ext as p22

    monkeypatch.setattr(p22, "V8_PATH", tmp_path / "does_not_exist.parquet")
    with pytest.raises(FileNotFoundError):
        p22.merge_tail_ext(now=ALLOWED_TS)


def test_force_in_session_bypasses_guard(monkeypatch, tmp_path) -> None:
    """逃生舱：--force-in-session 时放行（同样以进入 IO 为证）。"""
    import scripts.p22_tail_ext as p22

    monkeypatch.setattr(p22, "V8_PATH", tmp_path / "does_not_exist.parquet")
    with pytest.raises(FileNotFoundError):
        p22.merge_tail_ext(force_in_session=True, now=BLOCKED_TS)


def test_main_precheck_exits_2(monkeypatch) -> None:
    """预检 fast-fail：exit 2，且不做任何改动（避免白跑 8 分钟重训）。"""
    import scripts.p22_tail_ext as p22

    monkeypatch.setattr(p22, "is_cache_rewrite_blocked", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["p22_tail_ext.py"])
    with pytest.raises(SystemExit) as ei:
        p22.main()
    assert ei.value.code == 2


def test_main_precheck_skipped_for_single_group(monkeypatch, tmp_path) -> None:
    """--only 单组模式只写 checkpoint，不触碰生产缓存 → 不应被预检拦下。"""
    import scripts.p22_tail_ext as p22

    monkeypatch.setattr(p22, "is_cache_rewrite_blocked", lambda *a, **k: True)
    # CKPT_DIR 指向空目录，确保 checkpoint 不存在而真的走到训练函数
    monkeypatch.setattr(p22, "CKPT_DIR", tmp_path / "ckpt")
    monkeypatch.setattr(
        p22,
        "build_tail_ext_for_group",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TRAIN_REACHED")),
    )
    monkeypatch.setattr(sys, "argv", ["p22_tail_ext.py", "--only", "precious"])
    with pytest.raises(RuntimeError, match="TRAIN_REACHED"):
        p22.main()


# ===========================================================================
# 三、p6_4 预检（K 线融合与信号重建解耦）
# ===========================================================================
def test_p6_4_preflight_blocked() -> None:
    from scripts.p6_4_apply_persisted_dir import _p22_rewrite_blocked

    blocked, banner = _p22_rewrite_blocked(now=BLOCKED_TS)
    assert blocked is True
    assert "P1-2" in banner
    # 横幅必须讲清 K 线融合不受影响，否则运维会误以为整条链路失败
    assert "K 线融合" in banner


def test_p6_4_preflight_allowed() -> None:
    from scripts.p6_4_apply_persisted_dir import _p22_rewrite_blocked

    blocked, banner = _p22_rewrite_blocked(now=ALLOWED_TS)
    assert blocked is False
    assert banner == ""


# ===========================================================================
# 四、启动期自动刷新（signal_refresh）
# ===========================================================================
def _stale_probe() -> list:
    return [sr.FreshnessProbe("c.parquet", "2026-08-01 00:00", fd=5, threshold=0)]


def test_auto_refresh_disabled_does_not_spawn() -> None:
    ok, msg = sr.maybe_auto_refresh(_stale_probe(), enabled=False, now=ALLOWED_TS)
    assert ok is False
    assert "未启用" in msg


def test_auto_refresh_skipped_during_blocked_window(monkeypatch) -> None:
    """禁写窗口内直接跳过，不空耗 timeout_sec 秒再拿到 exit 2。"""
    called = {}

    def boom(*a, **k):  # pragma: no cover - 若被调用即测试失败
        called["ran"] = True
        raise AssertionError("不应在禁写窗口内拉起重建子进程")

    monkeypatch.setattr(sr.subprocess, "run", boom)
    ok, msg = sr.maybe_auto_refresh(_stale_probe(), enabled=True, now=BLOCKED_TS)
    assert ok is False
    assert "P1-2" in msg and "跳过" in msg
    assert "ran" not in called


def test_auto_refresh_never_passes_force_flag(monkeypatch) -> None:
    """自动刷新**永不**自行加 --force-in-session（那会让盘中重写变成默认行为）。"""
    seen: dict = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    ok, _ = sr.maybe_auto_refresh(_stale_probe(), enabled=True, now=ALLOWED_TS)
    assert ok is True
    joined = " ".join(seen["cmd"])
    assert "--skip-eval" in joined
    assert "--force-in-session" not in joined
