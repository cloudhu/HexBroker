"""备源 raw（名义价）拉取器 —— P0-4。

职责：在主源（pandadata ``close_pcr`` 后复权）不可用时，从**免费源**取回
**名义价**，供 :mod:`hexbroker.data.graft` 续接到既有后复权序列上。

设计红线（源自 37 号架构文档 §6）：
1. **备源只供 raw，绝不自行定义主力连续合约、绝不自造后复权**。
   复权口径唯一权威是主源，备源只提供比例因子所需的分子。
2. **失败必须可区分归因**：哪个源、哪种失败（空/陈旧/配额/网络）都要留痕，
   否则"静默降级"会重演 2026-08-28 停摆事故。
3. **新鲜度门禁不放过**：备源拉到的最新 bar 若早于容忍窗口，视为失败并尝试下一源。

源的分层（37 号 §5 关键发现）：
- 新浪新端点与 akshare ``futures_main_sina`` **是同一上游**（实测收盘价逐位相同），
  二者**不构成冗余**。所以这里的"多源"只是**解析层冗余**（防接口格式变更），
  不是**上游冗余**。真正的上游冗余只能由交易所官方源提供（P1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .. import (
    HexDataError,
    HexEmptyDataError,
    HexNetworkError,
    HexQuotaError,
    HexStaleDataError,
)
from .base import DataSource
from .freshness import DEFAULT_MAX_STALE_DAYS, assert_fresh, latest_bar_date
from .sources import AkshareSource, SinaSource

#: 默认优先级。新浪在前（直连、无第三方依赖），akshare 在后（同一上游的
#: 另一层解析，仅防接口格式变更）。
DEFAULT_BACKUP_SOURCES = ("sina", "akshare")

#: 已知备源名。**构造时即校验** —— 配置错误应在建对象时炸，
#: 而不是伪装成"全部备源失败"这种误导性信息。
KNOWN_BACKUP_SOURCES = ("sina", "akshare")


@dataclass
class RawPull:
    """单品种的备源拉取结果。"""

    symbol: str
    #: 名义收盘价序列（**未复权**），索引为 datetime
    close: pd.Series
    #: 来源名（"sina" / "akshare" ...）
    source: str = ""
    #: 持仓量（新浪新端点有，可用于 P0-6 换月检测）
    open_interest: pd.Series | None = None
    #: 成交量
    volume: pd.Series | None = None
    #: 非致命提示（不阻断使用，但必须上报）
    warnings: list[str] = field(default_factory=list)

    @property
    def latest(self):
        """该品种最新 bar 日期。"""
        return latest_bar_date(self.close.to_frame("close"))


class BackupExhaustedError(HexDataError):
    """所有备源均失败。"""

    def __init__(self, message: str, *, attempts: list[tuple[str, str]] | None = None):
        super().__init__(message)
        self.attempts = attempts or []

    def summary(self) -> str:
        return "; ".join(f"{src}: {err}" for src, err in self.attempts)


def _build_source(name: str, root: str | None, save: bool) -> DataSource:
    if name == "sina":
        return SinaSource(root=root, save=save)
    if name == "akshare":
        return AkshareSource(root=root, save=save)
    raise HexDataError(f"未知备源名：{name}（可选：{KNOWN_BACKUP_SOURCES}）")


class BackupRawFetcher:
    """按优先级从免费源拉取**名义价**，失败自动降级到下一源。

    参数
    ----
    sources : 源名元组，按优先级排列。默认 ``("sina", "akshare")``。
    root : 数据湖根目录，透传给各源。
    save : 是否落 Parquet。**默认 False** —— 备源数据仅用于续接，不应污染
        数据湖（否则后续"主源恢复后重建"会分不清哪些是权威数据）。
    max_stale_days : 新鲜度容忍天数（日历日）。
    cross_check : 是否对多源结果做交叉校验。**注意其能力边界**：新浪与
        akshare 是同一上游，此校验只能发现"解析层不一致"，**无法**发现
        上游停更或格式变更（两者会同时坏）。
    """

    def __init__(
        self,
        sources=DEFAULT_BACKUP_SOURCES,
        root: str | None = None,
        save: bool = False,
        max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
        cross_check: bool = True,
        cross_check_tol: float = 1e-6,
    ):
        if not sources:
            raise HexDataError("备源列表为空，无法拉取")
        unknown = [s for s in sources if s not in KNOWN_BACKUP_SOURCES]
        if unknown:
            raise HexDataError(
                f"未知备源名 {unknown}（可选：{list(KNOWN_BACKUP_SOURCES)}）"
            )
        self.source_names = tuple(sources)
        self.root = root
        self.save = save
        self.max_stale_days = max_stale_days
        self.cross_check = cross_check
        self.cross_check_tol = cross_check_tol
        self._sources: dict[str, DataSource] = {}

    # ---- 内部 --------------------------------------------------------
    def _get(self, name: str) -> DataSource:
        if name not in self._sources:
            self._sources[name] = _build_source(name, self.root, self.save)
        return self._sources[name]

    def _pull_one(self, name: str, symbols: list[str], start: str,
                  end: str) -> dict[str, RawPull]:
        """从单个源拉取，返回 ``{symbol: RawPull}``。失败抛异常由调用方归因。"""
        src = self._get(name)
        bf = src.fetch_bars(symbols, start, end, "1d")
        df = bf.df
        out: dict[str, RawPull] = {}
        for sym in df.index.get_level_values("symbol").unique():
            sub = df.loc[[sym]].droplevel("symbol").sort_index()
            if sub.empty:
                continue
            oi = sub["open_interest"] if "open_interest" in sub.columns else None
            vol = sub["volume"] if "volume" in sub.columns else None
            out[str(sym)] = RawPull(
                symbol=str(sym),
                close=sub["close"].astype(float),
                source=name,
                open_interest=(oi.astype(float) if oi is not None else None),
                volume=(vol.astype(float) if vol is not None else None),
            )
        if not out:
            raise HexEmptyDataError(
                f"备源 {name} 返回 0 个品种的有效数据（请求 {symbols}）",
                source=name,
            )
        return out

    def _cross_check(self, a: dict[str, RawPull], b: dict[str, RawPull],
                     name_a: str, name_b: str) -> list[str]:
        """交叉校验两源一致性。仅告警，不阻断。

        能力边界：新浪与 akshare 是同一上游，此校验**不能**检出上游停更
        （两者会同时坏），只能检出解析层/字段映射不一致。
        """
        warns: list[str] = []
        for sym in set(a) & set(b):
            sa, sb = a[sym].close, b[sym].close
            common = sa.index.intersection(sb.index)
            if len(common) < 2:
                continue
            diff = (sa.loc[common] - sb.loc[common]).abs() / sb.loc[common].abs()
            worst = float(diff.max())
            if worst > self.cross_check_tol:
                bad = diff[diff > self.cross_check_tol]
                warns.append(
                    f"{sym}: {name_a} 与 {name_b} 在 {len(bad)}/{len(common)} 个共同日期不一致，"
                    f"最大相对偏差 {worst:.3e}（首例 {bad.index[0].date()}）。"
                    "注意：两源同一上游，此偏差只说明解析层分叉，不代表上游有问题"
                )
        return warns

    # ---- 对外 --------------------------------------------------------
    def fetch_raw(
        self,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict[str, RawPull]:
        """拉取名义价。按 ``sources`` 顺序尝试，首个成功的源即返回。

        抛出
        ----
        BackupExhaustedError : 全部源失败。``.attempts`` 含逐源失败原因。
        """
        if not symbols:
            raise HexDataError("备源拉取：symbols 为空")

        attempts: list[tuple[str, str]] = []
        warns: list[str] = []
        success: dict[str, RawPull] | None = None
        success_name = ""

        for name in self.source_names:
            try:
                got = self._pull_one(name, symbols, start, end)
            except (HexEmptyDataError, HexNetworkError, HexQuotaError) as exc:
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            except HexDataError as exc:
                # 其余数据层错误（契约校验失败等）同样降级，但记录类型
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不可控
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue

            # 新鲜度复核：复用 assert_fresh（自带历史回填豁免），避免自己
            # 重写一个更差的版本。陈旧即视为该源失败，继续尝试下一源。
            stale: list[str] = []
            for sym, pull in got.items():
                try:
                    assert_fresh(
                        pull.close.to_frame("close"), end, source=name,
                        symbols=[sym], max_stale_days=self.max_stale_days,
                    )
                except HexStaleDataError as exc:
                    stale.append(str(exc))
            if stale:
                attempts.append((name, "数据陈旧 -> " + " | ".join(stale[:3])))
                continue

            if success is None:
                success, success_name = got, name
                if not self.cross_check:
                    break
                continue  # 继续拉下一源，用于交叉校验

            # 已有一个成功源，当前是第二个 → 交叉校验后结束
            warns.extend(self._cross_check(success, got, success_name, name))
            break

        if success is None:
            raise BackupExhaustedError(
                f"全部备源失败（{', '.join(self.source_names)}），"
                f"无法取回 {len(symbols)} 个品种的名义价",
                attempts=attempts,
            )

        for pull in success.values():
            pull.warnings.extend(warns)
        return success

    def fetch_close_series(
        self,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict[str, pd.Series]:
        """便捷方法：只要名义收盘价序列（``graft_adjusted`` 的直接输入）。"""
        return {s: p.close for s, p in self.fetch_raw(symbols, start, end).items()}
