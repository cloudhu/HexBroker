"""交易成本模型（§3.5）。

实现期货手续费（开/平/平今 double）+ 滑点（按 tick）+ 保证金。
所有计算为确定性纯函数，供回测引擎与 RL 环境共用，保证一致性。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..market.rule import MarketRuleTable

logger = logging.getLogger(__name__)

# 品种规格乘数/最小跳动默认值（V5 修复：contracts 未提供时按此回退，
# 避免 au/ag 误用全局 10.0 导致 P&L 量级错误）。
# 依据 §3.5 约定：au=×1000/tick0.02、ag=×15/tick0.01、m=×10/tick1。
# R3-1 补全：rb/c 依据 configs/paper.yaml symbols 段显式声明（rb0/c0: multiplier=10, min_tick=1）。
_SPEC_MULTIPLIER = {"au": 1000.0, "ag": 15.0, "m": 10.0, "rb": 10.0, "c": 10.0}
_SPEC_MIN_TICK = {"au": 0.02, "ag": 0.01, "m": 1.0, "rb": 1.0, "c": 1.0}

# 已对「回退全局默认」告警过的 (kind:key)，避免每个 tick 重复刷屏。
_WARNED_FALLBACK: set[str] = set()


def _short_symbol(symbol: str | None) -> str | None:
    """规范化品种短名：'SHFE.au'/'au0'/'AU0' -> 'au'（去点前缀与尾部数字）。"""
    if not symbol:
        return None
    return symbol.split(".")[-1].rstrip("0123456789").lower()


def _warn_fallback_once(kind: str, symbol: str | None, value: float) -> None:
    """品种未在规格表声明、回退全局默认时告警一次（避免静默 10× 虚高；不刷屏）。

    kind 取 "MULTIPLIER"/"MIN_TICK"；仅对非 None symbol 且每 (kind, key) 首次回退时告警。
    """
    key = _short_symbol(symbol)
    if not key:
        return
    dedup_key = f"{kind}:{key}"
    if dedup_key in _WARNED_FALLBACK:
        return
    _WARNED_FALLBACK.add(dedup_key)
    logger.warning(
        "品种 %s 未在 _SPEC_%s 声明，回退全局默认 %s=%s",
        symbol,
        kind,
        kind.lower(),
        value,
    )


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
    # P1-9：分品种中国市场规则表；None → 保证金回退字段 margin_rate（现状 0.12），行为不变。
    market_rules: "MarketRuleTable | None" = None

    def _multiplier(self, symbol: str | None) -> float:
        if symbol and self.contracts and symbol in self.contracts:
            return float(self.contracts[symbol].get("multiplier", self.multiplier))
        # V5 修复：contracts 未提供或非上市品种时，按品种规格回退，
        # 避免 au(×1000)/ag(×15) 误用全局默认 10.0（P&L 量级错误）。
        key = _short_symbol(symbol)
        if key in _SPEC_MULTIPLIER:
            return _SPEC_MULTIPLIER[key]
        _warn_fallback_once("MULTIPLIER", symbol, self.multiplier)
        return self.multiplier

    def _min_tick(self, symbol: str | None) -> float:
        if symbol and self.contracts and symbol in self.contracts:
            return float(self.contracts[symbol].get("min_tick", self.min_tick))
        # V5 修复：同上，contracts 缺失时按品种规格回退最小跳动。
        key = _short_symbol(symbol)
        if key in _SPEC_MIN_TICK:
            return _SPEC_MIN_TICK[key]
        _warn_fallback_once("MIN_TICK", symbol, self.min_tick)
        return self.min_tick

    @classmethod
    def from_config(cls, cfg: Any) -> "CostModel":
        bc = getattr(cfg, "backtest", None)
        if bc is None:
            return cls()
        contracts = getattr(bc, "contracts", None)
        margin_rate = getattr(bc, "margin_rate", 0.12)
        # P1-9：若配置显式给出市场规则 yaml 路径（cfg.market_rules），则加载分品种规则表；
        # 否则不建表（market_rules=None → margin() 回退字段 margin_rate，行为不变，549 全绿）。
        mr_path = getattr(cfg, "market_rules", None)
        market_rules = MarketRuleTable.from_yaml(mr_path) if mr_path else None
        return cls(
            fee_open=getattr(bc, "fee_rate_open", 0.00005),
            fee_close=getattr(bc, "fee_rate_close", 0.00005),
            fee_close_today=getattr(bc, "fee_rate_close_today", 0.00010),
            slippage_ticks=getattr(bc, "slippage_ticks", 1.0),
            margin_rate=margin_rate,
            multiplier=getattr(bc, "multiplier", 10.0),
            min_tick=getattr(bc, "min_tick", 10.0),
            contracts=dict(contracts) if contracts else None,
            market_rules=market_rules,
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
        # P1-9：若配置了分品种规则表，用分品种保证金率；否则回退字段 margin_rate（现状 0.12）。
        rate = self.market_rules.margin_rate(symbol) if self.market_rules else self.margin_rate
        return float(fill_price * self._multiplier(symbol) * abs(qty) * rate)
