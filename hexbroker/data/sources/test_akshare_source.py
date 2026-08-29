"""AkshareSource 单元测试。

背景：该源此前**从未跑通过**（按英文列名 ``{"date": "datetime"}`` 做 rename，
对 akshare 实际的中文列名无效 → ``KeyError: 'datetime'``），而项目里根本没有
``test_akshare_source.py``，缺陷因此长期被掩盖。本文件补齐离线单测。

联网冒烟（真实调用 akshare）另见 ``scripts/p36_smoke_sources.py``，
不放在单测里以免 CI 依赖外网。
"""

from __future__ import annotations

import sys
from datetime import date
from types import ModuleType
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hexbroker import HexConfigError, HexDataError, HexEmptyDataError, HexStaleDataError
from hexbroker.data.schema import BarFrame
from hexbroker.data.sources.akshare_source import AkshareSource

# akshare 真实返回的中文列名（2026-08 实测 futures_main_sina）
CN_COLS = ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "持仓量", "动态结算价"]


def _ak_payload(rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=CN_COLS)


def _fake_ak(df: pd.DataFrame | None):
    """构造注入 ``sys.modules`` 的假 akshare 模块。"""
    mod = ModuleType("akshare")
    mod.futures_main_sina = MagicMock(return_value=df)  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def good_payload():
    return _ak_payload(
        [
            ["2026-08-26", 3071.0, 3090.0, 3057.0, 3076.0, 725416, 1386273, 3070.0],
            ["2026-08-27", 3071.0, 3090.0, 3071.0, 3088.0, 650663, 1254003, 3080.0],
            ["2026-08-28", 3081.0, 3129.0, 3081.0, 3112.0, 862248, 1100758, 3109.0],
        ]
    )


# ---------------------------------------------------------------------------
# 符号解析
# ---------------------------------------------------------------------------
class TestResolveSymbol:
    @pytest.mark.parametrize(
        "sym,expected",
        [
            ("rb0", "RB0"),
            ("RB0", "RB0"),
            ("ag", "AG0"),  # 无后缀自动补主力连续
            ("SHFE.cu", "CU0"),
            ("DCE.i", "I0"),
        ],
    )
    def test_resolve(self, sym, expected):
        assert AkshareSource._resolve_symbol(sym) == expected

    @pytest.mark.parametrize("sym", ["RB2609", "SHFE.cu2609", "J2601"])
    def test_contract_month_rejected(self, sym):
        """futures_main_sina 只支持主力连续，具体月份合约须显式报错。"""
        with pytest.raises(HexConfigError):
            AkshareSource._resolve_symbol(sym)


class TestSymbolKey:
    @pytest.mark.parametrize(
        "sym,expected",
        [("rb0", "rb0"), ("RB0", "rb0"), ("SHFE.cu", "cu0"), ("ag", "ag0")],
    )
    def test_key(self, sym, expected):
        assert AkshareSource._symbol_key(sym) == expected


# ---------------------------------------------------------------------------
# fetch_bars（monkeypatch akshare）
# ---------------------------------------------------------------------------
class TestFetchBars:
    def test_chinese_columns_mapped(self, monkeypatch, good_payload):
        fake = _fake_ak(good_payload)
        monkeypatch.setitem(sys.modules, "akshare", fake)

        src = AkshareSource(save=False)
        bf = src.fetch_bars(["rb0"], "2026-08-26", "2026-08-28", freq="1d")
        assert isinstance(bf, BarFrame)
        assert bf.source == "akshare"
        assert bf.symbols == ["rb0"]
        assert bf.length == 3

        # 中文列 -> 标准列，且持仓量/结算价被真实填充（旧实现硬编码 0.0）
        assert float(bf.df["open_interest"].iloc[-1]) == 1100758.0
        assert float(bf.df["settlement"].iloc[-1]) == 3109.0

    def test_symbol_normalized_from_exchange_form(self, monkeypatch, good_payload):
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(good_payload))
        bf = AkshareSource(save=False).fetch_bars(["SHFE.cu"], "2026-08-26", "2026-08-28")
        assert bf.symbols == ["cu0"]

    def test_freq_other_than_1d_rejected(self, monkeypatch, good_payload):
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(good_payload))
        with pytest.raises(HexConfigError):
            AkshareSource(save=False).fetch_bars(["rb0"], "2026-08-26", "2026-08-28", freq="60m")

    def test_unknown_columns_raise_clear_error(self, monkeypatch):
        """列名无法识别时必须明确报错，而不是 KeyError 冒泡。"""
        bad = pd.DataFrame([["2026-08-28", 1, 2, 3, 4]], columns=["foo", "a", "b", "c", "d"])
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(bad))
        with pytest.raises(HexDataError, match="返回列名无法识别"):
            AkshareSource(save=False).fetch_bars(["rb0"], "2026-08-26", "2026-08-28")

    def test_empty_result_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(_ak_payload([])))
        with pytest.raises(HexEmptyDataError):
            AkshareSource(save=False).fetch_bars(["rb0"], "2026-08-26", "2026-08-28")

    def test_stale_result_raises(self, monkeypatch):
        """停摆事故模式：源停更在 2026-06-29，请求窗口到 08-28 → 必须抛陈旧错误。"""
        stale = _ak_payload(
            [
                ["2026-06-26", 1.0, 2.0, 0.5, 1.5, 100, 1000, 1.4],
                ["2026-06-29", 1.0, 2.0, 0.5, 1.5, 100, 1000, 1.4],
            ]
        )
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(stale))
        src = AkshareSource(save=False)
        src.today = date(2026, 8, 29)  # 确定性注入
        # start 需覆盖到陈旧数据本身，否则会先被裁成 0 行而命中"空结果"门禁
        # （空门禁优先于陈旧门禁，这是正确行为）
        with pytest.raises(HexStaleDataError) as ei:
            src.fetch_bars(["rb0"], "2026-06-20", "2026-08-28")
        assert ei.value.latest == "2026-06-29"

    def test_historical_backfill_not_killed_by_freshness(self, monkeypatch):
        """历史回填窗口不应被新鲜度门禁误杀。"""
        old = _ak_payload(
            [
                ["2020-03-02", 1.0, 2.0, 0.5, 1.5, 100, 1000, 1.4],
                ["2020-03-06", 1.0, 2.0, 0.5, 1.5, 100, 1000, 1.4],
            ]
        )
        monkeypatch.setitem(sys.modules, "akshare", _fake_ak(old))
        src = AkshareSource(save=False)
        src.today = date(2026, 8, 29)
        bf = src.fetch_bars(["rb0"], "2020-03-01", "2020-03-06")
        assert bf.length == 2
