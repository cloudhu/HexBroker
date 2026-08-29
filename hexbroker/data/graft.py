"""后复权序列续接器（P0-5）：把备源名义价挂到主源后复权序列上。

为什么不能直接拼接
------------------
主源 pandadata 的 ``close_pcr`` 是**比例（乘性）后复权**价格，消除了主力换月跳空；
所有免费源只给**不复权名义价**（主力连续，保留跳空）。两者价格绝对值差一个常数比例因子
（18 品种跨度 0.4781(cf0) ~ 8.7769(i0)），直接拼接会造出假跳空。

但比**日收益率**后 14/18 品种逐位相同（比值变异系数 ≈1e-16），剩余 4 品种的分歧
**只发生在主力换月当日一天**。这说明两者底层是同一份数据 —— 差异仅是复权处理。

因此正确做法是"**不换口径，只换 raw 供给**"：备源只提供名义价，由本模块按主源口径
续接出后复权价格。

算法
----
设 ``adj`` = 主源后复权序列，``raw`` = 备源名义价序列，``t0`` = 锚点日。

**情形 1（窗口内无换月）** —— 价格连续，比例因子恒定：:

    k = adj[t0] / raw[t0]
    adj[t] = k * raw[t]            (t > t0)

**情形 2（窗口内有换月日 R）** —— 备源在 R 处整段切换合约，需换一个比例因子：:

    spread_R = raw[R] / raw[R-1]              # 观测到的跳空
    k_R = k_{R-1} / spread_R                  # 新段因子
    adj[t] = k_R * raw[t]                     (t >= R)

    此时 adj[R] == adj[R-1]，即**假定换月当日真实收益为 0**（整段跳空都算作价差）。

⚠️ 这是一个**近似**：真实的换月日既有价差也有市场波动，两者无法从主力连续序列中分离。
要精确分离必须拿到新旧两个合约在 R-1 同日的价格（``rollover_spreads`` 参数即为此时预留）。
故跨换月续接会在诊断中**显式告警**，且可用 ``max_graft_days`` 限制窗口。

约束④的正确用法（重要）
----------------------
"段内 ``adj[t]/raw[t]`` 应为常数"在**向前外推时是恒真式** —— 因为 adj 本就是由 raw 算出来的，
比值必然恒定，校验不了任何东西。

它**唯一有意义的位置是锚点之前的重叠区**：那里 ``adj_hist`` 与 ``raw`` 都是已知真值，
比值恒定才真正验证了「两源口径一致」。该校验能：
  1. 验证锚点可靠；
  2. **检出 dominant 日历未登记的换月**（比值在某日突变）；
  3. 发现两源数据本身不一致（如备源脏数据）。

故本模块的流程是：**先在重叠区验证对齐 → 再外推**，而非外推后再检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .. import HexDataError

__all__ = ["GraftResult", "verify_alignment", "graft_adjusted"]

#: 重叠区比值恒定性判定容差（相对标准差）。1e-6 足以容纳浮点误差，
#: 远小于任何真实换月跳空（实测最小 62.7bp ≈ 6.3e-3）。
DEFAULT_ALIGN_TOL = 1e-6


@dataclass
class GraftResult:
    """续接结果。

    属性
    ----
    series : 续接出的完整后复权序列（历史段取主源真值 + 新增段为续接值）
    new_dates : 本次续接新增的日期
    anchor_date : 锚点日
    anchor_ratio : 锚点比例因子 k
    segments : 分段列表 [(起始日, 比例因子)]，段数 >1 表示窗口内发生了换月
    warnings : 诊断告警（非空时调用方应记录/告警，切勿静默）
    alignment : 重叠区对齐校验诊断
    """

    series: pd.Series
    new_dates: list
    anchor_date: object
    anchor_ratio: float
    segments: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    alignment: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否无告警续接成功。False 时调用方应谨慎使用（但不代表数值一定错）。"""
        return not self.warnings


