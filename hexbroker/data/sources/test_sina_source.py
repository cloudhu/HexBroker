"""SinaSource 单元测试（独立验证，fresh eyes，不替源码解释缺陷）。

覆盖：
- ``_resolve_symbol``（SHFE.cu -> cu0）
- ``_normalize_rows``（列表的列表 -> dict；dict 透传；非法行报错）
- ``_rows_to_frame`` 列/索引/amount&open_interest 补 0.0/可 validate
- ``health_check`` 依赖缺失降级
- ``fetch_bars`` monkeypatch requests 后返回合法 BarFrame
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hexbroker import HexConfigError, HexDataError
from hexbroker.data.schema import BarFrame
from hexbroker.data.sources.sina_source import SinaSource


# ---------------------------------------------------------------------------
# _resolve_symbol
# ---------------------------------------------------------------------------
class TestResolveSymbol:
    @pytest.mark.parametrize(
        "sym,expected",
        [
            ("SHFE.cu", "cu0"),
            ("INE.sc", "sc0"),
            ("cu0", "cu0"),
            ("rb0", "rb0"),
            ("sc0", "sc0"),
            ("CU2609", "cu2609"),
            ("  SHFE.rb  ", "rb0"),  # 容忍空白
        ],
    )
    def test_resolve_symbol(self, sym, expected):
        assert SinaSource._resolve_symbol(sym) == expected


# ---------------------------------------------------------------------------
# _normalize_rows
# ---------------------------------------------------------------------------
class TestNormalizeRows:
    def test_list_of_lists(self):
        data = [
            ["2024-01-02", 1.0, 2.0, 0.5, 1.5, 100],
            ["2024-01-03", 2.0, 3.0, 1.5, 2.5, 120],
        ]
        norm = SinaSource._normalize_rows(data)
        assert norm[0] == {
            "date": "2024-01-02",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100,
        }
        assert norm[1]["volume"] == 120

    def test_dict_passthrough(self):
        data = [
            {
                "date": "2024-01-02",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }
        ]
        assert SinaSource._normalize_rows(data) == data

    def test_invalid_row_raises(self):
        with pytest.raises(HexDataError):
            SinaSource._normalize_rows([42])


# ---------------------------------------------------------------------------
# _rows_to_frame
# ---------------------------------------------------------------------------
class TestRowsToFrame:
    @staticmethod
    def _data():
        return [
            ["2024-01-02", 1.0, 2.0, 0.5, 1.5, 100],
            ["2024-01-03", 2.0, 3.0, 1.5, 2.5, 120],
        ]

    def test_columns_and_index(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        expected = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "open_interest",
            "raw_close",
            "adj_close",
            "limit_up",
            "limit_down",
            "is_rollover",
        ]
        assert list(df.columns) == expected
        assert isinstance(df.index, pd.MultiIndex)
        assert list(df.index.names) == ["symbol", "datetime"]

    def test_amount_oi_padded_zero(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        assert (df["amount"] == 0.0).all()
        assert (df["open_interest"] == 0.0).all()
        assert df["amount"].dtype == float
        assert df["open_interest"].dtype == float

    def test_datetime_sorted_ascending(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        dts = df.index.get_level_values("datetime")
        assert dts.is_monotonic_increasing

    def test_no_nan_required(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        for c in [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "open_interest",
            "raw_close",
            "adj_close",
        ]:
            assert df[c].notna().all(), c

    def test_raw_close_equals_close(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        assert (df["raw_close"] == df["close"]).all()

    def test_validates(self):
        df = SinaSource._rows_to_frame(self._data(), "cu0")
        bf = BarFrame(df=df, freq="1d", source="sina")
        assert bf.validate() is bf


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------
class TestHealthCheck:
    def test_false_when_requests_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "requests", None)
        assert SinaSource().health_check() is False

    def test_true_when_requests_present(self):
        # requests 为可选数据源依赖：缺失时 skip（而非 fail），已装才验证 True
        pytest.importorskip("requests")

        assert SinaSource().health_check() is True


# ---------------------------------------------------------------------------
# fetch_bars（monkeypatch requests）
# ---------------------------------------------------------------------------
class FakeResp:
    """模拟新浪响应。

    同时提供 ``text``（JSONP 包装，1d 新端点路径）与 ``json()``（分钟线旧端点路径），
    使 mock 与真实契约一致 —— 早期只提供 ``json()``，会掩盖两类端点的风格差异。
    """

    def __init__(self, payload, style: str = "jsonp"):
        self._payload = payload
        self._style = style

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def text(self):
        if self._style != "jsonp":
            return json.dumps(self._payload)
        # 真实包装形如：/*<script>location.href='//sina.com';</script>*/\nvar _RB0=(…);
        return (
            "/*<script>location.href='//sina.com';</script>*/\n"
            f"var _XX=({json.dumps(self._payload)});"
        )


class TestFetchBars:
    def test_fetch_bars_returns_valid_barframe(self, monkeypatch):
        data = [
            ["2024-01-02", 1.0, 2.0, 0.5, 1.5, 100],
            ["2024-01-03", 2.0, 3.0, 1.5, 2.5, 120],
        ]
        fake_requests = MagicMock()
        fake_requests.get.return_value = FakeResp(data)

        def fake_require(self):
            return fake_requests

        monkeypatch.setattr(SinaSource, "_require_requests", fake_require)
        src = SinaSource(rate_limit_sleep=0.0, save=False)
        bf = src.fetch_bars(
            ["SHFE.cu"], "2015-01-01", "2026-08-15", freq="1d", save=False
        )
        assert isinstance(bf, BarFrame)
        assert bf.source == "sina"
        assert "cu0" in bf.symbols
        assert bf.df.index.nlevels == 2
        assert bf.validate() is bf

    def test_fetch_bars_unknown_freq_raises(self):
        src = SinaSource(save=False)
        with pytest.raises(HexConfigError):
            src.fetch_bars(["SHFE.cu"], "2015-01-01", "2026-08-15", freq="9d")
