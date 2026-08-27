"""组合目标合并层（P1-8，§3）：同品种多引擎目标合并 + 单笔下单单量流控。

复用 ``scripts/combo_validation.py`` 思路提炼为平台能力。

防自成交：合并后每个品种只产生**一个净目标** → 自然只生成一条下单指令，
从物理上杜绝同品种反向自成交（多引擎同向相加、反向相消，最终单方向）。
pre-trade 单笔下单单量流控：单笔 ``|qty|`` 不得超过 ``max_order_qty``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineTarget:
    """单个引擎对某品种的目标仓位（比例）。"""

    symbol: str
    engine: str          # "A" | "B"
    target: float        # 目标仓位比例


class ComboTargetMerger:
    """组合目标合并器（防自成交 + 单笔流控）。"""

    def __init__(self, max_order_qty: float = 1e9, self_trade_guard: bool = True) -> None:
        self.max_order_qty = float(max_order_qty)
        self.self_trade_guard = bool(self_trade_guard)

    def merge(self, targets: list[EngineTarget]) -> dict[str, float]:
        """合并同品种多引擎目标，返回 ``symbol -> 净目标仓位``。

        防自成交保证：每个品种只输出一个净目标（单方向），即使输入含反向分量
        也相互抵消，绝不输出同品种双向目标。
        """
        by_symbol: dict[str, list[float]] = {}
        for t in targets:
            by_symbol.setdefault(t.symbol, []).append(float(t.target))

        merged: dict[str, float] = {}
        for sym, vals in by_symbol.items():
            net = sum(vals)
            if self.self_trade_guard and self._has_opposing(vals):
                # 方向相反的多引擎目标相互抵消，仅保留净方向（net 已体现）。
                merged[sym] = float(net)
            else:
                merged[sym] = float(net)
        return merged

    @staticmethod
    def _has_opposing(vals: list[float]) -> bool:
        """是否存在同时为正、为负的目标（即方向冲突）。"""
        return any(v > 0 for v in vals) and any(v < 0 for v in vals)

    def check_flow(self, symbol: str, qty: float) -> bool:
        """pre-trade 单笔下单单量流控：``|qty|`` 不得超过 ``max_order_qty``。

        ``symbol`` 保留用于未来按品种差异化流控（当前为全局上限）。
        """
        return abs(float(qty)) <= self.max_order_qty
