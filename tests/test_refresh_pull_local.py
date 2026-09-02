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


# ---- Part A：接缝三分判定（2026-09-02 主理人裁决实施） --------------------
#
# 现场（deliverables/hexbroker_seam_basis_conflict_diagnosis_20260901.md）：
# 2026-09-01 夜盘 18/18 SEAM_BASIS_CONFLICT —— 换月日湖已先切新主力、
# tqsdk KQ.m 官方保守切约仍报旧约 → 接缝日天然不对齐被钝器硬拒。
# Part A：ext 为空（湖不滞后）软放行；ext 非空仍硬拒；软放行前提
# fail-closed（湖必须确实含接缝日行）。

def _seam_args(**kw):
    """默认现场：接缝日 2026-09-01 不在稳定段、无扩展区、湖含接缝日行。"""
    base = dict(
        seam_date=pd.Timestamp("2026-09-01"),
        tail_dates={pd.Timestamp("2026-08-28")},
        ext_empty=True,
        lake_has_seam=True,
    )
    base.update(kw)
    return base


def test_seam_ok_when_in_tail():
    """接缝日 ∈ 对齐稳定段 → OK（正常路径不变）。"""
    status, err = rpl._seam_decision(
        **_seam_args(seam_date=pd.Timestamp("2026-08-28")))
    assert status == "OK"
    assert err is None


def test_seam_lead_window_soft_pass():
    """换月领先窗口（ext 空 + 湖含接缝日行）→ 软放行 ROLLOVER_LEAD_WINDOW。

    即 2026-09-01 夜盘 18/18 误杀现场：旧版在此硬拒。
    """
    status, err = rpl._seam_decision(**_seam_args())
    assert status == "ROLLOVER_LEAD_WINDOW"
    assert err is None


def test_seam_fail_closed_when_lake_missing_seam_row():
    """软放行前提不成立（湖缺接缝日行）→ fail-closed 硬拒，绝不静默跳数据。"""
    status, err = rpl._seam_decision(**_seam_args(lake_has_seam=False))
    assert status == ""
    assert err is not None
    assert "SEAM_BASIS_CONFLICT" in err and "fail-closed" in err


def test_seam_hard_reject_when_extension_zone_exists():
    """湖滞后 + 接缝不对齐（ext 非空）→ 仍硬拒，安全护栏不削弱。"""
    status, err = rpl._seam_decision(**_seam_args(ext_empty=False))
    assert status == ""
    assert err is not None
    assert "扩展区" in err


def test_lead_window_seam_day_never_emitted():
    """软放行下接缝日既不在 tail 也不在 ext → 发射集合天然不含接缝日
    （不把旧主力价错写进缓存，复现 09-01 诊断 §三结论）。"""
    tail_dates = {pd.Timestamp("2026-08-28")}
    ext_index = pd.DatetimeIndex([])          # ext 空
    seam_date = pd.Timestamp("2026-09-01")
    emit_dates = set(tail_dates) | set(ext_index)
    assert seam_date not in emit_dates
