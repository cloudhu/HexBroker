"""L7 回归测试：pytdx 主力/市场硬编码修复。

三处根因（fresh-eyes 实证）：
- F1: K 线持仓量字段名为 ``position``（非 open_interest），旧实现读 open_interest 恒为 0；
- F2: ``_select_main_contract`` 曾硬编码 ``market=30`` 且读 get_instrument_info 的
     open_interest（静态元数据无此字段，恒为 0）→ 退化成「取首个候选」，且 DCE/CZCE 等
     品种永取不到；修复后跨全市场 + 实时 ``chicang`` 选主力，返回 ``(code, market)``；
- F3: ``_fetch_contract_bars`` 曾写死 ``market=30``，修复后跨 _KNOWN_MARKETS 自动发现。

全部用 mock，不触网。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from hexbroker.data.sources.pytdx_source import (
    PytdxSource,
    _product_of_code,
    _KNOWN_MARKETS,
)


def _make_src() -> PytdxSource:
    src = PytdxSource(save=False)
    return src


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def test_product_of_code():
    assert _product_of_code("M2509") == "m"
    assert _product_of_code("MA2509") == "ma"
    assert _product_of_code("cu2401") == "cu"
    assert _product_of_code("SC2509") == "sc"


# ---------------------------------------------------------------------------
# F1: _bars_to_frame 正确读 position -> open_interest
# ---------------------------------------------------------------------------
def test_bars_to_frame_reads_position_as_open_interest():
    bars = [
        {
            "datetime": "2024-01-02", "open": 3000, "high": 3010,
            "low": 2990, "close": 3005, "volume": 100, "amount": 1e6,
            "position": 12345,  # pytdx 真实字段名
        }
    ]
    df = PytdxSource._bars_to_frame(bars, "M2509")
    assert "open_interest" in df.columns
    assert df["open_interest"].iloc[0] == 12345.0


def test_bars_to_frame_prefers_open_interest_when_present():
    bars = [
        {
            "datetime": "2024-01-02", "open": 3000, "high": 3010,
            "low": 2990, "close": 3005, "volume": 100, "amount": 1e6,
            "open_interest": 999, "position": 12345,
        }
    ]
    df = PytdxSource._bars_to_frame(bars, "M2509")
    # 兼容：open_interest 优先，缺失时退 position
    assert df["open_interest"].iloc[0] == 999.0


# ---------------------------------------------------------------------------
# F2: _select_main_contract 跨市场 + 实时 chicang 选主力
# ---------------------------------------------------------------------------
def test_select_main_contract_cross_market_and_chicang():
    api = MagicMock()
    api.get_instrument_count.return_value = 2
    # 两个候选：M2509（DCE, market=47）与 MA2509（CZCE, market=28，不同品种须被过滤）
    info_m = {"category": 3, "market": 47, "code": "M2509", "name": "豆粕2509"}
    info_ma = {"category": 3, "market": 28, "code": "MA2509", "name": "甲醇2509"}
    api.get_instrument_info.return_value = [info_m, info_ma]

    q_m = [{"chicang": 50000, "code": "M2509", "market": 47}]
    q_ma = [{"chicang": 30000, "code": "MA2509", "market": 28}]
    api.get_instrument_quote.side_effect = lambda m, c: q_m if c == "M2509" else q_ma

    src = _make_src()
    src._api = api
    src._connect = lambda: api

    code, market = src._select_main_contract("m")
    assert code == "M2509"
    assert market == 47  # 返回所属市场，使 DCE 主力可正确拉取


def test_select_main_contract_picks_highest_chicang():
    api = MagicMock()
    api.get_instrument_count.return_value = 2
    info_a = {"category": 3, "market": 47, "code": "M2505", "name": "豆粕2505"}
    info_b = {"category": 3, "market": 47, "code": "M2509", "name": "豆粕2509"}
    api.get_instrument_info.return_value = [info_a, info_b]

    q_a = [{"chicang": 80000, "code": "M2505", "market": 47}]
    q_b = [{"chicang": 20000, "code": "M2509", "market": 47}]
    api.get_instrument_quote.side_effect = (
        lambda m, c: q_a if c == "M2505" else q_b
    )

    src = _make_src()
    src._api = api
    src._connect = lambda: api

    code, _ = src._select_main_contract("m")
    assert code == "M2505"  # 持仓最大者为主力


# ---------------------------------------------------------------------------
# F3: _fetch_contract_bars 跨市场自动发现（DCE 合约在 market=30 取不到，须回退 47）
# ---------------------------------------------------------------------------
def test_fetch_contract_bars_auto_discovers_dce_market():
    api = MagicMock()

    def fake_bars(category, market, code, start, take):
        if market == 30:  # SHFE 无 M 合约
            return []
        if market == 47:  # DCE 命中
            return [
                {
                    "datetime": "2024-01-02", "open": 3000, "high": 3010,
                    "low": 2990, "close": 3005, "volume": 100, "amount": 1e6,
                    "position": 12345,
                }
            ]
        return []

    api.get_instrument_bars.side_effect = fake_bars

    src = _make_src()
    src._api = api
    src._connect = lambda: api

    df = src._fetch_contract_bars("M2509", 4, 10)  # market=None → 跨所自动发现
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    assert df["open_interest"].iloc[0] == 12345.0


def test_fetch_contract_bars_known_markets_order():
    # 确认自动发现遍历的市场包含 DCE(47)/CZCE(28)/CFFEX(29)/INE(60)，不止 SHFE(30)
    assert 30 in _KNOWN_MARKETS
    assert 47 in _KNOWN_MARKETS
    assert 28 in _KNOWN_MARKETS
    assert 29 in _KNOWN_MARKETS
    assert 60 in _KNOWN_MARKETS