def _as_series(x, name: str) -> pd.Series:
    if isinstance(x, pd.Series):
        s = x.dropna().astype(float)
    else:
        s = pd.Series(dict(x)).astype(float).dropna()
    if s.empty:
        raise HexDataError(f"{name} 为空，无法续接")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if (s <= 0).any():
        bad = s[s <= 0].index[:3].tolist()
        raise HexDataError(f"{name} 含非正价格，无法做比例复权：{bad}")
    return s


def verify_alignment(
    adj_hist: pd.Series,
    raw: pd.Series,
    lookback: int = 60,
    tol: float = DEFAULT_ALIGN_TOL,
) -> dict:
    """在**重叠区**验证两源口径一致，并检出未登记的换月。

    返回诊断字典：
    ``{"n": 重叠样本数, "ratio_mean": 比值均值, "ratio_cv": 相对标准差,
       "aligned": 是否恒定, "breaks": 比值突变日期列表}``
    """
    a = _as_series(adj_hist, "主源后复权序列")
    r = _as_series(raw, "备源名义价序列")

    common = a.index.intersection(r.index)
    if lookback and len(common) > lookback:
        common = common[-lookback:]
    if len(common) < 2:
        return {"n": len(common), "ratio_mean": None, "ratio_cv": None,
                "aligned": False, "breaks": [],
                "note": "重叠样本不足（<2），无法验证对齐"}

    ratio = (a.loc[common] / r.loc[common]).astype(float)
    mean = float(ratio.mean())
    cv = float(ratio.std(ddof=0) / mean) if mean else float("inf")

    # 检出比值突变：相邻日相对变化超过 tol 即视为换月/口径破裂点
    chg = ratio.pct_change().abs()
    breaks = [d for d in chg.index[1:] if float(chg.loc[d]) > max(tol, 1e-9)]

    return {
        "n": int(len(common)),
        "ratio_mean": mean,
        "ratio_cv": cv,
        "aligned": bool(cv <= tol),
        "breaks": list(breaks),
        "window": [str(common.min()), str(common.max())],
    }


def graft_adjusted(
    adj_hist: pd.Series,
    raw_backup: pd.Series,
    rollover_dates=(),
    rollover_spreads: dict | None = None,
    max_graft_days: int = 30,
    lookback: int = 60,
    tol: float = DEFAULT_ALIGN_TOL,
    skip_alignment_check: bool = False,
) -> GraftResult:
    """把备源名义价续接到主源后复权序列上。

    参数
    ----
    adj_hist : 主源（pandadata close_pcr）后复权收盘价，索引为日期。
    raw_backup : 备源（新浪/akshare）主力连续名义收盘价，索引为日期。
    rollover_dates : 窗口内的换月日。
        ⚠️ **已知陷阱（2026-08-28 实测）**：pandadata 的 ``dominant_id`` 切换日
        **不等于**后复权因子切换日 —— cu0 实测相差 3 个交易日（dominant_id 标
        08-24，比值实际在 08-21 已跳变）；ni0 则是 dominant_id 变了但比值未变。
        因此仅传 ``rollover_dates`` 而不传 ``rollover_spreads`` 时，本函数
        **跳过**换月调整（见下）。
    rollover_spreads : 可选，``{换月日: 精确价差}``。**必须**来自逐合约价格
        （新旧主力合约同日收盘价之比），不可用 ``raw[R]/raw[R-1]`` 近似 ——
        18 品种消融证明该近似为净损害。未提供则跳过该日调整并告警。
    max_graft_days : 单次续接天数上限，超过则截断并告警（备源只应急救短窗口）。
    lookback : 重叠区校验回看样本数。
    tol : 重叠区比值恒定性容差。
    skip_alignment_check : 跳过重叠区验证（**仅测试/已知一致时使用**，生产不建议）。

    返回
    ----
    GraftResult —— 含续接序列与诊断。``.warnings`` 非空必须告警，不得静默。
    """
    adj = _as_series(adj_hist, "主源后复权序列")
    raw = _as_series(raw_backup, "备源名义价序列")
    warnings: list[str] = []

    # ---- 1) 重叠区验证（先验证，再外推） --------------------------------
    if skip_alignment_check:
        alignment = {"skipped": True}
    else:
        alignment = verify_alignment(adj, raw, lookback=lookback, tol=tol)
        if alignment.get("n", 0) < 2:
            warnings.append(
                f"重叠样本不足（n={alignment.get('n', 0)}），未经对齐验证即外推，结果不可信"
            )
        elif not alignment.get("aligned"):
            warnings.append(
                f"重叠区比值非恒定（cv={alignment['ratio_cv']:.3e} > tol={tol:.1e}），"
                f"两源口径可能不一致，续接值可能失真"
            )
        if alignment.get("breaks"):
            warnings.append(
                f"重叠区检出 {len(alignment['breaks'])} 处比值突变（疑似未登记换月）："
                f"{[str(d) for d in alignment['breaks'][:5]]}"
            )

    # ---- 2) 锚点 --------------------------------------------------------
    common = adj.index.intersection(raw.index)
    if common.empty:
        raise HexDataError(
            "主源与备源无重叠日期，无法确定锚点比例因子（禁止在无锚点时外推）"
        )
    t0 = common.max()
    k0 = float(adj.loc[t0] / raw.loc[t0])

    # ---- 3) 待续接日期 --------------------------------------------------
    new_dates = [d for d in raw.index if d > t0]
    if not new_dates:
        return GraftResult(
            series=adj, new_dates=[], anchor_date=t0, anchor_ratio=k0,
            segments=[(t0, k0)], warnings=warnings, alignment=alignment,
        )

    if len(new_dates) > max_graft_days:
        warnings.append(
            f"待续接 {len(new_dates)} 天超过上限 {max_graft_days} 天，已截断。"
            "备源只应急救短窗口，长窗口请等主源恢复后全量重建"
        )
        new_dates = new_dates[:max_graft_days]

    # ---- 4) 分段比例因子 ------------------------------------------------
    spreads = rollover_spreads or {}
    roll_set = set(rollover_dates)
    segments: list[tuple] = [(t0, k0)]

    k_by_date: dict = {}
    cur_k = k0
    skipped: list = []
    for d in new_dates:
        if d in roll_set:
            spread = spreads.get(d)
            if spread is None:
                # 证据（2026-08-28，18 品种真实数据消融，见
                # scripts/dev_probe_37_graft_truth.py --ablation）：
                # 近似价差 cur_raw/prev_raw 在 18/18 品种上**不优于**完全不调整，
                # 且在 2/18 品种上显著劣化（cu0 +36.06bp、ni0 +20.12bp）。
                # 根因：pandadata 的 dominant_id 切换日 ≠ 后复权因子切换日
                # （cu0 相差 3 个交易日；ni0 换月但比值根本未变）。
                # 因此：无精确价差时**跳过调整**并告警，绝不静默套用近似。
                skipped.append(d)
                segments.append((d, cur_k))
            elif spread <= 0:
                raise HexDataError(f"换月日 {d} 的价差非正：{spread}")
            else:
                cur_k = cur_k / float(spread)
                segments.append((d, cur_k))
        k_by_date[d] = cur_k

    if skipped:
        warnings.append(
            f"{len(skipped)} 个换月日未提供精确价差，已**跳过**换月调整："
            f"{[str(d) for d in skipped[:5]]}。"
            "实测近似价差为净损害（18/18 品种不优于不调整），故不做近似；"
            "如确需调整请用 rollover_spreads 提供逐合约精确价差"
        )

    # ---- 5) 外推 --------------------------------------------------------
    grafted = pd.Series(
        {d: k_by_date[d] * float(raw.loc[d]) for d in new_dates}, dtype=float
    )
    out = pd.concat([adj, grafted]).sort_index()
    out.name = getattr(adj, "name", None)

    return GraftResult(
        series=out, new_dates=list(new_dates), anchor_date=t0, anchor_ratio=k0,
        segments=segments, warnings=warnings, alignment=alignment,
    )
