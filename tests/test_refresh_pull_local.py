"""refresh_pull_local._augment_ext_with_collector 回归测试。

重现 2026-08-31 夜盘前刷新 18/18 失败根因：采集湖 POC 无日线数据时
``_load_collector_daily`` 返回无 ``datetime`` 列的空帧，原链式 ``.set_index``
抛 ``KeyError``。新 helper 必须安全回落 tqsdk，不再整批失败。
"""
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "refresh_pull_local.py"
spec = importlib.util.spec_from_file_location("refresh_pull_local", SPEC)
rpl = importlib.util.module_from_spec(spec)
sys.modules["refresh_pull_local"] = rpl
spec.loader.exec_module(rpl)


def _make_ext():
    idx = pd.date_range("2026-08-29", periods=3, freq="D")
    return pd.DataFrame(
        {"close": [8000.0, 8010.0, 8020.0], "open_interest": [100.0, 101.0, 102.0]},
        index=idx,
    )


def test_empty_collector_falls_back_to_tqsdk(monkeypatch):
    """采集湖 POC 无日线 → 回落 tqsdk，且 ext_rows 不变（不抛 KeyError）。"""
    ext = _make_ext()
    lake_last = pd.Timestamp("2026-08-28")
    monkeypatch.setattr(rpl, "_load_collector_daily", lambda *a, **k: pd.DataFrame())
    src, rows = rpl._augment_ext_with_collector(
        "ag0", pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-31"), ext, lake_last
    )
    assert src == "tqsdk"
    assert len(rows) == 3


def test_collector_ext_augments_when_available(monkeypatch):
    """采集湖在 lake_last 之后有数据 → 采纳为 collector 源并补全。"""
    ext = _make_ext()
    lake_last = pd.Timestamp("2026-08-28")
    cidx = pd.date_range("2026-08-29", periods=2, freq="D")
    col = pd.DataFrame(
        {"close": [7990.0, 8005.0], "open_interest": [99.0, 100.0]}, index=cidx
    ).reset_index().rename(columns={"index": "datetime"})
    monkeypatch.setattr(rpl, "_load_collector_daily", lambda *a, **k: col)
    src, rows = rpl._augment_ext_with_collector(
        "ag0", pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-31"), ext, lake_last
    )
    assert src == "collector"
    assert len(rows) == 3  # 2 采集 + 1 tqsdk 兜底补全


def test_ext_empty_returns_safely(monkeypatch):
    """ext 本身为空 → 直接返回空帧，不抛异常。"""
    lake_last = pd.Timestamp("2026-08-28")
    monkeypatch.setattr(rpl, "_load_collector_daily", lambda *a, **k: pd.DataFrame())
    src, rows = rpl._augment_ext_with_collector(
        "ag0", pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-31"),
        pd.DataFrame(), lake_last,
    )
    assert src == "tqsdk"
    assert rows.empty
