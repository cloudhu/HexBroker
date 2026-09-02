"""D-5：sizing 层归零也计入 ``_block_reasons``（补 C2 的统计盲区）。

背景（2026-09-02 实证，取证脚本 artifacts/_tmp/verify_d5_gap.py 全绿）：
``scheduler._process_symbol`` 第一路拦截统计只认「风控意图 = 0」；而 sizing 归零
（risk_budget / margin_cap / min_lot_threshold）发生在 planner 层 —— 此时
``decision.target_position`` **非零**，那路统计不成立 → C2 的「0 开仓原因分布」
永远看不到 risk_budget / margin_cap。

生产实证：09-01 ag0 被 risk_budget 拦 14 次（20:38–21:50 夜盘），而当天 C2 的
43 次输出全在日盘 09:08–10:59，全库原因分布仅见 rl_intent(71) / signal_cooldown(67)。
最坏场景（所有品种都「风控想开、但手数算出 0」）``_block_reasons`` 为空 →
``_warn_zero_open`` 直接 return，彻底静默；叠加 P1 把 ag0 每轮 WARNING 降为
每日 1 条 INFO 后，静默更彻底。

修法：新增 ``TradingScheduler._record_sizing_block`` —— 「无持仓 + 风控想开 +
手数算出 0」三条件同时成立时，按 ``size_metrics()["capped_by"]`` 归因补记。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from hexbroker.paper.planner import PlanManager
from hexbroker.paper.scheduler import TradingScheduler
from hexbroker.paper.types import Plan, Quote

EQUITY = 94856.70
DAY = "2026-09-02"
MULTIPLIERS = {"ag0": 15.0, "rb0": 10.0}


def _planner(**kw) -> PlanManager:
    base = dict(
        multipliers=MULTIPLIERS,
        plans_dir="trade_plans",
        size_by_risk=True,
        risk_per_trade=0.01,
        risk_stop_atr_mult=2.5,
        max_position_pct=0.50,
        margin_rate=0.12,
        max_margin_pct=0.20,
        struct_untradeable_ratio=3.0,
        risk_per_trade_by_symbol={"ag0": 0.01},
    )
    base.update(kw)
    return PlanManager(**base)


def _sched(planner: PlanManager) -> TradingScheduler:
    s = TradingScheduler.__new__(TradingScheduler)   # 跳过 __init__（重依赖）
    s._planner = planner
    s._block_reasons = {}
    return s


def _quote(sym: str, price: float) -> Quote:
    return Quote(symbol=sym, ts=datetime(2026, 9, 2, 21, 0, 0), price=price)


def _plan(sym: str, qty: float) -> Plan:
    return Plan(
        symbol=sym,
        direction=1 if qty > 0 else 0,
        target_qty=qty,
        target_pos_pct=0.30 if qty else 0.0,
    )


def _record(sched, sym, price, equity, intent, qty, position=0.0, stop=None, atr=None):
    sched._record_sizing_block(
        sym,
        DAY,
        SimpleNamespace(target_position=intent, stop_price=stop),
        _plan(sym, qty),
        SimpleNamespace(position=position),
        _quote(sym, price),
        SimpleNamespace(equity=equity),
        atr,
    )
    return sched._block_reasons.get(DAY) or {}


# ---- ① 主场景：风控想开仓、sizing 归零 → 必须被归因 ----
def test_risk_budget_block_recorded():
    """ag0 意图 30% → risk_budget 拦（09-01 生产实证 14 次的场景）。

    修复前：这一路统计条件 ``abs(target_position) < 1e-9`` 不成立 →
    C2 全库从未出现过 risk_budget。
    """
    sched = _sched(_planner())
    got = _record(sched, "ag0", 16245.0, EQUITY, 0.30, 0.0, atr=615.00)
    assert got.get("risk_budget") == 1, got
    # 多轮拦截按次累计（与第一路统计的计数语义一致）
    _record(sched, "ag0", 16245.0, EQUITY, 0.30, 0.0, atr=615.00)
    assert sched._block_reasons[DAY]["risk_budget"] == 2


def test_margin_cap_block_recorded():
    """保证金硬顶拦截同样要被归因（rb0 + max_margin_pct=0.01 逼出 margin_cap）。"""
    sched = _sched(_planner(max_margin_pct=0.01))
    got = _record(sched, "rb0", 3174.0, EQUITY, 0.30, 0.0, atr=31.76)
    assert got.get("margin_cap") == 1, got


# ---- ② 反向：三条件不满足时一律不记录（不得污染既有统计）----
def test_intent_zero_not_recorded():
    """风控意图 = 0 → 归第一路统计（rl_intent 等），本方法不得重复记。"""
    sched = _sched(_planner())
    got = _record(sched, "rb0", 3174.0, EQUITY, 0.0, 0.0, atr=31.76)
    assert got == {}, got


def test_qty_nonzero_not_recorded():
    """手数 > 0（正常开仓）→ 不是拦截，不记录。"""
    sched = _sched(_planner())
    got = _record(sched, "rb0", 3174.0, EQUITY, 0.30, 1.0, atr=31.76)
    assert got == {}, got


def test_with_position_not_recorded():
    """已有持仓（调仓/平仓场景）→ 不属于「0 开仓」统计范畴，不记录。"""
    sched = _sched(_planner())
    got = _record(sched, "ag0", 16245.0, EQUITY, 0.30, 0.0, position=1.0, atr=615.00)
    assert got == {}, got
