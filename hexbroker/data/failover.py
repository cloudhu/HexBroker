"""三级故障切换编排器（主源 → 备1 → 备2）—— P0 链路收口。

职责边界（薄层组合，不重新造轮子）：
- Tier 1 主源：pandadata ``close_pcr`` 后复权真值。**注入式** —— 主源是
  MCP 连接器（token 可失效），本模块绝不绑定具体实现。
- Tier 2 备1：:class:`~hexbroker.data.backup.BackupRawFetcher`（sina/akshare
  同一上游，仅解析层冗余）拉**名义价**，经
  :func:`~hexbroker.data.graft.graft_adjusted` 续接到湖内既有后复权序列上。
- Tier 3 备2：交易所官方（CZCE `.txt` / SHFE 待修，P1）。同样注入式，
  缺省为 None（不可用即如实归因，不伪装成功）。

差异化路由（按 §4.1 异常类型，防止"盲目重试"与"过早放弃"两个极端）：
- ``HexQuotaError``（配额/token）→ **当日锁定主源**，会话内不再重试；
- ``HexNetworkError``（网络抖动）→ 重试**一次**，再失败才降级；
- ``HexEmptyDataError`` / ``HexStaleDataError`` → 源已坏，直接降级。

红线（37 号文档 §6）：
- 备源**绝不自造后复权** —— 湖内无该品种历史（无锚点）时拒绝续接，
  如实失败而不是用名义价冒充。
- 续接段**一律 provisional**：成功后自动调用
  :func:`~hexbroker.data.rebuild.mark_provisional` 按年挂标（P0-9 集成）。
- 编排器**不落盘数据**（写路径归既有流程），只挂 provisional 标记。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .. import (
    HexDataError,
    HexEmptyDataError,
    HexNetworkError,
    HexQuotaError,
    HexStaleDataError,
)
from .backup import BackupExhaustedError, BackupRawFetcher
from .graft import DEFAULT_ALIGN_TOL, GraftResult, graft_adjusted
from .rebuild import mark_provisional
from .store import DataLake

#: 主源拉取器签名：``(symbols, start, end) -> {symbol: 后复权真值 close 序列}``
PrimaryFetcher = Callable[[list[str], str, str], dict[str, pd.Series]]

#: Tier 3 交易所官方拉取器签名：``(symbols, start, end) -> {symbol: 名义价 close 序列}``
ExchangeFetcher = Callable[[list[str], str, str], dict[str, pd.Series]]


@dataclass
class Attempt:
    """单次尝试归因。失败必须可区分：哪层、哪个源、什么异常。"""

    tier: str          # primary | backup | exchange
    source: str
    ok: bool = False
    kind: str = ""     # Quota | Network | Empty | Stale | Exhausted | NoAnchor | Other
    error: str = ""


@dataclass
class Outcome:
    """单品种故障切换结果。"""

    symbol: str
    tier_hit: str = ""                 # primary | backup | exchange | none
    source: str = ""
    #: 续接出的完整后复权序列（主源命中时即真值；备源命中时含新增段）
    adj_close: pd.Series | None = None
    #: 备源名义价（主源命中时为 None —— 真值场景无 raw 对照）
    raw_close: pd.Series | None = None
    #: True 表示含未经主源校验的续接段（Tier 2/3 命中时恒为 True）
    provisional: bool = False
    new_dates: list = field(default_factory=list)
    graft: GraftResult | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.adj_close is not None

    @property
    def primary_error_kind(self) -> str:
        """主源失败类型（供调用方决定告警级别）。'' 表示主源正常。"""
        for a in self.attempts:
            if a.tier == "primary" and not a.ok:
                return a.kind
        return ""

    def attempts_summary(self) -> str:
        return "; ".join(
            f"{a.tier}/{a.source}:{'OK' if a.ok else a.kind}" for a in self.attempts
        )


class FailoverOrchestrator:
    """按 主源 → 备1 → 备2 顺序取后复权序列，失败差异化路由。

    参数
    ----
    primary_fetcher : 主源真值拉取器（pandadata MCP 的适配器由调用方注入）。
        None 表示主源未接线（如 MCP 未连接），直接从备源开始。
    backup : 备源拉取器。缺省自建（save=False，不落数据湖）。
    exchange_fetcher : Tier 3 交易所官方（P1 实现后注入）。None = 该层不可用。
    root : 数据湖根目录。用于读取续接锚点（湖内既有后复权序列）与挂
        provisional 标记。
    max_stale_days : 主源新鲜度容忍天数。
    lookback : 双重语义 —— ①备源/Tier3 请求窗口向前扩展的日历天数（保证
        与湖内锚点有重叠日期，graft 锚定的前提）；②透传给
        :func:`graft_adjusted` 的重叠区校验样本数。
    tol : 重叠区比值恒定性容差。
    """

    def __init__(
        self,
        primary_fetcher: PrimaryFetcher | None = None,
        backup: BackupRawFetcher | None = None,
        exchange_fetcher: ExchangeFetcher | None = None,
        root: str | None = None,
        max_stale_days: int = 5,
        max_graft_days: int = 30,
        lookback: int = 60,
        tol: float = DEFAULT_ALIGN_TOL,
    ):
        self.primary_fetcher = primary_fetcher
        self.exchange_fetcher = exchange_fetcher
        self.backup = backup or BackupRawFetcher(save=False)
        self.root = root
        self.max_stale_days = max_stale_days
        self.max_graft_days = max_graft_days
        self.lookback = lookback
        self.tol = tol
        #: Quota 锁定（当日日期字符串）。主源配额/token 故障当日不再重试。
        self._quota_locked_date: str | None = None

    # ---- 主源 ---------------------------------------------------------
    def _try_primary(self, symbols: list[str], start: str, end: str,
                     outcome_of: dict[str, Outcome]) -> dict[str, pd.Series] | None:
        """尝试主源。返回真值字典，全部失败返回 None（归因写进 outcome）。"""
        if self.primary_fetcher is None:
            for o in outcome_of.values():
                o.attempts.append(Attempt("primary", "pandadata", kind="NotWired",
                                          error="主源未接线（primary_fetcher=None）"))
            return None
        if self._quota_locked_date == end[:10]:
            for o in outcome_of.values():
                o.attempts.append(Attempt("primary", "pandadata", kind="QuotaLocked",
                                          error="主源今日已因配额/token 锁定，跳过重试"))
            return None

        def _one_try() -> dict[str, pd.Series] | None:
            try:
                got = self.primary_fetcher(symbols, start, end)
            except HexQuotaError as exc:
                self._quota_locked_date = end[:10]
                for o in outcome_of.values():
                    o.attempts.append(Attempt("primary", "pandadata", kind="Quota",
                                              error=str(exc)))
                return None
            except HexNetworkError as exc:
                for o in outcome_of.values():
                    o.attempts.append(Attempt("primary", "pandadata", kind="Network",
                                              error=str(exc)))
                return None  # 交由外层做一次重试
            except (HexEmptyDataError, HexStaleDataError) as exc:
                # 源已坏（停更/陈旧），重试无意义 → 直接降级
                kind = type(exc).__name__.replace("Hex", "").replace("DataError", "")
                for o in outcome_of.values():
                    o.attempts.append(Attempt("primary", "pandadata", kind=kind,
                                              error=str(exc)))
                return None
            except Exception as exc:  # noqa: BLE001 - 第三方适配层异常类型不可控
                for o in outcome_of.values():
                    o.attempts.append(Attempt("primary", "pandadata", kind="Other",
                                              error=f"{type(exc).__name__}: {exc}"))
                return None
            if not got:
                for o in outcome_of.values():
                    o.attempts.append(Attempt("primary", "pandadata", kind="Empty",
                                              error="主源返回空字典"))
                return None
            return got

        got = _one_try()
        if got is None and any(
            a.tier == "primary" and a.kind == "Network" for o in outcome_of.values()
            for a in o.attempts
        ):
            # 网络抖动重试一次（防抖，不针对 Quota/Empty/Stale）
            got = _one_try()
        if got is None:
            return None
        for o in outcome_of.values():
            o.attempts.append(Attempt("primary", "pandadata", ok=True))
        return got

    # ---- 备源 ---------------------------------------------------------
    def _overlap_start(self, start: str) -> str:
        """备源/Tier3 请求窗口向前扩展 ``lookback`` 天。

        graft_adjusted 依赖 raw 与湖内 adj 的**重叠日期**确定锚点比例因子
        （无重叠即拒绝，红线）。若按调用方 ``start`` 原样请求备源，锚点
        止于 start 之前时两序列无交集，续接必然失败 —— 故请求窗口必须
        向前扩展，让备源把锚点重叠段一并返回。
        """
        return (pd.Timestamp(start) - pd.Timedelta(days=self.lookback)).strftime(
            "%Y-%m-%d"
        )

    def _anchor(self, symbol: str) -> pd.Series | None:
        """湖内既有后复权 close（续接锚点）。无历史返回 None。"""
        if not self.root:
            return None
        try:
            bf = DataLake(self.root).load_processed(symbol, "1d")
        except FileNotFoundError:
            return None
        df = bf.df
        if df.empty:
            return None
        s = df.xs(symbol, level="symbol")["close"].astype(float)
        s.index = pd.to_datetime(s.index)
        return s[~s.index.duplicated(keep="last")].sort_index()

    def _try_backup(self, symbols: list[str], start: str, end: str,
                    outcome_of: dict[str, Outcome]) -> dict[str, bool]:
        """备源 + 续接。返回 {symbol: 是否成功}。"""
        success: dict[str, bool] = {}
        try:
            raw = self.backup.fetch_close_series(symbols, self._overlap_start(start), end)
        except BackupExhaustedError as exc:
            for sym, o in outcome_of.items():
                o.attempts.append(Attempt("backup", self.backup.source_names and
                                          "+".join(self.backup.source_names) or "backup",
                                          kind="Exhausted", error=exc.summary()))
                success[sym] = False
            return success
        except Exception as exc:  # noqa: BLE001
            for sym, o in outcome_of.items():
                o.attempts.append(Attempt("backup", "backup", kind="Other",
                                          error=f"{type(exc).__name__}: {exc}"))
                success[sym] = False
            return success

        for sym, o in outcome_of.items():
            if sym not in raw:
                o.attempts.append(Attempt("backup", "backup", kind="Empty",
                                          error="备源未返回该品种"))
                success[sym] = False
                continue
            raw_s = raw[sym].astype(float)
            adj_hist = self._anchor(sym)
            if adj_hist is None or adj_hist.empty:
                # 红线：无锚点绝不自造后复权
                o.attempts.append(Attempt(
                    "backup", "backup", kind="NoAnchor",
                    error="湖内无该品种既有后复权序列，备源名义价无法续接（红线：备源绝不自造后复权）",
                ))
                success[sym] = False
                continue
            try:
                res = graft_adjusted(
                    adj_hist, raw_s, max_graft_days=self.max_graft_days,
                    lookback=self.lookback, tol=self.tol,
                )
            except HexDataError as exc:
                o.attempts.append(Attempt("backup", "backup", kind="Graft",
                                          error=str(exc)))
                success[sym] = False
                continue
            if not res.new_dates:
                o.attempts.append(Attempt("backup", "backup", kind="Empty",
                                          error="备源数据无新增日期（湖内已覆盖）"))
                success[sym] = False
                continue
            o.tier_hit = "backup"
            o.source = "backup:" + "+".join(self.backup.source_names)
            o.adj_close = res.series
            o.raw_close = raw_s
            o.provisional = True        # 续接段一律临时值（§6 硬约束 6）
            o.new_dates = list(res.new_dates)
            o.graft = res
            o.attempts.append(Attempt("backup", "backup", ok=True))
            # P0-9 集成：按年挂标（幂等）。不落数据，只留痕。
            if self.root:
                by_year: dict[int, list] = {}
                for d in res.new_dates:
                    by_year.setdefault(pd.Timestamp(d).year, []).append(d)
                for y, dates in by_year.items():
                    mark_provisional(
                        self.root, "processed", sym, "1d", y, dates,
                        method="graft", reason=f"failover: {res.warnings or '续接段'}",
                    )
            success[sym] = True
        return success

    # ---- 对外 ---------------------------------------------------------
    def fetch_adjusted(self, symbols: list[str], start: str, end: str,
                       ) -> dict[str, Outcome]:
        """取后复权序列。主源 → 备1 → 备2 逐层降级，逐次尝试全量归因。"""
        if not symbols:
            raise HexDataError("failover: symbols 为空")
        outcome_of = {s: Outcome(symbol=s) for s in symbols}

        got = self._try_primary(symbols, start, end, outcome_of)
        hit_primary = set()
        if got:
            for sym, o in outcome_of.items():
                if sym in got and got[sym] is not None:
                    s = got[sym].astype(float).dropna()
                    if len(s):
                        o.tier_hit = "primary"
                        o.source = "primary:pandadata"
                        o.adj_close = s.sort_index()
                        o.provisional = False
                        hit_primary.add(sym)
                    else:
                        o.attempts.append(Attempt("primary", "pandadata", kind="Empty",
                                                  error="主源返回该品种空序列"))
        rest = [s for s in symbols if s not in hit_primary]
        if rest:
            self._try_backup(rest, start, end, outcome_of)
            rest = [s for s, o in outcome_of.items() if not o.ok]
        if rest and self.exchange_fetcher is not None:
            try:
                ex = self.exchange_fetcher(rest, self._overlap_start(start), end)
            except Exception as exc:  # noqa: BLE001
                for s in rest:
                    outcome_of[s].attempts.append(
                        Attempt("exchange", "official", kind="Other",
                                error=f"{type(exc).__name__}: {exc}"))
                ex = None
            if ex:
                for sym, o in outcome_of.items():
                    if sym in ex and o.adj_close is None:
                        s = ex[sym].astype(float).dropna()
                        if len(s):
                            # 交易所官方同备源语义：名义价，仍需续接 + provisional
                            o.raw_close = s.sort_index()
                            adj_hist = self._anchor(sym)
                            if adj_hist is None or adj_hist.empty:
                                o.attempts.append(Attempt(
                                    "exchange", "official", kind="NoAnchor",
                                    error="湖内无锚点，官方名义价无法续接"))
                                continue
                            res = graft_adjusted(
                                adj_hist, o.raw_close,
                                max_graft_days=self.max_graft_days,
                                lookback=self.lookback, tol=self.tol,
                            )
                            o.tier_hit = "exchange"
                            o.source = "exchange:official"
                            o.adj_close = res.series
                            o.provisional = True
                            o.new_dates = list(res.new_dates)
                            o.graft = res
                            o.attempts.append(Attempt("exchange", "official", ok=True))
        return outcome_of
