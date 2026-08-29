"""年度口径一致性检测（P0-10 审计的固化件）。

背景（37 号文档 §6.5.7 / §4.12，2026-08-29 全库审计）：
- ``processed`` 湖内 ``raw_close`` 列恒等于 ``adj_close``（p6_4 管线映射），
  **不携带名义价校准信息**——名义价冒充只能靠外部参照（新浪/akshare）定罪。
- 但**后复权序列在任何日期都不应大幅跳变**（复权因子的存在意义就是消除
  换月跳空；年度边界通常无换月）。因此年度边界的 adj 大幅跳变是
  口径断裂的**内部可检信号**——零外部依赖，可进 CI。

两类检测：
1. :func:`boundary_fake_gaps` —— 年度边界 adj 跳变（只看相邻年；缺失年
   跨界拼接的幅度是多年累积，不判罪）。跳变超阈值即告警，``rollover``
   标记仅作参考**不豁免**（真后复权在换月日也应连续）。
2. :func:`nominal_suspect_years` —— 名义价嫌疑年（外部参照 k≡1）。
   需要调用方提供外部名义价；湖内 raw_close 不可作参照。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

#: 年度边界 adj 跳变告警阈值。期货单日涨跌停幅度量级（4%~12%），
#: 超过即高度可疑；**定罪仍需外部名义价参照**（本检测是初筛，非终审）。
DEFAULT_MAX_JUMP = 0.10

#: 名义价判定容差：|k - 1| 全年不超过此值即视为 k≡1。
DEFAULT_FLAT1_TOL = 5e-4


@dataclass
class BoundaryGap:
    """年度边界 adj 跳变记录。"""

    from_year: int
    to_year: int
    prev_close: float
    next_close: float
    #: ``next_close / prev_close - 1``，即拼接注入的虚假收益率
    jump: float
    #: 边界两日的 is_rollover 标记（参考信息，不豁免）
    rollover_prev: bool
    rollover_next: bool


def boundary_fake_gaps(
    adj_by_year: dict[int, pd.Series],
    *,
    max_jump: float = DEFAULT_MAX_JUMP,
    roll_by_year: dict[int, pd.Series] | None = None,
) -> list[BoundaryGap]:
    """扫描年度边界的 adj 跳变（P0-10 内部初筛）。

    参数
    ----
    adj_by_year : ``{year: 后复权 close 序列}``，索引为日期。
    max_jump : 跳变告警阈值（双边）。
    roll_by_year : 可选，``{year: is_rollover 序列}``，与 adj 同索引对齐；
        仅用于在结果中记录边界换月标记，不做豁免。

    返回
    ----
    超阈值的 :class:`BoundaryGap` 列表（按年度顺序）。**只比较相邻年**：
    中间年度缺失（如 2020 已隔离）时的跨界拼接跨度 >1 年，幅度属正常
    多年累积，不判罪。
    """
    gaps: list[BoundaryGap] = []
    ys = sorted(y for y, s in adj_by_year.items() if s is not None and len(s))
    for a, b in zip(ys, ys[1:]):
        if b - a != 1:
            continue
        sa = adj_by_year[a].sort_index()
        sb = adj_by_year[b].sort_index()
        if sa.empty or sb.empty:
            continue
        prev, nxt = float(sa.iloc[-1]), float(sb.iloc[0])
        if prev <= 0:
            continue
        jump = nxt / prev - 1.0
        if abs(jump) <= max_jump:
            continue
        rp = rn = False
        if roll_by_year:
            rp = bool(roll_by_year.get(a, pd.Series(dtype=float)).iloc[-1]) \
                if len(roll_by_year.get(a, pd.Series(dtype=float))) else False
            rn = bool(roll_by_year.get(b, pd.Series(dtype=float)).iloc[0]) \
                if len(roll_by_year.get(b, pd.Series(dtype=float))) else False
        gaps.append(BoundaryGap(a, b, prev, nxt, jump, rp, rn))
    return gaps


def nominal_suspect_years(
    adj_by_year: dict[int, pd.Series],
    nominal: pd.Series,
    *,
    flat1_tol: float = DEFAULT_FLAT1_TOL,
) -> list[int]:
    """外部参照校准：找出 k≡1 的名义价嫌疑年。

    ``k = adj_close / 外部名义价``。某年度内 |k-1| 全部不超过 ``flat1_tol``
    即判为名义价嫌疑年。**注意**：k≈1 也可能是真值特征（滚动价差极小的
    品种，如 ag/au/m），单凭本函数不定罪——必须结合相邻年度 k 的量级
    对照（如 rb0/cu0 的 k≈1.3~1.5 中突现 k≡1 年）与边界跳变共同裁决。
    """
    nom = nominal.dropna()
    nom.index = pd.to_datetime(nom.index)
    nom = nom[~nom.index.duplicated()].sort_index().astype(float)
    suspects: list[int] = []
    for y in sorted(adj_by_year):
        s = adj_by_year[y]
        if s is None or s.empty:
            continue
        s = s.sort_index()
        s.index = pd.to_datetime(s.index)
        common = s.index.intersection(nom.index)
        if common.empty:
            continue
        k = (s.loc[common] / nom.loc[common]).dropna()
        if k.empty:
            continue
        if (k - 1.0).abs().max() <= flat1_tol:
            suspects.append(y)
    return suspects
