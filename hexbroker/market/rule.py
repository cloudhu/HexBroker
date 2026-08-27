"""中国市场规则表（§3 / P1-9）。

首批三字段（裁决 ⑤）：分品种 ``margin_rate`` / ``limit_up``+``limit_down`` /
``delivery_rule``（"none" | "no_open" 交割月禁开仓）。交易时段/夜盘本次不纳入规则表
（paper ``TradingSession`` 已覆盖，仅提升为共享 ``market/session.py``）。

默认等价：默认表 = 统一 0.12 保证金 / 无幅度覆盖 / 无交割限制 → 与现状数值一致（<1e-12）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, cast

import yaml


def _to_date(ts: Any) -> Optional[date]:
    """把时间戳（datetime / date / pd.Timestamp / ISO 字符串）归一为 date；无法解析返回 None。"""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if hasattr(ts, "date"):
        try:
            return cast(date, ts.date())
        except Exception:
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).date()
        except Exception:
            return None
    return None


@dataclass(frozen=True)
class MarketRule:
    """单品种中国市场规则。

    ``delivery_months`` 为 ``no_open`` 模式下判定的交割月集合（扩展字段，便于测试与实盘配置；
    未声明时 ``no_open`` 保守地始终禁止开仓）。
    """

    symbol: str
    margin_rate: float = 0.12
    limit_up: Optional[float] = None  # 幅度（比例）；None=不覆盖（沿用现状 price 列拦截）
    limit_down: Optional[float] = None
    delivery_rule: str = "none"  # "none" | "no_open"
    delivery_months: Optional[tuple[int, ...]] = None
    trading_session: Optional[list[Any]] = None

    def allows_open(self, ts: Any) -> bool:
        """该品种在 ``ts`` 是否允许开仓。

        - ``none``：始终允许。
        - ``no_open``：``ts`` 落在交割月（``delivery_months``）内 → 禁止开仓；
          未声明 ``delivery_months`` 时保守地始终禁止。
        """
        if self.delivery_rule == "none":
            return True
        if self.delivery_rule == "no_open":
            d = _to_date(ts)
            if d is None:
                return True
            months = self.delivery_months
            if months is None:
                return False
            return d.month not in months
        return True


class MarketRuleTable:
    """分品种中国市场规则表（P1-9）。

    读取点：``CostModel``（保证金）/ ``BacktestEngine``（涨跌停幅度 + 交割月禁开仓）。
    默认回退 ``default``（统一 0.12 / 无幅度 / 无交割限制）→ 与现状数值一致。
    """

    def __init__(
        self,
        rules: dict[str, MarketRule],
        default: MarketRule = MarketRule(symbol="*", margin_rate=0.12),
    ) -> None:
        self._rules = dict(rules)
        self._default = default

    def _rule(self, symbol: str) -> MarketRule:
        return self._rules.get(symbol, self._default)

    def margin_rate(self, symbol: str) -> float:
        """分品种保证金率；缺省回退 default（默认 0.12）。"""
        return float(self._rule(symbol).margin_rate)

    def limit(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        """分品种涨跌停幅度（比例）；(None, None) = 不覆盖（沿用现状 price 列拦截）。"""
        r = self._rule(symbol)
        return (r.limit_up, r.limit_down)

    def allows_open(self, symbol: str, ts: Any) -> bool:
        """该品种在 ``ts`` 是否允许开仓（交割月禁开仓规则）。"""
        return self._rule(symbol).allows_open(ts)

    @classmethod
    def default(cls) -> "MarketRuleTable":
        """空规则表（全部回退 default 统一值）。"""
        return cls(rules={})

    @classmethod
    def from_yaml(cls, path: Optional[str]) -> "MarketRuleTable":
        """从 yaml 加载分品种覆盖；文件缺失 → 默认统一值（零变化）。"""
        if path is None or not Path(path).exists():
            return cls.default()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        rules: dict[str, MarketRule] = {}
        for sym, rd in (data.get("rules") or {}).items():
            rd = rd or {}
            dm = rd.get("delivery_months")
            rules[sym] = MarketRule(
                symbol=sym,
                margin_rate=float(rd.get("margin_rate", 0.12)),
                limit_up=rd.get("limit_up"),
                limit_down=rd.get("limit_down"),
                delivery_rule=str(rd.get("delivery_rule", "none")),
                delivery_months=tuple(int(x) for x in dm) if dm else None,
                trading_session=rd.get("trading_session"),
            )
        return cls(rules=rules)
