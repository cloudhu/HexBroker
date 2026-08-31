"""P6-5 缓存内容卫生守卫测试（脏行 / 当日覆盖 / 坏值 / 自证循环破局）。

核心设计约束（QA 观察项 2）：**校验不得用被检缓存自己的信号日构造日历**。
``inspect_cache`` 接收外部传入的日历 —— 本测试用迷你日历注入，验证三件事：
① 非交易日信号行被识别为脏行；② 主湖覆盖内的脏行不会因「缓存里有信号」被洗白；
③ 当日覆盖检查的品种清单来自配置而非缓存去重。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from scripts.p6_5_cache_postfill_guard import (
    EXIT_NEED_ATTENTION,
    EXIT_OK,
    EXIT_UNKNOWN,
    inspect_cache,
)

# 迷你日历：02-13（周五）与 02-24~27 是交易日；02-16~23 春节休市不在日历内
_CAL = tuple(
    dt.date(2026, d.month, d.day)
    for d in [
        dt.date(2026, 2, 13),
        dt.date(2026, 2, 24),
        dt.date(2026, 2, 25),
        dt.date(2026, 2, 26),
        dt.date(2026, 2, 27),
    ]
)
SYMS = ["ag0", "rb0"]


def _df(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ts": pd.to_datetime([r[1] for r in rows]),
            "p_up": [r[2] for r in rows],
            "exp_ret": [r[3] for r in rows],
            "is_effective": [r[4] for r in rows],
        }
    )


def test_clean_cache_passes():
    """全交易日信号 + 当日覆盖齐全 + 数值合法 → 三项检查全过。"""
    df = _df(
        [
            ("ag0", "2026-02-24 15:00", 0.8, 1.0, True),
            ("rb0", "2026-02-24 15:00", 0.6, 0.5, True),
        ]
    )
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 24), SYMS, check_today_coverage=True)
    assert rep["unknown"] is None
    assert rep["dirty_rows"] == []
    assert rep["missing_today"] == []
    assert rep["bad_values"] == []


def test_non_trading_day_row_is_flagged():
    """春节休市期间的信号行 → 脏行（主湖无 bar 铁证 + 非可判定交易日）。"""
    df = _df(
        [
            ("ag0", "2026-02-24 15:00", 0.8, 1.0, True),
            ("ag0", "2026-02-17 15:00", 0.7, 0.5, True),  # 休市期间的脏行
        ]
    )
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 24), SYMS, check_today_coverage=False)
    assert [(r["symbol"], r["day"]) for r in rep["dirty_rows"]] == [
        ("ag0", "2026-02-17")
    ]


def test_dirty_row_cannot_self_certify_via_signal_days():
    """⛔ 自证循环破局点：脏行再多、覆盖品种再全，也不能被洗白。

    脏行存在的唯一效果是被报告 —— 本工具不把缓存信号日喂给日历增补，
    因此「缓存里有一行 X 日信号」不构成「X 日是交易日」的证据。
    """
    rows = [
        (sym, "2026-02-17 15:00", 0.7, 0.5, True)  # 全部品种都只有脏行
        for sym in SYMS
    ]
    rep = inspect_cache(_df(rows), _CAL, dt.date(2026, 2, 24), SYMS, False)
    assert len(rep["dirty_rows"]) == len(SYMS)


def test_missing_today_symbols_reported_against_config_list():
    """当日覆盖检查以**配置品种清单**为基准：缓存缺 ag0 的当日行 → 报缺。"""
    df = _df(
        [
            ("rb0", "2026-02-24 15:00", 0.6, 0.5, True),
        ]
    )
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 24), SYMS, check_today_coverage=True)
    assert rep["missing_today"] == ["ag0"]
    # 缺的品种不能靠缓存自己的 symbol 去重蒙混（缓存里根本没有 ag0）
    assert "ag0" not in {str(s) for s in df["symbol"].unique()}


def test_no_today_coverage_check_in_day_session():
    """day session 不要求当日行（08:00 盘前用上一交易日信号）。"""
    df = _df([("rb0", "2026-02-24 15:00", 0.6, 0.5, True)])
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 25), SYMS, check_today_coverage=False)
    assert rep["missing_today"] == []


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan")])
def test_p_up_out_of_range_flagged(bad):
    """p_up 越界 / NaN → 坏值。"""
    df = _df(
        [
            ("ag0", "2026-02-24 15:00", 0.8, 1.0, True),
            ("rb0", "2026-02-24 15:00", bad, 0.5, True),
        ]
    )
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 24), SYMS, False)
    assert len(rep["bad_values"]) == 1
    assert rep["bad_values"][0]["col"] == "p_up"
    assert rep["bad_values"][0]["symbol"] == "rb0"


def test_future_day_row_flagged():
    """未来日期行（数据污染）同样是脏行 —— 即使它是「交易日」。

    判别力锚点：2026-09-15 是周二、不在 holidays_2026、且 > cal_max(02-27)。
    若缺 ref_day 截断，它会走「当日新补」分支被洗白 —— 这正是初版实现的
    真 bug（与 augment_calendar 截断层语义不一致），由本测试先行揪出。
    """
    df = _df(
        [
            ("ag0", "2026-02-24 15:00", 0.8, 1.0, True),
            ("ag0", "2026-09-15 15:00", 0.9, 2.0, True),
        ]
    )
    rep = inspect_cache(df, _CAL, dt.date(2026, 2, 24), SYMS, False)
    assert ("ag0", "2026-09-15") in [(r["symbol"], r["day"]) for r in rep["dirty_rows"]]


def test_empty_and_unparsable_are_unknown():
    """空表 / ts 不可解析 → unknown（不假绿）。"""
    rep_empty = inspect_cache(_df([]), _CAL, dt.date(2026, 2, 24), SYMS, False)
    assert rep_empty["unknown"] is not None
    bad = pd.DataFrame({"symbol": ["ag0"], "ts": ["not-a-date"], "p_up": [0.5]})
    rep_bad = inspect_cache(bad, _CAL, dt.date(2026, 2, 24), SYMS, False)
    assert rep_bad["unknown"] is not None


def test_exit_code_contract():
    """退出码契约：0/2/3/10/11 与 docstring 一致（自动化依赖，勿改值）。"""
    from scripts.p6_5_cache_postfill_guard import (
        EXIT_CONFIG_ERROR,
        EXIT_NOT_TRADING_DAY,
    )

    assert (EXIT_OK, EXIT_NOT_TRADING_DAY, EXIT_CONFIG_ERROR) == (0, 2, 3)
    assert (EXIT_NEED_ATTENTION, EXIT_UNKNOWN) == (10, 11)
