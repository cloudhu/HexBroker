"""结构化审计 JSON 序列化契约单测（P2：bool 统一为 JSON 原生类型）。"""

import io
from datetime import date, datetime

from loguru import logger

from hexbroker.utils.logging import _json_default, log_structured


def test_json_default_bool_and_datetime() -> None:
    """_json_default：bool 保持原生（防御分支）、datetime→ISO、其余→str。"""
    assert _json_default(False) == "false"
    assert _json_default(True) == "true"
    assert _json_default(datetime(2026, 8, 24, 9, 5)) == "2026-08-24T09:05:00"
    assert _json_default(date(2026, 8, 24)) == "2026-08-24"
    o = object()
    assert _json_default(o) == str(o)


def test_log_structured_serializes_bool() -> None:
    """log_structured 产出 JSON 中布尔字段为 true/false（非 "True"/"False" 字符串）。

    回归保护：历史日志曾因旧序列化路径把 is_open 写成字符串 "False"，
    导致下游按 truthy 判断误读为开仓（见 trade_flip_analysis_20260824 Bug B）。
    """
    buf = io.StringIO()
    sink_id = logger.add(buf, format="{message}", level="INFO")
    try:
        log_structured(
            "trade",
            {
                "trade_id": "T000259",
                "symbol": "ag0",
                "is_open": False,
                "is_today_close": True,
            },
        )
    finally:
        logger.remove(sink_id)
    out = buf.getvalue()
    assert '"is_open": false' in out
    assert '"is_today_close": true' in out
    # 明确禁止字符串化形态
    assert '"is_open": "False"' not in out
    assert '"is_today_close": "True"' not in out


def test_log_structured_keeps_datetime_iso() -> None:
    """datetime 字段序列化为 ISO 字符串（可解析）。"""
    buf = io.StringIO()
    sink_id = logger.add(buf, format="{message}", level="INFO")
    try:
        log_structured("trade", {"ts": datetime(2026, 8, 24, 9, 5, 30)})
    finally:
        logger.remove(sink_id)
    assert "2026-08-24T09:05:30" in buf.getvalue()
