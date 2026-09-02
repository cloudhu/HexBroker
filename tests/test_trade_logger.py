"""结构化审计 JSON 序列化契约单测（P2：bool 统一为 JSON 原生类型）。"""

import io
import uuid
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from hexbroker.paper.logger import TradeLogger
from hexbroker.utils.logging import _json_default, log_structured

# 生产审计日志（相对仓库根定位，不依赖 CWD）
_PROD_LOG = Path(__file__).resolve().parents[1] / "data" / "paper" / "trades.log"


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


def test_test_events_never_land_in_production_audit_log(
    _isolate_production_audit_log: Path,
) -> None:
    """⛔ P1-8 契约：测试产生的审计事件必须落沙箱，**绝不能**进生产 ``trades.log``。

    这是上面两个用例的**隐形前提**：它们只 add 了自己的 StringIO sink 捕获输出，
    但 ``log_structured`` 会**广播到所有已注册 sink**。一旦同批次里有别的用例
    构建过生产组件（注册了生产文件 sink），这两个用例的 ``T000259`` 就会被写进
    生产日志——污染 ``analyze_trades_log`` 的 ``raw_count``/``unique_count``。

    实测（2026-09-01）：``test_size_qty_risk_cap.py`` + 本文件同跑，稳定写入 2 行。
    守卫见 ``tests/conftest.py::_isolate_production_audit_log``。

    ⛔ 本用例故意用 ``TradeLogger()`` 的**默认路径**（= 生产路径）来触发守卫，
    这样才能验证重定向确实生效，而不是验证一个本来就没问题的 tmp_path。
    """
    sentinel = "T_P1_8_" + uuid.uuid4().hex

    TradeLogger()  # 默认 log_file 即生产路径 —— 必须被守卫重定向到沙箱
    log_structured(
        "trade",
        {
            "trade_id": sentinel,
            "symbol": "zz0",
            "is_open": False,
            "is_today_close": True,
            "ts": datetime(2026, 9, 1, 21, 0, 0),
        },
    )

    # ① 正向：哨兵必须出现在沙箱日志里（证明守卫把它接住了）
    sandbox = Path(_isolate_production_audit_log)
    assert sandbox.exists(), "守卫未生效：沙箱日志文件未创建"
    assert sentinel in sandbox.read_text(encoding="utf-8"), (
        f"守卫未生效：哨兵 {sentinel} 未落到沙箱 {sandbox}"
    )

    # ② 反向：哨兵绝不能出现在生产日志里
    if _PROD_LOG.exists():
        assert sentinel not in _PROD_LOG.read_text(encoding="utf-8"), (
            f"⛔ P1-8 回归：测试事件 {sentinel} 泄漏进生产审计日志 {_PROD_LOG}"
        )
