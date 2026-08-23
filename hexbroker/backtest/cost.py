"""交易成本模型（§3.5）。

实现期货手续费（开/平/平今 double）+ 滑点（按 tick）+ 保证金。
所有计算为确定性纯函数，供回测引擎与 RL 环境共用，保证一致性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 品种规格乘数/最小跳动默认值（V5 修复：contracts 未提供时按此回退，
# 避免 au/ag 误用全局 10.0 导致 P&L 量级错误）。
# 依据 §3.5 约定：au=×1000/tick0.02、ag=×15/tick0.01、m=×10/tick1。
_SPEC_MULTIPLIER = {"au": 1000.0, "ag": 15.0, "m": 10.0}
_SPEC_MIN_TICK = {"au": 0.02, "ag": 0.01, "m": 1.0}


def _short_symbol(symbol: str | None) -> str | None:
    """规范化品种短名：'SHFE.au'/'au0'/'AU0' -> 'au'（去点前缀与尾部数字）。"""
    if not symbol:
        return None
    return symbol.split(".")[-1].rstrip("0123456789").lower()


@dataclass
class CostModel:
    """交易手续费/滑点/保证金模型。

    contracts 可选：``{symbol: {"multiplier": float, "min_tick": float}}`` 提供
    品种级合约参数（au=×1000/0.02、ag=×15/0.01、m=×10/1）；未提供时回退全局默认。
    """

    fee_open: float = 0.00005
    fee_close: float = 0.00005
    fee_close_today: float = 0.00010
    slippage_ticks: float = 1.0
    margin_rate: float = 0.12
    multiplier: float = 10.0
    min_tick: float = 10.0
    contracts: dict[str, dict] | None = None

    def _multiplier(self, symbol: str | None) -> float:
        if symbol and self.contracts and symbol in self.contracts:
            return float(self.contracts[symbol].get("multiplier", self.multiplier))
        # V5 修复：contracts 未提供或非上市品种时，按品种规格回退，
        # 避免 au(×1000)/ag(×15) 误用全局默认 10.0（P&L 量级错误）。
        key = _short_symbol(symbol)
        if key in _SPEC_MULTIPLIER:
            return _SPEC_MULTIPLIER[key]
        return self.multiplier

    def _min_tick(self, symbol: str | None) -> float:
        if symbol and self.contracts and symbol in self.contracts:
            return float(self.contracts[symbol].get("min_tick", self.min_tick))
        # V5 修复：同上，contracts 缺失时按品种规格回退最小跳动。
        key = _short_symbol(symbol)
        if key in _SPEC_MIN_TICK:
            return _SPEC_MIN_TICK[key]
        return self.min_tick

    @classmethod
    def from_config(cls, cfg: Any) -> "CostModel":
        bc = getattr(cfg, "backtest", None)
        if bc is None:
            return cls()
        contracts = getattr(bc, "contracts", None)
        return cls(
            fee_open=getattr(bc, "fee_rate_open", 0.00005),
            fee_close=getattr(bc, "fee_rate_close", 0.00005),
            fee_close_today=getattr(bc, "fee_rate_close_today", 0.00010),
            slippage_ticks=getattr(bc, "slippage_ticks", 1.0),
            margin_rate=getattr(bc, "margin_rate", 0.12),
            multiplier=getattr(bc, "multiplier", 10.0),
            min_tick=getattr(bc, "min_tick", 10.0),
            contracts=dict(contracts) if contracts else None,
        )

    # ---- 复权成交价（含滑点） ----
    def fill_price(self, ref_price: float, side: int, symbol: str | None = None) -> float:
        """side=+1 买入，side=-1 卖出；滑点使成交价不利。"""
        slip = self._min_tick(symbol) * self.slippage_ticks
        return float(ref_price + side * slip)

    # ---- 手续费 ----
    def fee(self, fill_price: float, qty: float, is_open: bool, is_today_close: bool = False, symbol: str | None = None) -> float:
        if is_open:
            rate = self.fee_open
        elif is_today_close:
            rate = self.fee_close_today
        else:
            rate = self.fee_close
        return float(fill_price * self._multiplier(symbol) * abs(qty) * rate)

    # ---- 单笔交易总成本（手续费 + 滑点成本） ----
    def trade_cost(
        self, ref_price: float, qty: float, is_open: bool, is_today_close: bool = False, symbol: str | None = None
    ) -> tuple[float, float, float, float]:
        """返回 (成交价, 手续费, 滑点成本, 总成本)。"""
        side = 1 if qty > 0 else -1
        fp = self.fill_price(ref_price, side, symbol)
        fee = self.fee(fp, qty, is_open, is_today_close, symbol)
        slip = self._min_tick(symbol) * self.slippage_ticks * self._multiplier(symbol) * abs(qty)
        return fp, fee, slip, fee + slip

    # ---- 保证金占用 ----
    def margin(self, fill_price: float, qty: float, symbol: str | None = None) -> float:
        return float(fill_price * self._multiplier(symbol) * abs(qty) * self.margin_rate)
