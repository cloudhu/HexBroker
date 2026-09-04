"""P0-C 止损止盈**影子模式**（2026-09-04）。

背景
----
2026-09-04 源码级取证确认（P0-2）：``PaperBroker`` 维护了每品种的
``_stops`` / ``_take_profits`` 档位，但**全仓从未有任何一处拿现价与它们比较过**
—— 计划止损/止盈只是被写进日志与 ``account.json`` 展示，从未参与执行。

本模块实现「影子模式」：**每个 tick 照常判定是否触发，但只记录、不平仓**。
目的是在真正接线之前，先用真实行情回答三个问题：

1. 触发频率多高？（会不会一天被扫十几次）
2. 触发时的浮盈分布如何？（是不是总在最坏的点被扫）
3. 与风控 S1-S5 / 硬止损的触发重叠度？（止损是否只是重复了已有保护）

何时转真实生效，见交付报告「观察指标」一节。

⛔ 铁律
------
影子模式下 **不得调用任何平仓路径，账户状态必须零变更**。
``tests/test_paper_shadow_stops.py`` 对此有逐字段断言（持仓/现金/权益/峰值/成交数）。

字段命名保持 ``stops`` / ``take_profits`` 原名，**不改名**（改名会撕裂
``account.json`` 持久化格式与复盘报表）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..utils.logging import get_logger

log = get_logger("PAPER")

#: 触发类型
KIND_STOP = "STOP"
KIND_TP = "TP"

#: 触发类型 → 日志标记（交付约定：[SHADOW-STOP] / [SHADOW-TP]）
KIND_TAG: dict[str, str] = {KIND_STOP: "[SHADOW-STOP]", KIND_TP: "[SHADOW-TP]"}

#: jsonl 记录字段（顺序即落盘字段顺序）
SHADOW_FIELDS: tuple[str, ...] = (
    "ts",
    "symbol",
    "kind",
    "position",
    "threshold",
    "trigger_price",
    "avg_entry",
    "unrealized_pnl",
    "quote_ts",
)

DEFAULT_PATH = Path("data/paper/shadow_stops.jsonl")

#: 视为「无持仓」的阈值（与 broker.py / scheduler.py 口径一致）
_FLAT_EPS = 1e-12


def evaluate_trigger(
    position: float,
    price: float,
    stop: Optional[float],
    take_profit: Optional[float],
) -> list[str]:
    """判定止损/止盈是否命中，返回按 ``[STOP, TP]`` 顺序排列的 kind 列表。

    规则（价格**等于**阈值即触发，用 ``<=`` / ``>=``）：

    - 多头（position > 0）：``price <= stop`` 止损，``price >= take_profit`` 止盈；
    - 空头（position < 0）：``price >= stop`` 止损，``price <= take_profit`` 止盈；
    - 持仓为 0（|position| <= 1e-12）→ 不触发；
    - 阈值为 ``None`` / 非正 / NaN → 该侧跳过（不触发）。

    极端行情下止损与止盈理论上可能同时命中（跳空穿越两个阈值），
    故返回**列表**而非单值，两条记录都留痕，不静默丢弃。
    """
    hits: list[str] = []
    if position is None or price is None:
        return hits
    try:
        pos = float(position)
        px = float(price)
    except (TypeError, ValueError):
        return hits
    if abs(pos) <= _FLAT_EPS or px != px or px <= 0:
        return hits

    if _usable(stop) and ((pos > 0 and px <= float(stop)) or (pos < 0 and px >= float(stop))):
        hits.append(KIND_STOP)
    if _usable(take_profit) and (
        (pos > 0 and px >= float(take_profit)) or (pos < 0 and px <= float(take_profit))
    ):
        hits.append(KIND_TP)
    return hits


def _usable(threshold: Optional[float]) -> bool:
    """阈值是否可用：非 None、为正、非 NaN。"""
    if threshold is None:
        return False
    try:
        val = float(threshold)
    except (TypeError, ValueError):
        return False
    return val == val and val > 0


class ShadowStopMonitor:
    """止损止盈影子监视器：判定 → 告警 → 落盘 jsonl，**不碰账户**。

    Parameters
    ----------
    path:
        jsonl 落盘路径（默认 ``data/paper/shadow_stops.jsonl``）。
    enabled:
        影子模式开关。``True``（默认）只记录不平仓；``False`` 由调用方转真实平仓
        （本模块自身**始终只记录**，是否平仓由 scheduler 决定，见
        ``TradingScheduler._shadow_stop_sweep``）。
    multiplier_fn:
        ``symbol -> 合约乘数``，用于计算浮动盈亏的**金额**。缺省按 1.0
        （此时 ``unrealized_pnl`` 退化为「每单位名义盈亏」，口径需在报告注明）。
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_PATH,
        enabled: bool = True,
        multiplier_fn: Optional[Callable[[str], float]] = None,
    ) -> None:
        self._path = Path(path)
        self._enabled = bool(enabled)
        self._multiplier_fn = multiplier_fn
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    def multiplier(self, symbol: str) -> float:
        """合约乘数；未注入或异常时退回 1.0（R22：不得因取乘数失败而停摆）。"""
        if self._multiplier_fn is None:
            return 1.0
        try:
            val = float(self._multiplier_fn(symbol))
            return val if val > 0 else 1.0
        except Exception:
            return 1.0

    def scan(
        self,
        symbol: str,
        position: float,
        price: float,
        stop: Optional[float],
        take_profit: Optional[float],
        avg_entry: Optional[float] = None,
        quote_ts: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """判定一个品种并落盘；返回本次写入的记录列表（未命中则空列表）。

        ⛔ 本方法**无任何副作用**：不修改持仓、不产生成交、不触碰账户状态。
        """
        hits = evaluate_trigger(position, price, stop, take_profit)
        if not hits:
            return []

        ts = now if now is not None else datetime.now()
        entry = float(avg_entry) if _usable_or_zero(avg_entry) else 0.0
        pos = float(position)
        px = float(price)
        mult = self.multiplier(symbol)
        pnl = (px - entry) * pos * mult

        records: list[dict[str, Any]] = []
        for kind in hits:
            threshold = float(stop if kind == KIND_STOP else take_profit)
            record: dict[str, Any] = {
                "ts": ts.isoformat(timespec="seconds"),
                "symbol": symbol,
                "kind": kind,
                "position": pos,
                "threshold": threshold,
                "trigger_price": px,
                "avg_entry": entry,
                "unrealized_pnl": pnl,
                "quote_ts": quote_ts.isoformat(timespec="seconds")
                if isinstance(quote_ts, datetime)
                else (str(quote_ts) if quote_ts is not None else None),
            }
            records.append(record)
            self._emit(symbol, kind, record)
        self._write(records)
        return records

    def _emit(self, symbol: str, kind: str, record: dict[str, Any]) -> None:
        """打 WARNING 告警。影子模式下这是**唯一**的外部可见行为。"""
        log.warning(
            "{} symbol={} 持仓={:g} 阈值={:.4f} 触发价={:.4f} 开仓均价={:.4f} "
            "浮动盈亏={:.2f} 行情时间={}（影子模式：仅记录，未平仓）",
            KIND_TAG.get(kind, "[SHADOW]"),
            symbol,
            float(record["position"]),
            float(record["threshold"]),
            float(record["trigger_price"]),
            float(record["avg_entry"]),
            float(record["unrealized_pnl"]),
            record["quote_ts"],
        )

    def _write(self, records: list[dict[str, Any]]) -> None:
        """追加写 jsonl。任何失败都只记日志，绝不向上抛（R22：不新增停摆模式）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lines = "".join(
                json.dumps({k: r.get(k) for k in SHADOW_FIELDS}, ensure_ascii=False) + "\n"
                for r in records
            )
            with self._lock, open(self._path, "a", encoding="utf-8") as fh:
                fh.write(lines)
                fh.flush()
        except Exception as exc:
            log.error("影子止损记录落盘失败 path={} err={}", self._path, exc)


def _usable_or_zero(value: Optional[float]) -> bool:
    """``avg_entry`` 是否可转 float（允许 0，仅排除 None/NaN/不可解析）。"""
    if value is None:
        return False
    try:
        val = float(value)
    except (TypeError, ValueError):
        return False
    return val == val
