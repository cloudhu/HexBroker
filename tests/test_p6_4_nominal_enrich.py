"""P1-c 名义价回填（``enrich_raw_close``）单测。

背景（37 号 §4.12 / §4.15 遗留）
--------------------------------
pandadata ``close_pcr`` 只供复权价，p6_4 管线原映射
``raw_close = adj_close = close``，湖内 nominal 校准信息完全丢失，
"名义价冒充"（P0-10 定罪的 cu0/rb0 2023）无法湖内自检。

本修复在 parse 阶段用备源（sina/akshare，save=False）名义价回填
``raw_close``，失败大声降级（原样返回 + 归因 note），不阻断管线。

为何单测而非跑真数据
--------------------
stage_parse 的 happy path 会写生产 parquet（data/raw/processed），
真数据验证会污染生产湖 —— 铁律禁止。故只测纯函数 + 注入 fake fetcher。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS / "p6_4_fill_gaps.py"


def _load_module():
    """scripts/ 不是包，按文件路径加载。"""
    spec = importlib.util.spec_from_file_location("p6_4_fill_gaps", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p6_4_fill_gaps"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class FakePull:
    def __init__(self, close: pd.Series, source: str = "sina",
                 warnings: list[str] | None = None):
        self.close = close
        self.source = source
        self.warnings = warnings or []


class FakeFetcher:
    """鸭子类型：只需 fetch_raw。"""

    def __init__(self, pull: FakePull | None = None, exc: Exception | None = None):
        self.pull = pull
        self.exc = exc
        self.calls: list[tuple[list[str], str, str]] = []

    def fetch_raw(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        if self.exc is not None:
            raise self.exc
        return {symbols[0]: self.pull}


def _make_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    """模拟 normalize_new_df 的输出（raw_close = adj_close = close 的伪映射）。"""
    return pd.DataFrame({
        "symbol": ["cu0"] * len(dates),
        "datetime": pd.to_datetime(dates),
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [100.0] * len(dates),
        "amount": [0.0] * len(dates),
        "open_interest": [1000.0] * len(dates),
        "raw_close": list(closes),
        "adj_close": list(closes),
        "limit_up": False,
        "limit_down": False,
        "is_rollover": False,
    })


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------
def test_enrich_success_full_coverage():
    dates = ["2026-08-24", "2026-08-25", "2026-08-26"]
    df = _make_df(dates, [100.0, 101.0, 102.0])
    nominal = pd.Series(
        [88.0, 88.5, 89.0],
        index=pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26"]),
    )
    fetcher = FakeFetcher(FakePull(nominal, source="sina"))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    # raw_close = 名义价；adj_close/close 不动
    assert out["raw_close"].tolist() == [88.0, 88.5, 89.0]
    assert out["adj_close"].tolist() == [100.0, 101.0, 102.0]
    assert out["close"].tolist() == [100.0, 101.0, 102.0]
    assert any("回填成功" in n and "sina" in n and "3/3" in n for n in notes)
    # fetch_raw 收到了正确的品种与日期窗口
    assert fetcher.calls[0][0] == ["cu0"]
    assert fetcher.calls[0][1] == "2026-08-24"
    assert fetcher.calls[0][2] == "2026-08-26"


def test_enrich_partial_alignment_fills_unmatched_with_adj_copy():
    """部分对齐（≥ min_coverage）：未对齐行保留 adj 复制品，不产生 NaN。"""
    dates = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"]
    df = _make_df(dates, [100.0, 101.0, 102.0, 103.0])
    nominal = pd.Series(
        [88.0, 88.5, 89.0],
        index=pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26"]),
    )  # 缺 08-27 → 覆盖 3/4 = 75% ≥ 0.9? 否 → 改 4 行里缺 0 行的场景不成立
    # 用 10 行缺 1 行的场景验证（90% 恰好达阈值）
    dates10 = [f"2026-08-{d:02d}" for d in range(10, 20)]
    df10 = _make_df(dates10, [float(c) for c in range(10, 20)])
    nom10 = pd.Series(
        [float(c * 2) for c in range(10, 19)],  # 缺最后一天
        index=pd.to_datetime(dates10[:-1]),
    )
    fetcher = FakeFetcher(FakePull(nom10, source="sina"))

    out, notes = mod.enrich_raw_close(df10, "cu0", fetcher=fetcher)

    assert out["raw_close"].tolist()[:9] == [float(c * 2) for c in range(10, 19)]
    assert out["raw_close"].iloc[-1] == df10["raw_close"].iloc[-1]  # 未对齐行保留
    assert not out["raw_close"].isna().any()
    assert any("9/10" in n and "未对齐行保留" in n for n in notes)


def test_enrich_dedups_duplicate_index():
    dates = ["2026-08-24", "2026-08-25"]
    df = _make_df(dates, [100.0, 101.0])
    nominal = pd.Series(
        [88.0, 88.8, 88.5],  # 同一天两条，取后者
        index=pd.to_datetime(["2026-08-24", "2026-08-24", "2026-08-25"]),
    )
    fetcher = FakeFetcher(FakePull(nominal))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    assert out["raw_close"].tolist() == [88.8, 88.5]


def test_enrich_carries_pull_warnings_into_notes():
    dates = ["2026-08-24", "2026-08-25"]
    df = _make_df(dates, [100.0, 101.0])
    nominal = pd.Series([88.0, 88.5], index=pd.to_datetime(dates))
    fetcher = FakeFetcher(FakePull(nominal, warnings=["交叉校验偏差 1.2e-6"]))

    _, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    assert any("交叉校验偏差" in n for n in notes)


# ---------------------------------------------------------------------------
# 失败路径（原样返回 + 归因 note，静默即事故）
# ---------------------------------------------------------------------------
def test_enrich_fetcher_raises_returns_df_unchanged():
    dates = ["2026-08-24", "2026-08-25"]
    df = _make_df(dates, [100.0, 101.0])
    fetcher = FakeFetcher(exc=RuntimeError("全部备源失败（sina, akshare）"))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    pd.testing.assert_frame_equal(out, df)  # 原样返回
    assert any("回填失败" in n and "RuntimeError" in n for n in notes)


def test_enrich_empty_pull_returns_df_unchanged():
    dates = ["2026-08-24", "2026-08-25"]
    df = _make_df(dates, [100.0, 101.0])
    fetcher = FakeFetcher(FakePull(pd.Series(dtype=float), source="akshare"))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    pd.testing.assert_frame_equal(out, df)
    assert any("回填失败" in n and "返回空" in n for n in notes)


def test_enrich_low_coverage_returns_df_unchanged():
    """覆盖率 < 90%：all-or-nothing，整段拒绝回填。"""
    dates = [f"2026-08-{d:02d}" for d in range(10, 20)]
    df = _make_df(dates, [float(c) for c in range(10, 20)])
    nominal = pd.Series(
        [88.0, 88.5],  # 仅 2/10 = 20%
        index=pd.to_datetime(["2026-08-10", "2026-08-11"]),
    )
    fetcher = FakeFetcher(FakePull(nominal, source="sina"))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    pd.testing.assert_frame_equal(out, df)
    assert any("覆盖率 20.0%" in n and "回填失败" in n for n in notes)


def test_enrich_empty_df_short_circuits():
    df = pd.DataFrame(columns=mod.SCHEMA_COLUMNS)
    fetcher = FakeFetcher()

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher)

    assert out.empty
    assert fetcher.calls == []  # 未发起网络请求
    assert any("空帧" in n for n in notes)


def test_enrich_min_coverage_param_is_honored():
    """显式放宽 min_coverage=0.5 时，50% 覆盖可通过。"""
    dates = [f"2026-08-{d:02d}" for d in range(10, 14)]  # 4 行
    df = _make_df(dates, [1.0, 2.0, 3.0, 4.0])
    nominal = pd.Series(
        [8.0, 8.5],  # 2/4 = 50%
        index=pd.to_datetime(["2026-08-10", "2026-08-11"]),
    )
    fetcher = FakeFetcher(FakePull(nominal))

    out, notes = mod.enrich_raw_close(df, "cu0", fetcher=fetcher, min_coverage=0.5)

    assert out["raw_close"].tolist() == [8.0, 8.5, 3.0, 4.0]
    assert any("回填成功" in n for n in notes)


# ---------------------------------------------------------------------------
# 既有伪映射的存在性守护（enrich 之前的形态，文档化 P1-c 修复对象）
# ---------------------------------------------------------------------------
def test_normalize_new_df_still_produces_adj_copy_semantics():
    """normalize_new_df 输出的 raw_close 恒等于 adj_close —— 伪映射仍在，
    由 enrich_raw_close 负责矫正（若未来 normalize 改真名义价，本测试提醒同步）。"""
    from datetime import date as _date

    payload = {
        "result": {
            "type": "dataframe",
            "columns": ["underlying_symbol", "date", "open", "high", "low",
                        "close", "volume", "open_interest"],
            "rows": [
                ["CU", "20260825", 100.0, 101.0, 99.0, 100.5, 1000.0, 2000.0],
                ["CU", "20260826", 100.6, 101.6, 99.6, 101.1, 1100.0, 2100.0],
            ],
        }
    }
    df = mod.load_persisted_rows(
        _write_json_tmp(payload)
    )
    out = mod.normalize_new_df(df, "CU", "cu0", _date(2026, 8, 25), _date(2026, 8, 26))

    assert len(out) == 2
    assert (out["raw_close"] == out["adj_close"]).all()
    assert (out["adj_close"] == out["close"]).all()


def _write_json_tmp(payload: dict) -> Path:
    import tempfile
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp
