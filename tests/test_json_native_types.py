"""P0-B：numpy 标量混入 JSON 审计日志的类型缺陷回归锁（2026-09-04）。

缺陷机理
--------
``SimBroker.execute`` 里::

    is_open = (current == 0.0) or (np.sign(delta) == np.sign(current))

``np.sign(a) == np.sign(b)`` 返回 **``numpy.bool_``**，不是 Python ``bool``。
当 ``current != 0``（减仓 / 平仓 / 反手）时 ``or`` 不会短路到左边的 Python
bool，``is_open`` 就变成 ``numpy.bool_``。

``json.dumps`` 只认 Python 原生类型；``np.bool_`` 不是 ``bool`` 的子类、
``np.int64`` 不是 ``int`` 的子类 → 二者都会掉进 ``default=_json_default``。
旧实现 ``return str(o)`` → 审计日志落成**字符串形态**的布尔值。

实证（``data/paper/trades.log``）：同一条成交记录里 ``is_open`` 是带引号的
字符串、而 ``is_today_close`` 是原生 ``true`` —— 同一对象两个布尔字段类型
不一致。下游因此不得不在 ``trade_intent.py:9``（``_as_bool``）与
``trade_stats.py:46``（``_norm_bool``）到处加字符串兜底；本修复**保留**
那两处兼容层不动（历史日志仍需兼容，属只读兼容层）。

修复两处
--------
1. ``backtest/broker.py:73`` 显式套 ``bool(...)``；
2. ``utils/logging.py::_json_default`` 增加 ``np.bool_`` / ``np.integer`` /
   ``np.floating`` 分支，且**必须返回原生类型**（不是 str），由 json.dumps
   二次序列化产出原生 ``false`` / ``123`` / ``1.5``。

断言一律用 ``type(x) is bool``（而非 ``isinstance``）—— 目的是把「必须是
**原生** bool」钉死；``np.bool_`` 不是原生 bool。
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pytest

from hexbroker.backtest.broker import SimBroker
from hexbroker.utils.logging import _json_default

from test_paper_pipeline import _cost

NOW = datetime(2026, 8, 24, 10, 0)


def _sim() -> SimBroker:
    return SimBroker(_cost(), initial_capital=100_000.0)


# --------------------------------------------------------------------------- #
# 根因：numpy 标量不是 Python 原生类型
# --------------------------------------------------------------------------- #
def test_np_bool_is_not_a_python_bool():
    """根因自证：np.bool_ 不是 bool 的子类 —— 这正是它会掉进 default 的原因。"""
    val = np.sign(-1.0) == np.sign(1.0)
    assert type(val) is not bool
    assert isinstance(val, np.bool_)
    assert bool(val) is False


def test_np_int64_is_not_a_python_int():
    """np.int64 同理，不是 int 的子类 → 也会掉进 default。"""
    val = np.int64(7)
    assert type(val) is not int
    assert isinstance(val, np.integer)


# --------------------------------------------------------------------------- #
# _json_default：numpy 标量必须还原为原生类型
# --------------------------------------------------------------------------- #
def test_json_default_returns_native_bool_for_np_bool():
    """必须返回**原生** bool，不能返回字符串。"""
    out = _json_default(np.bool_(False))
    assert type(out) is bool, f"必须返回原生 bool，实际 {type(out).__name__}"
    assert out is False
    assert type(_json_default(np.bool_(True))) is bool
    assert _json_default(np.bool_(True)) is True


def test_json_default_returns_native_int_for_np_integer():
    for val in (np.int64(-3), np.int32(5), np.uint8(2)):
        out = _json_default(val)
        assert type(out) is int, f"{type(val).__name__} -> 必须返回原生 int"
        assert out == int(val)


def test_json_default_returns_native_float_for_np_floating():
    for val in (np.float64(1.5), np.float32(-0.25)):
        out = _json_default(val)
        assert type(out) is float, f"{type(val).__name__} -> 必须返回原生 float"
        assert out == pytest.approx(float(val))


def test_json_default_datetime_still_iso():
    assert _json_default(datetime(2026, 8, 24, 10, 0)) == "2026-08-24T10:00:00"


def test_json_default_unknown_type_falls_back_to_str():
    """未知类型仍走 str 兜底，绝不抛异常（R22）。"""

    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"

    assert _json_default(_Weird()) == "<weird>"


def test_json_dumps_np_bool_yields_native_false_not_string():
    """端到端：经 json.dumps（走 default）后必须是原生 false。"""
    blob = json.dumps({"is_open": np.bool_(False)}, default=_json_default)
    assert blob == '{"is_open": false}', f"实际产出 {blob}"

    payload = json.loads(blob)
    assert payload["is_open"] is False
    assert type(payload["is_open"]) is bool


def test_json_dumps_np_int64_yields_number_not_string():
    blob = json.dumps({"n": np.int64(42)}, default=_json_default)
    assert json.loads(blob)["n"] == 42
    assert type(json.loads(blob)["n"]) is int


# --------------------------------------------------------------------------- #
# 缺陷现场：SimBroker 成交的 is_open 必须是原生 bool
# --------------------------------------------------------------------------- #
def test_is_open_is_native_bool_on_open():
    """开仓路径（current == 0）→ 左侧短路，本来就是 Python bool（回归保护）。"""
    sim = _sim()
    trade = sim.execute("rb0", 1.0, ref_price=3000.0, timestamp=NOW)
    assert trade is not None
    assert type(trade.is_open) is bool
    assert trade.is_open is True


def test_is_open_is_native_bool_on_close():
    """平仓路径（current != 0）→ 修复前这里是 np.bool_，本例即缺陷现场。"""
    sim = _sim()
    sim.execute("rb0", 1.0, ref_price=3000.0, timestamp=NOW)
    trade = sim.execute("rb0", 0.0, ref_price=3010.0, timestamp=NOW)
    assert trade is not None
    assert type(trade.is_open) is bool, (
        f"is_open 必须是原生 bool，实际 {type(trade.is_open).__name__}"
        "（np.bool_ 会被 json 序列化成带引号的字符串）"
    )
    assert trade.is_open is False


def test_is_open_is_native_bool_on_reduce_and_reverse():
    """减仓与反手两条路径同样要覆盖（current != 0 的两个分支）。"""
    sim = _sim()
    sim.execute("rb0", 2.0, ref_price=3000.0, timestamp=NOW)

    reduce_trade = sim.execute("rb0", 1.0, ref_price=3010.0, timestamp=NOW)
    assert reduce_trade is not None
    assert type(reduce_trade.is_open) is bool
    assert reduce_trade.is_open is False

    reverse_trade = sim.execute("rb0", -1.0, ref_price=3020.0, timestamp=NOW)
    assert reverse_trade is not None
    assert type(reverse_trade.is_open) is bool


def test_trade_record_json_has_native_bools_for_both_flags():
    """审计 JSON 里 is_open 与 is_today_close 必须**同为**原生布尔。

    实证缺陷现场：同一条记录里 is_open 是带引号的字符串，
    而 is_today_close 是原生 true。
    """
    sim = _sim()
    sim.execute("rb0", 1.0, ref_price=3000.0, timestamp=NOW)
    trade = sim.execute("rb0", 0.0, ref_price=3010.0, timestamp=NOW)
    assert trade is not None

    record = trade.to_dict() if hasattr(trade, "to_dict") else dict(vars(trade))
    payload = json.loads(json.dumps(record, default=_json_default))
    assert payload["is_open"] is False, f"实际 {payload['is_open']!r}"
    assert type(payload["is_open"]) is bool
    assert type(payload["is_today_close"]) is bool, "两个布尔字段类型必须一致"


def test_serializing_np_bool_never_produces_quoted_booleans():
    """反证：产出里不得再出现带引号的布尔字符串。"""
    blob = json.dumps({"a": np.bool_(False), "b": np.bool_(True)}, default=_json_default)
    assert '"False"' not in blob
    assert '"True"' not in blob
    assert blob == '{"a": false, "b": true}'
