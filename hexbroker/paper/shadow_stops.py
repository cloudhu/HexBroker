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
#: P2-3（2026-09-06）新增 ``event_id`` / ``first_hit`` —— 事件语义：
#: 一次连续的触发状态段（价格持续停留在阈值一侧，中间**从未**离开触发区）
#: 属于**同一个事件**，共用一个 ``event_id``；只有进入触发区的那一 tick
#: 才开新事件。落盘时首条标 ``first_hit=True``，其余为 ``False``。
#: 之所以必须在去重窗口之外再加这层语义：300s 去重只控制**写盘节奏**，
#: 一个持续 6h 的触发段仍会产生 ~72 行 —— 若分析侧直接 count 行数回答
#: 「止损触发了几次」，会高估一到两个数量级，而这份 jsonl 是 09-21
#: 「是否转真实平仓」的唯一决策依据。
SHADOW_FIELDS: tuple[str, ...] = (
    "ts",
    "event_id",
    "first_hit",
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

#: 去重窗口默认值（秒）。同一 (symbol, kind) 在最近 N 秒内已 emit 过则跳过本轮，
#: 避免每个 tick 都写一条把 jsonl 刷成噪声。默认 300s 与账户快照心跳对齐。
#: 窗口**外**再次穿越阈值会重新 emit（真·二次穿越必须能触发，不能漏）。
#: 只做「时间窗内去重」，不引入「只记极值」之类会改变观察语义的规则。
DEFAULT_DEDUP_WINDOW_SEC = 300.0


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
    dedup_window_sec:
        去重窗口（秒）。同一 ``(symbol, kind)`` 在最近 ``dedup_window_sec`` 秒内
        已 emit 过一次，则本轮静默跳过（不写第二条、不打第二条告警）；窗口外
        再次穿越阈值会**重新** emit。设为 ``0`` / 非正可关闭去重（始终 emit）。
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_PATH,
        enabled: bool = True,
        multiplier_fn: Optional[Callable[[str], float]] = None,
        dedup_window_sec: float = DEFAULT_DEDUP_WINDOW_SEC,
    ) -> None:
        self._path = Path(path)
        self._enabled = bool(enabled)
        self._multiplier_fn = multiplier_fn
        self._dedup_window_sec = float(dedup_window_sec)
        #: 上次成功 emit 的 epoch 秒，键 = (symbol, kind)
        self._last_emit: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        # ---- P2-3 事件语义状态（与去重哨兵相互独立，各用各的锁，永不同持）----
        #: 上一 tick 该 (symbol, kind) 是否处于触发状态（事件边界判定依据）
        self._in_trigger: dict[tuple[str, str], bool] = {}
        #: 当前事件段状态：{"event_id": str, "pending_first": bool}
        #: pending_first=True 表示本事件段尚未落盘过任何记录
        self._event_state: dict[tuple[str, str], dict[str, Any]] = {}
        #: 事件段单调序号（保证 event_id 绝对唯一，跨进程重启即归零——
        #: event_id 同时携带 ts，故重启后也不会与旧 id 冲突）
        self._event_seq = 0
        #: 事件状态专用锁。与 ``_lock`` 职责分离且**永不嵌套持有**：
        #: 事件边界判定拿本锁，去重哨兵/落盘拿 ``_lock``，两段临界区互不包含。
        self._event_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dedup_window_sec(self) -> float:
        return self._dedup_window_sec

    def reset_dedup(self) -> None:
        """清空去重状态（测试用 / 跨会话重扫时手动复位）。

        ⛔ 事件语义状态（``_in_trigger`` / ``_event_state``）必须同步清空，
        否则复位后第一段触发会被误判成「旧事件的延续」。
        """
        with self._lock:
            self._last_emit.clear()
        with self._event_lock:
            self._in_trigger.clear()
            self._event_state.clear()

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
            # ⛔ P2-3：本 tick 全部 kind 都不在触发区 → 在跑的（若有）事件段
            # 必须在此关闭。绝不能因提前 return 跳过 —— 否则「价格离开触发区」
            # 这个事件边界永远丢失，旧事件会把离开后的再次触发并成同一段
            # （两次真实触发被算成一次，低估事件数）。新测试
            # test_new_event_after_price_leaves_trigger_zone 抓的正是这条。
            self._mark_event_closed(symbol, set())
            return []

        ts = now if now is not None else datetime.now()
        now_epoch = ts.timestamp()
        entry = float(avg_entry) if _usable_or_zero(avg_entry) else 0.0
        pos = float(position)
        px = float(price)
        mult = self.multiplier(symbol)
        pnl = (px - entry) * pos * mult

        records: list[dict[str, Any]] = []
        for kind in hits:
            # ---- P2-3 事件边界判定（tick 级触发状态连续性，先于去重）----
            event_id, first_hit = self._resolve_event(symbol, kind, ts, now_epoch)
            # 去重窗口：同一 (symbol, kind) 最近 dedup_window_sec 内已 emit 过则跳过，
            # 不再 build / emit / 落盘第二条。窗口外再次穿越阈值则重新 emit。
            if not self._should_emit(symbol, kind, now_epoch):
                log.debug(
                    "影子止损去重（窗口内 {:.0f}s）symbol={} kind={} event_id={} —— 本轮跳过",
                    self._dedup_window_sec,
                    symbol,
                    kind,
                    event_id,
                )
                continue
            threshold = float(stop if kind == KIND_STOP else take_profit)
            record: dict[str, Any] = {
                "ts": ts.isoformat(timespec="seconds"),
                "event_id": event_id,
                "first_hit": first_hit,
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
            self._mark_event_emitted(symbol, kind)
            self._emit(symbol, kind, record)
        # 本 tick 触发集合之外的 kind：若上一 tick 还在触发，此刻事件段结束
        self._mark_event_closed(symbol, set(hits))
        self._write(records)
        return records

    # ------------------------------------------------------------------ #
    # P2-3 事件语义（2026-09-06）
    # ------------------------------------------------------------------ #
    def _resolve_event(
        self, symbol: str, kind: str, ts: datetime, now_epoch: float
    ) -> tuple[str, bool]:
        """判定本 tick 属于哪个事件段，返回 ``(event_id, first_hit)``。

        事件边界 = **tick 级触发状态的连续性**：上一 tick 未触发、本 tick
        触发 → 新事件；连续触发 → 同段延续。该判定**独立于去重窗口**：
        去重只控制写盘节奏，事件边界忠实于行情结构（价格离开触发区哪怕
        一个 tick，旧事件即结束）。

        ⛔ fail-open（R22）：状态取不到 / 类型损坏 / 任何异常 → 退化为
        「按新事件处理」并打 debug 日志，**绝不抛异常、绝不中断 tick**。
        """
        key = (symbol, kind)
        try:
            with self._event_lock:
                was_in = self._in_trigger.get(key, False)
                self._in_trigger[key] = True
                state = self._event_state.get(key)
                if not was_in or not isinstance(state, dict) or "event_id" not in state:
                    # 新事件（或旧状态损坏 fail-open → 按新事件处理）
                    self._event_seq += 1
                    event_id = f"{symbol}:{kind}:{ts.isoformat(timespec='seconds')}#{self._event_seq}"
                    self._event_state[key] = {"event_id": event_id, "pending_first": True}
                    return event_id, True
                # 同段延续：复用 event_id；first_hit 取决于本段是否已落盘过
                pending = bool(state.get("pending_first", True))
                return str(state["event_id"]), pending
        except Exception as exc:  # noqa: BLE001 — R22：事件状态任何异常不得中断 tick
            log.debug("影子止损事件状态异常（fail-open 按新事件处理）err={}", exc)
            self._event_seq += 1
            return f"{symbol}:{kind}:{ts.isoformat(timespec='seconds')}#{self._event_seq}", True

    def _mark_event_emitted(self, symbol: str, kind: str) -> None:
        """本事件段已落盘一条 → ``pending_first`` 翻 False（后续落盘标延续）。

        ⛔ 在记录已 append 进 ``records`` 之后调用。失败只打日志（R22）：
        翻转失败的最坏后果是同段下一条仍标 ``first_hit=True``（事件计数
        偏多 1），远好于为它引入停摆风险。
        """
        key = (symbol, kind)
        try:
            with self._event_lock:
                state = self._event_state.get(key)
                if isinstance(state, dict):
                    state["pending_first"] = False
        except Exception as exc:  # noqa: BLE001
            log.debug("影子止损事件状态翻转失败（不影响落盘）err={}", exc)

    def _mark_event_closed(self, symbol: str, kinds_with_state: set[str]) -> None:
        """本 tick 结束后，把不再触发的 kind 的事件段关闭（事件结束）。

        ⛔ ``hits`` 只含本 tick 触发的 kind；若某 (symbol, kind) 上一 tick
        还在触发、本 tick 不在，必须显式清状态，否则下次再触发会被误判为
        「同段延续」（一次短暂离开触发区会被并进同一段）。
        """
        try:
            with self._event_lock:
                for kind in list(self._in_trigger.keys()):
                    if kind[0] != symbol or kind[1] in kinds_with_state:
                        continue
                    if self._in_trigger.get(kind):
                        self._in_trigger[kind] = False
                        self._event_state.pop(kind, None)
        except Exception as exc:  # noqa: BLE001 — R22
            log.debug("影子止损事件关闭失败（不影响主流程）err={}", exc)

    def _should_emit(self, symbol: str, kind: str, now_epoch: float) -> bool:
        """去重判定：是否应 emit 本 (symbol, kind)。

        规则：去重窗口非正 → 恒放行（关闭去重）；否则查 ``_last_emit``，若
        存在且 ``now_epoch - last < dedup_window_sec`` → 判为窗口内重复 → ``False``；
        否则记录本次时间戳并放行 ``True``（原子地推进哨兵，杜绝并发重复）。
        """
        window = self._dedup_window_sec
        if window <= 0:
            return True
        with self._lock:
            last = self._last_emit.get((symbol, kind))
            if last is not None and (now_epoch - last) < window:
                return False
            self._last_emit[(symbol, kind)] = now_epoch
            return True

    def _emit(self, symbol: str, kind: str, record: dict[str, Any]) -> None:
        """打 WARNING 告警。影子模式下这是**唯一**的外部可见行为。

        ⚠️ 调用方契约：**调用前必须先过 ``_should_emit`` 去重**（``scan`` 已做）。
        ``_emit`` 自身**不**再去重 —— 去重哨兵 ``_last_emit`` 在 ``_should_emit``
        里推进。若绕过 ``scan`` 直接调 ``_emit``，不会受窗口约束（保持简单语义，
        调用路径只有 ``scan``）。"""
        log.warning(
            "{} symbol={} 持仓={:g} 阈值={:.4f} 触发价={:.4f} 开仓均价={:.4f} "
            "浮动盈亏={:.2f} 行情时间={} event_id={} first_hit={}（影子模式：仅记录，未平仓）",
            KIND_TAG.get(kind, "[SHADOW]"),
            symbol,
            float(record["position"]),
            float(record["threshold"]),
            float(record["trigger_price"]),
            float(record["avg_entry"]),
            float(record["unrealized_pnl"]),
            record["quote_ts"],
            record.get("event_id"),
            record.get("first_hit"),
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
