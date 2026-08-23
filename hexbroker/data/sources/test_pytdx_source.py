"""PytdxSource 单元测试（独立验证，fresh eyes，不替源码解释缺陷）。

覆盖：
- ``_resolve_symbol`` 符号解析（SHFE.cu / cu0 / CU2609）
- ``FREQ_TO_PERIOD`` 频率->周期映射（模块级常量，非类属性）
- ``_bars_to_frame`` 列/两级索引/无 NaN/_repair_ohlc 包络修复
- ``_repair_ohlc`` 废 bar 丢弃
- ``health_check`` 依赖缺失降级
- ``fetch_bars`` monkeypatch ``_connect`` 后返回可 validate 的 BarFrame
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest

import hexbroker.data.sources.pytdx_source as pmod
from hexbroker.data.sources.pytdx_source import PytdxSource
from hexbroker import HexConfigError, HexDataError
from hexbroker.data.schema import BarFrame

# 源码中 FREQ_TO_PERIOD 为模块级常量（非类属性），按实际位置引用。
FREQ_TO_PERIOD = pmod.FREQ_TO_PERIOD


# ---------------------------------------------------------------------------
# _resolve_symbol
# ---------------------------------------------------------------------------
class TestResolveSymbol:
    @pytest.mark.parametrize(
        "sym,expected",
        [
            ("SHFE.cu", ("continuous", "cu")),
            ("INE.sc", ("continuous", "sc")),
            ("cu0", ("continuous", "cu")),
            ("rb0", ("continuous", "rb")),
            ("sc0", ("continuous", "sc")),
            ("CU2609", ("contract", "CU2609")),
            ("cu2401", ("contract", "CU2401")),
            ("  SHFE.rb  ", ("continuous", "rb")),  # 容忍空白
        ],
    )
    def test_resolve_symbol(self, sym, expected):
        assert PytdxSource._resolve_symbol(sym) == expected


# ---------------------------------------------------------------------------
# FREQ_TO_PERIOD 映射
# ---------------------------------------------------------------------------
class TestFreqMapping:
    def test_freq_to_period(self):
        assert FREQ_TO_PERIOD == {
            "1d": 4,
            "60m": 3,
            "30m": 2,
            "15m": 1,
            "5m": 0,
            "1m": 7,
        }

    def test_period_values_distinct(self):
        # 各频率映射到不同 pytdx category，避免冲突
        assert len(set(FREQ_TO_PERIOD.values())) == len(FREQ_TO_PERIOD)

    def test_expected_keys_present(self):
        for k in ("1d", "60m", "30m", "15m", "5m", "1m"):
            assert k in FREQ_TO_PERIOD


# ---------------------------------------------------------------------------
# _bars_to_frame
# ---------------------------------------------------------------------------
class TestBarsToFrame:
    @staticmethod
    def _fake_bars():
        return [
            # 包络破坏：open(110) > high(100)，close(105) > high(100)
            {
                "datetime": "2024-01-01",
                "open": 110.0,
                "high": 100.0,
                "low": 95.0,
                "close": 105.0,
                "volume": 1000.0,
                "amount": 1e6,
                "open_interest": 500.0,
            },
            # 合法 bar
            {
                "datetime": "2024-01-02",
                "open": 120.0,
                "high": 130.0,
                "low": 115.0,
                "close": 125.0,
                "volume": 1100.0,
                "amount": 1.1e6,
                "open_interest": 520.0,
            },
        ]

    def test_columns_and_index(self):
        df = PytdxSource._bars_to_frame(self._fake_bars(), "CU2609")
        expected_cols = [
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
        assert list(df.columns) == expected_cols
        assert isinstance(df.index, pd.MultiIndex)
        assert df.index.nlevels == 2
        assert list(df.index.names) == ["symbol", "datetime"]

    def test_no_nan_in_required_cols(self):
        df = PytdxSource._bars_to_frame(self._fake_bars(), "CU2609")
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
            assert df[c].notna().all(), f"必需列 {c} 含 NaN"

    def test_repair_ohlc_envelope(self):
        df = PytdxSource._bars_to_frame(self._fake_bars(), "CU2609")
        row0 = df.iloc[0]
        # 修复后包络必须成立：low <= open,close <= high
        assert row0["low"] <= row0["open"] <= row0["high"]
        assert row0["low"] <= row0["close"] <= row0["high"]
        # 具体修复值：四价极值重定 low/high
        assert float(row0["low"]) == 95.0
        assert float(row0["high"]) == 110.0
        # 第 2 行原本合法，不应被改变
        assert float(df.iloc[1]["high"]) == 130.0

    def test_raw_close_equals_close(self):
        df = PytdxSource._bars_to_frame(self._fake_bars(), "CU2609")
        assert (df["raw_close"] == df["close"]).all()
        assert (df["adj_close"] == df["close"]).all()

    def test_validates_as_barframe(self):
        df = PytdxSource._bars_to_frame(self._fake_bars(), "CU2609")
        bf = BarFrame(df=df, freq="1d", source="pytdx")
        assert bf.validate() is bf

    def test_empty_bars_raises(self):
        with pytest.raises(HexDataError):
            PytdxSource._bars_to_frame([], "CU2609")


# ---------------------------------------------------------------------------
# _repair_ohlc
# ---------------------------------------------------------------------------
class TestRepairOhlc:
    def test_discard_zero_close(self):
        df = pd.DataFrame(
            [
                {"open": 110.0, "high": 120.0, "low": 100.0, "close": 105.0},
                {"open": 1.0, "high": 2.0, "low": 0.5, "close": 0.0},  # 废 bar
            ]
        )
        out = PytdxSource._repair_ohlc(df)
        assert len(out) == 1
        assert float(out.iloc[0]["close"]) == 105.0


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------
class TestHealthCheck:
    def test_health_check_false_when_pytdx_missing(self, monkeypatch):
        # 模拟 pytdx 未安装：import pytdx 应抛 ImportError -> 返回 False
        monkeypatch.setitem(sys.modules, "pytdx", None)
        assert PytdxSource().health_check() is False

    def test_health_check_true_when_pytdx_present(self):
        # pytdx 为可选数据源依赖：缺失时 skip（而非 fail），已装才验证 True
        pytest.importorskip("pytdx")

        assert PytdxSource().health_check() is True


# ---------------------------------------------------------------------------
# fetch_bars（monkeypatch _connect）
# ---------------------------------------------------------------------------
class TestFetchBars:
    @staticmethod
    def _fake_api(bars):
        api = MagicMock()
        api.get_instrument_bars.return_value = bars
        return api

    def test_fetch_bars_contract_returns_valid_barframe(self, monkeypatch):
        bars = [
            {
                "datetime": "2024-01-02",
                "open": 120.0,
                "high": 130.0,
                "low": 115.0,
                "close": 125.0,
                "volume": 1100.0,
                "amount": 1.1e6,
                "open_interest": 520.0,
            },
            {
                "datetime": "2024-01-03",
                "open": 125.0,
                "high": 135.0,
                "low": 120.0,
                "close": 130.0,
                "volume": 1200.0,
                "amount": 1.2e6,
                "open_interest": 530.0,
            },
        ]
        fake_api = self._fake_api(bars)

        def fake_connect(self):
            return fake_api

        monkeypatch.setattr(PytdxSource, "_connect", fake_connect)
        src = PytdxSource(rate_limit_sleep=0.0, save=False)
        bf = src.fetch_bars(
            ["CU2609"], "2015-01-01", "2026-08-15", freq="1d", count=5, save=False
        )
        assert isinstance(bf, BarFrame)
        assert bf.source == "pytdx"
        assert "CU2609" in bf.symbols
        assert bf.df.index.nlevels == 2
        assert bf.validate() is bf

    def test_fetch_bars_unknown_freq_raises(self):
        src = PytdxSource(save=False)
        with pytest.raises(HexConfigError):
            src.fetch_bars(["CU2609"], "2015-01-01", "2026-08-15", freq="9d")
