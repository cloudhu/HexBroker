"""P1-b 补刷守卫回归锁。

全部用例用 ``tmp_path`` 自建配置与缓存——**不依赖本地真实产物**
（2026-08-28 CI 假失败教训：依赖本地才有的文件 → CI 全新 checkout 必然假失败）。

P3-C（2026-08-31）口径同步：新鲜度由「自然日差」改为**交易日历 lag**，
故所有用例必须**注入迷你日历**（``use_calendar`` 夹具），否则 CI 主湖缺失时
日历为空 → 全部判「无法判定」→ 用例结果与真实语义脱节。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from scripts.p6_4_backfill_guard import (
    EXIT_CONFIG_ERROR,
    EXIT_NEED_BACKFILL,
    EXIT_NOT_TRADING_DAY,
    EXIT_OK,
    EXIT_UNKNOWN,
    SESSION_EXPECT_FD,
    judge,
    load_paper_cfg,
    main,
)

# 固定基准日：2026-08-28（周五，交易日；2026-09-25 是最近的休市日）
ASOF = date(2026, 8, 28)
SATURDAY = date(2026, 8, 29)
HOLIDAY = date(2026, 9, 25)  # 中秋

# 迷你日历区间：覆盖 ASOF-44（07-15）至 HOLIDAY 之后，全部工作日（无节假日）
CAL_START = date(2026, 7, 1)
CAL_END = date(2026, 9, 30)


def _write_cfg(tmp_path: Path, cache_names: list[str], holidays: list[str] | None = None) -> Path:
    cfg = {
        "paper": {
            "symbols": {"rb0": {"sessions": {"day": [["09:00", "15:00"]]}}},
            # 空串必须原样保留（用于测"解析为空"分支）——
            # Path(tmp) / "" 会退化成目录本身，不是空串。
            "signal_caches": [(str(tmp_path / n) if n else n) for n in cache_names],
            "freshness_threshold_trading_days": 0,
            "holidays_2026": holidays if holidays is not None else ["2026-09-25"],
        }
    }
    p = tmp_path / "paper.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _write_cache(tmp_path: Path, name: str, ts: date) -> None:
    pd.DataFrame({"symbol": ["rb0"], "ts": [pd.Timestamp(ts)], "p_up": [0.6]}).to_parquet(
        tmp_path / name
    )


# ---------------- 期望 fd 口径（交易日 lag，P3-C） ----------------
def test_session_expect_fd_contract():
    """夜盘当日已收盘 → 期望 lag=-1（缓存应覆盖当日）；日盘前用隔夜信号 → lag=0。

    ⛔ 负值不是异常：lag<0 表示信号比「标准 T+1」更新。守卫传的 ``asof`` 是
    **纯 date**（无时刻 → 视为盘前 → R 回退到上一交易日），故当日信号 = -1。
    """
    assert SESSION_EXPECT_FD["night"] == -1
    assert SESSION_EXPECT_FD["day"] == 0


# ---------------- 核心判定 ----------------
def test_need_backfill_when_lag(use_calendar, tmp_path: Path):
    """夜盘口径（期望 lag=-1）：缓存停在上一交易日（lag=0）→ 判定落后 1 个交易日。"""
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF - timedelta(days=1))  # 08-27 周四
    cfg = _write_cfg(tmp_path, ["main.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=SESSION_EXPECT_FD["night"])
    assert res["verdict"] == "NEED_BACKFILL"
    assert res["system_fd"] == 0
    assert res["lag"] == 1
    assert main(["--config", str(cfg), "--asof", ASOF.isoformat(), "--session", "night"]) == (
        EXIT_NEED_BACKFILL
    )


def test_ok_when_meets_expectation(use_calendar, tmp_path: Path):
    """日盘口径（期望 lag=0）：隔夜信号是**正确**状态，不得误报需补刷。"""
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF - timedelta(days=1))
    cfg = _write_cfg(tmp_path, ["main.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=SESSION_EXPECT_FD["day"])
    assert res["verdict"] == "OK"
    assert res["system_fd"] == 0
    assert main(["--config", str(cfg), "--asof", ASOF.isoformat(), "--session", "day"]) == EXIT_OK


def test_night_session_fresh_signal_is_ok(use_calendar, tmp_path: Path):
    """夜盘口径：缓存已覆盖**当日**（lag=-1）→ OK（20:30 刷新成功态）。"""
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF)
    cfg = _write_cfg(tmp_path, ["main.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=SESSION_EXPECT_FD["night"])
    assert res["verdict"] == "OK"
    assert res["system_fd"] == -1


def test_stale_fallback_cache_does_not_distort_verdict(use_calendar, tmp_path: Path):
    """兜底缓存本就陈旧（lag=31），不得把系统级判定拖成 NEED_BACKFILL。

    系统级 lag 取 **min**（最好的那个），与"只要有一个缓存达标即可交易"一致。
    """
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF - timedelta(days=1))  # lag=0（日盘合规）
    _write_cache(tmp_path, "fallback.parquet", ASOF - timedelta(days=44))
    cfg = _write_cfg(tmp_path, ["main.parquet", "fallback.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=SESSION_EXPECT_FD["day"])
    assert res["verdict"] == "OK", "最差缓存污染了系统级判定"
    assert res["system_fd"] == 0


# ---------------- 无法判定：绝不当 OK ----------------
def test_missing_cache_is_unknown_not_ok(tmp_path: Path):
    """缓存全部缺失 → UNKNOWN + exit 11（假绿缺陷的直接防线）。"""
    cfg = _write_cfg(tmp_path, ["__missing_a.parquet", "__missing_b.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=0)
    assert res["verdict"] == "UNKNOWN"
    assert res["probes"], "UNKNOWN 也必须给出探测明细"
    assert main(["--config", str(cfg), "--asof", ASOF.isoformat()]) == EXIT_UNKNOWN


def test_empty_signal_caches_falls_back_to_default(tmp_path: Path):
    """空 ``signal_caches`` 会回退到 ``DEFAULT_CACHE``（既有设计，与 SignalEngine 同口径）。

    守卫**不推翻**这个兜底——它保证"配置缺失时仍探测生产默认路径"，
    而不是假装没问题。此处固化该行为，防止后人误判为缺陷而改动。
    """
    cfg = _write_cfg(tmp_path, [])
    paper = load_paper_cfg(cfg)
    from hexbroker.diagnostics.signal_refresh import (
        DEFAULT_CACHE,
        resolve_cache_paths,
    )

    assert resolve_cache_paths(paper) == [DEFAULT_CACHE]

    # 默认路径不存在（典型 CI）→ 探测结果必须判为 UNKNOWN，绝不当 OK
    res = judge(paper, ASOF, expect_fd=0)
    assert res["verdict"] in {"UNKNOWN", "NEED_BACKFILL"}


def test_blank_cache_entry_is_unknown(tmp_path: Path):
    """``signal_caches`` 里全是空串 → 解析为空 = 接线错误，必须 UNKNOWN。"""
    cfg = _write_cfg(tmp_path, [""])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=0)
    assert res["verdict"] == "UNKNOWN"
    assert "signal_caches" in res["reason"]


def test_partially_unknown_still_judges_by_known(use_calendar, tmp_path: Path):
    """部分缓存不可判定时，按可判定的那些下结论，并把 unknown 显性列出。"""
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF)  # lag=-1，达标
    cfg = _write_cfg(tmp_path, ["main.parquet", "__missing.parquet"])
    paper = load_paper_cfg(cfg)

    res = judge(paper, ASOF, expect_fd=0)
    assert res["verdict"] == "OK"
    assert res.get("unknown") == ["__missing.parquet"], "unknown 未显性列出"


# ---------------- 非交易日 ----------------
@pytest.mark.parametrize("d", [SATURDAY, HOLIDAY])
def test_non_trading_day_skips(tmp_path: Path, d: date):
    """周末 / 节假日 → exit 2（跳过，不是失败）。"""
    _write_cache(tmp_path, "main.parquet", d - timedelta(days=1))
    cfg = _write_cfg(tmp_path, ["main.parquet"])
    rc = main(["--config", str(cfg), "--asof", d.isoformat(), "--session", "night"])
    assert rc == EXIT_NOT_TRADING_DAY


# ---------------- 配置异常 ----------------
def test_missing_config_file_is_config_error(tmp_path: Path):
    assert main(["--config", str(tmp_path / "nope.yaml")]) == EXIT_CONFIG_ERROR


# ---------------- --json 契约 ----------------
def test_json_output_is_parseable(use_calendar, tmp_path: Path, capsys):
    """自动化靠 --json 做判断，结构必须稳定可解析。"""
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF - timedelta(days=1))
    cfg = _write_cfg(tmp_path, ["main.parquet"])

    rc = main(["--config", str(cfg), "--asof", ASOF.isoformat(), "--session", "night", "--json"])
    assert rc == EXIT_NEED_BACKFILL
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "NEED_BACKFILL"
    assert payload["expect_fd"] == SESSION_EXPECT_FD["night"]  # -1
    assert payload["system_fd"] == 0
    assert payload["lag"] == 1
    assert payload["asof"] == ASOF.isoformat()


def test_quiet_suppresses_stdout(use_calendar, tmp_path: Path, capsys):
    use_calendar(CAL_START, CAL_END)
    _write_cache(tmp_path, "main.parquet", ASOF - timedelta(days=1))
    cfg = _write_cfg(tmp_path, ["main.parquet"])
    main(["--config", str(cfg), "--asof", ASOF.isoformat(), "--quiet"])
    assert capsys.readouterr().out.strip() == ""


# ---------------- 真实生产配置接线 ----------------
def test_real_config_resolves_two_caches():
    """生产配置必须解析出 2 个缓存路径（口径与 SignalEngine 一致）。"""
    paper = load_paper_cfg(Path("configs/paper.yaml"))
    from hexbroker.diagnostics.signal_refresh import resolve_cache_paths

    paths = resolve_cache_paths(paper)
    assert len(paths) == 2, f"生产缓存路径解析异常：{paths}"
