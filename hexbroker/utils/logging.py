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

# numpy 视为**可选**依赖：缺失时 _json_default 跳过 numpy 分支，
# 绝不因「序列化兜底」这类非关键路径让进程启动失败（R22）。
try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy 缺失属异常环境
    _np = None  # type: ignore[assignment]

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


def _json_default(o: Any) -> Any:
    """JSON 序列化兜底（P0-B：numpy 标量必须还原为**原生类型**，杜绝字符串化）。

    历史缺陷
    --------
    ``json.dumps`` 只认 Python 原生类型。``np.bool_`` **不是** ``bool`` 的子类、
    ``np.int64`` **不是** ``int`` 的子类，二者都会掉进 ``default``；旧实现统一
    ``return str(o)`` → 审计日志落成 ``"False"`` / ``"123"`` **字符串**。

    实证（``data/paper/trades.log``）：同一条成交记录里
    ``"is_open": "False"``（字符串，来自 ``np.bool_``）
    与 ``"is_today_close": true``（原生，Python bool）并列 —— 同一个对象的两个
    布尔字段类型不一致。下游不得不在 ``trade_intent.py:9``（``_as_bool``）与
    ``trade_stats.py:46``（``_norm_bool``）到处加字符串兜底，即为佐证。

    修复
    ----
    numpy 标量一律还原为**原生** ``bool`` / ``int`` / ``float``，由 json.dumps
    二次序列化，产出 ``false`` / ``123`` / ``1.5`` 而非 ``"False"`` / ``"123"``。
    保留 ``_as_bool`` / ``_norm_bool`` 不动（历史日志仍需兼容，属只读兼容层）。

    ⛔ 分支顺序：``np.bool_`` 必须排在数值分支**之前**——numpy 的标量类型关系
    随版本演进，先判 bool 可杜绝「把布尔当数值」的误判。
    """
    if _np is not None:
        if isinstance(o, _np.bool_):
            return bool(o)
        if isinstance(o, _np.integer):
            return int(o)
        if isinstance(o, _np.floating):
            return float(o)
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
