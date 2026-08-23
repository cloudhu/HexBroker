"""模拟盘公共 DTO 与常量（§3.1 类图 / §7 共享知识）。

组件间一律通过本模块 dataclass 通信，禁止共享可变状态。
所有时间均为 tz-naive 的 Asia/Shanghai 本地时间（``hexbroker/utils/timeutil.py`` 约定）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 符号规范（§7.1）：内部统一 ag0/rb0/c0（小写品种 + 0 主力连续）
# ---------------------------------------------------------------------------
#: 内部短名 → 展示名
SYMBOL_DISPLAY: dict[str, str] = {
    "ag0": "SHFE.ag",
    "rb0": "SHFE.rb",
    "c0": "DCE.c",
}

#: 内部短名 → 新浪实时代码（hq.sinajs.cn）
SINA_REALTIME_CODE: dict[str, str] = {
    "ag0": "nf_AG0",
    "rb0": "nf_RB0",
    "c0": "nf_C0",
}

#: 内部短名 → 新浪 K 线源代码（与 CostModel._short_symbol 语义一致）
SINA_BAR_CODE: dict[str, str] = {
    "ag0": "ag0",
    "rb0": "rb0",
    "c0": "c0",
}

#: 日志事件类型（log_structured 审计 event 名）
EVT_TRADE = "trade"
EVT_PLAN_CHANGE = "plan_change"


def dt_now() -> datetime:
    """当前本地时间（tz-naive，Asia/Shanghai）。"""
    return datetime.now()


@dataclass
class Quote:
    """实时行情快照（§3.1）。"""

    symbol: str
    ts: datetime
    price: float          # 最新价
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_settle: float = 0.0  # 昨结算

    def valid(self) -> bool:
        """行情可用性：价格为正。"""
        return self.price is not None and self.price > 0


@dataclass
class SignalFrame:
    """信号帧（§3.3：v8 缓存列 p_up/exp_ret/is_effective + 来源标注）。"""

    symbol: str
    ts: datetime
    p_up: float
    exp_ret: float
    is_effective: bool
    source: str = "engine_a"   # engine_a / technical / risk_only
    freshness_days: int = 0

    def direction(self) -> int:
        """+1 多 / -1 空 / 0 中性。"""
        if not self.is_effective:
            return 0
        return 1 if self.p_up >= 0.5 else -1


@dataclass
class Plan:
    """交易计划（§3.1；由信号+风控决策生成，可被情报备注/风险提示修改）。"""

    symbol: str
    direction: int            # +1 多 / -1 空 / 0 平
    target_qty: float         # 目标手数（带符号）
    target_pos_pct: float     # 目标仓位比例 [-1,1]
    stop_price: float | None = None
    take_profit: float | None = None
    note: str = ""
    risk_flag: str = ""
    source: str = "engine_a"
    updated_at: datetime = field(default_factory=dt_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanChange:
    """计划变更审计记录（§3.4：情报仅风险提示/备注，Q3 批复）。"""

    symbol: str
    change_type: str         # signal_update / risk_hint / note
    detail: str
    ts: datetime = field(default_factory=dt_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeEvent:
    """一笔成交事件（A3 五要素：品种/方向/仓位/止盈/止损 + 价格/时间/编号）。"""

    trade_id: str
    ts: datetime
    symbol: str
    direction: int           # +1 多 / -1 空
    qty: float               # 成交手数（带符号）
    entry: float             # 建仓/当前持仓均价
    stop: float | None
    take_profit: float | None
    price: float             # 成交价（含滑点）
    fee: float
    is_open: bool
    is_today_close: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def direction_label(self) -> str:
        """日志方向标签：LONG / SHORT / FLAT。"""
        if self.direction > 0:
            return "LONG"
        if self.direction < 0:
            return "SHORT"
        return "FLAT"


@dataclass
class NewsItem:
    """结构化情报事件（§3.4）。"""

    ts: datetime
    symbols: list[str]
    title: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "static"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountSnapshot:
    """账户快照（§3.1；mark-to-market 口径与 SimBroker 一致）。"""

    ts: datetime
    equity: float
    cash: float              # 可用资金 = equity - margin_used
    margin_used: float
    positions: dict[str, float] = field(default_factory=dict)
    avg_entry: dict[str, float] = field(default_factory=dict)
    realized: dict[str, float] = field(default_factory=dict)
    drawdown: float = 0.0
    peak_equity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PositionCtx:
    """单品种持仓上下文（§3.1；RiskGate 构造 RiskState 的输入）。"""

    symbol: str
    position: float = 0.0         # 当前持仓手数（带符号）
    entry_price: float = 0.0
    atr: float = 0.0
    bars_in_position: int = 0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
