"""统一日志（loguru）。

层级前缀标签：``[DATA] [FEAT] [FCST] [EVO] [RL] [RISK] [BT] [EVAL] [LIVE]``。
关键决策（RiskDecision / Fill / DriftEvent）写结构化 JSON lines 供审计。

用法::

    from hexbroker.utils.logging import get_logger
    log = get_logger("RISK")
    log.info("硬止损触发", reason_codes=["HARD_STOP"])
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger as _loguru_logger

_LOGGER_CONFIGURED = False
_JSON_SINK_PATH: Optional[Path] = None


class TaggedLogger:
    """带标签的前缀日志器，方法签名与 loguru 一致。"""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def _msg(self, message: str) -> str:
        return f"[{self._tag}] {message}"

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        _loguru_logger.debug(self._msg(message), *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        _loguru_logger.info(self._msg(message), *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        _loguru_logger.warning(self._msg(message), *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        _loguru_logger.error(self._msg(message), *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        _loguru_logger.exception(self._msg(message), *args, **kwargs)


def get_logger(tag: str = "ROOT") -> TaggedLogger:
    """获取带层级前缀的日志器。"""
    return TaggedLogger(tag)


def _json_default(o: Any) -> str:
    """JSON 序列化兜底（P2：bool 统一为 JSON 原生类型，杜绝字符串化）。

    ``bool`` 是 JSON 原生类型，``json.dumps`` 不会调用 default——此函数**不处理
    bool**，确保 ``is_open``/``is_today_close`` 等布尔字段始终序列化为
    ``true/false`` 而非 ``"True"/"False"`` 字符串（历史日志曾因旧序列化路径
    产出字符串导致下游解析误判）。datetime/date → ISO 字符串，其余 → str。
    """
    if isinstance(o, bool):
        return "true" if o else "false"  # 防御：理论不可达（json.dumps 原生处理 bool）
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def log_structured(event: str, payload: dict[str, Any]) -> None:
    """写一条结构化 JSON lines 审计日志（风控决策 / 成交 / 漂移事件）。"""
    record = {"event": event, **payload}
    _loguru_logger.bind(audit=True).info(json.dumps(record, default=_json_default, ensure_ascii=False))


def init_logging(run_id: str, artifacts_dir: str = "artifacts", level: str = "INFO") -> None:
    """初始化日志：控制台 + ``artifacts/logs/{run_id}.log``。"""
    global _LOGGER_CONFIGURED, _JSON_SINK_PATH
    if _LOGGER_CONFIGURED:
        return
    _loguru_logger.remove()
    _loguru_logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | {message}"
        ),
    )
    log_path = Path(artifacts_dir) / "logs" / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _loguru_logger.add(
        str(log_path),
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation="50 MB",
        encoding="utf-8",
    )
    _JSON_SINK_PATH = log_path
    _LOGGER_CONFIGURED = True
