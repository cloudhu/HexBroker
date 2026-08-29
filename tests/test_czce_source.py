"""郑商所官方源（``CzceSource``）单测。

夹具取自 2026-08-29 主理人真实联网抓取样例（截取 AP/CF 段 + 小计行），
格式契约固化：管道分隔、千分位逗号、小计行跳过、表头文字定位。
网络层全部 mock，不打真实请求（真实联网冒烟归 p36）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexbroker import HexEmptyDataError, HexNetworkError
from hexbroker.data.sources.czce_source import CzceSource, _to_float

# ---- 真实格式夹具（2026-08-28 CZCE 官方文件节选） --------------------------

CZCE_SAMPLE = (
    "\t\t\t\t郑州商品交易所期货每日行情表(2026-08-28)\n"
    "合约代码|昨结算    |今开盘    |最高价    |最低价    |今收盘    |今结算    "
    "|涨跌1    |涨跌2    |成交量(手)|持仓量    |增减量    |成交额(万元)|交割结算价\n"
    "AP610 |7,438.00  |7,462.00  |7,778.00  |7,428.00  |7,778.00  |7,646.00  "
    "|340.00   |208.00   |199,772   |78,095    |-11,906   |1,527,441.93|\n"
    "小计    |          |          |          |          |          |          "
    "|         |         |328,452   |191,286   |-16,335   |2,483,457.70|\n"
    "CF609 |16,570.00 |16,615.00 |16,920.00 |16,610.00 |16,775.00 |16,760.00 "
    "|205.00   |190.00   |19,722    |46,600    |-5,987    |165,284.52  |\n"
    "CF701 |17,020.00 |17,100.00 |17,345.00 |17,050.00 |17,180.00 |17,200.00 "
    "|160.00   |180.00   |464,467   |604,731   |41,253    |4,555,627.31|\n"
    "CF705 |16,845.00 |16,900.00 |17,150.00 |16,870.00 |16,990.00 |17,030.00 "
    "|145.00   |185.00   |13,683    |41,178    |2,593     |116,519.33  |\n"
)

DAY = pd.Timestamp("2026-08-28")


def _mk_source(monkeypatch, responses: dict[str, object], **kw) -> CzceSource:
    """mock requests.get：URL → FakeResponse | Exception。"""
    class FakeResponse:
        def __init__(self, text: str, status: int = 200):
            self.status_code = status
            self._text = text

        @property
        def content(self):
            return self._text.encode("gbk")

        def raise_for_status(self):
            assert self.status_code == 200

    import requests as requests_mod

    def fake_get(url, timeout=None, headers=None):
        for prefix, resp in responses.items():
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                if resp is None:
                    return FakeResponse("", status=404)
                return FakeResponse(resp)
        raise AssertionError(f"意外 URL: {url}")

    monkeypatch.setattr(requests_mod, "get", fake_get)
    src = CzceSource(save=False, **kw)
    # 新鲜度确定性：注入"今天"，避免测试随真实日历漂移变陈旧
    src.today = date(2026, 9, 1)
    return src


# ---- 纯解析层 ---------------------------------------------------------------

def test_to_float_thousands_and_blank():
    assert _to_float("7,438.00  ") == 7438.0
    assert _to_float("  ") != _to_float("  ")  # NaN
    assert _to_float("") != _to_float("")      # NaN


def test_resolve_symbol_alias():
    assert CzceSource._resolve_symbol("cf0") == "CF"
    assert CzceSource._resolve_symbol("SR0") == "SR"
    assert CzceSource._resolve_symbol("TA0") == "TA"
    assert CzceSource._resolve_symbol("CF701") == "CF701"


def test_parse_table_columns_and_skip_subtotal():
    src = CzceSource(save=False)
    table = src._parse_table(CZCE_SAMPLE, DAY)
    # AP610 + CF609/701/705 = 4 行；小计行被剔除
    assert len(table) == 4
    cf = table[table["_contract"] == "CF701"].iloc[0]
    assert cf["open"] == 17100.0
    assert cf["close"] == 17180.0
    assert cf["open_interest"] == 604731.0
    assert cf["amount_wan"] == 4555627.31
    assert cf["settlement"] == 17200.0
    assert "_contract" in table.columns


# ---- 网络层（mock）----------------------------------------------------------

URL_0828 = CzceSource._build_url(DAY)


def test_fetch_bars_continuous_picks_max_oi(monkeypatch, tmp_path):
    """cf0 → 当日 OI 最大合约 CF701（OI 604,731 > 46,600 > 41,178）。"""
    src = _mk_source(monkeypatch, {URL_0828: CZCE_SAMPLE})
    bf = src.fetch_bars(["cf0"], "2026-08-28", "2026-08-28")
    df = bf.by_symbol("cf0")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["close"] == 17180.0        # CF701 今收盘
    assert row["open_interest"] == 604731.0
    assert row["amount"] == pytest.approx(4555627.31 * 1e4)  # 万元 → 元
    assert df.index.get_level_values("datetime")[0] == DAY  # 00:00 惯例
    assert bf.source == "czce"
    assert (df["raw_close"] == df["close"]).all()  # 官方名义价


def test_fetch_bars_single_contract_direct(monkeypatch, tmp_path):
    src = _mk_source(monkeypatch, {URL_0828: CZCE_SAMPLE})
    bf = src.fetch_bars(["CF609"], "2026-08-28", "2026-08-28")
    df = bf.by_symbol("CF609")
    assert df.iloc[0]["close"] == 16775.0


def test_fetch_bars_404_day_skipped(monkeypatch, tmp_path):
    """08-27 未发布（404）跳过，08-28 有数据 → 仅返回 08-28 一根。"""
    url_27 = CzceSource._build_url(pd.Timestamp("2026-08-27"))
    src = _mk_source(monkeypatch, {url_27: None, URL_0828: CZCE_SAMPLE})
    bf = src.fetch_bars(["cf0"], "2026-08-27", "2026-08-28")
    df = bf.by_symbol("cf0")
    assert list(df.index.get_level_values("datetime")) == [DAY]


def test_fetch_bars_all_404_empty_error(monkeypatch, tmp_path):
    url_27 = CzceSource._build_url(pd.Timestamp("2026-08-27"))
    src = _mk_source(monkeypatch, {url_27: None, URL_0828: None})
    with pytest.raises(HexEmptyDataError):
        src.fetch_bars(["cf0"], "2026-08-27", "2026-08-28")


def test_fetch_bars_non_czce_product_clean_error(monkeypatch, tmp_path):
    """rb0 → 无 RB 合约 → HexEmptyDataError（备源链据此归因降级）。"""
    src = _mk_source(monkeypatch, {URL_0828: CZCE_SAMPLE})
    with pytest.raises(HexEmptyDataError, match="RB"):
        src.fetch_bars(["rb0"], "2026-08-28", "2026-08-28")


def test_fetch_bars_network_error_loud(monkeypatch, tmp_path):
    src = _mk_source(monkeypatch, {URL_0828: ConnectionError("boom")})
    with pytest.raises(HexNetworkError):
        src.fetch_bars(["cf0"], "2026-08-28", "2026-08-28")


def test_non_daily_freq_rejected(monkeypatch, tmp_path):
    src = _mk_source(monkeypatch, {URL_0828: CZCE_SAMPLE})
    from hexbroker import HexConfigError

    with pytest.raises(HexConfigError):
        src.fetch_bars(["cf0"], "2026-08-28", "2026-08-28", freq="60m")
